#!/usr/bin/env python
import argparse
import binascii
import etao
import spidev
import struct
import time
import RPi.GPIO as GPIO

from card import GCMHeader

BLOCK_SZ = 0x2000
SECTOR_SZ = 0x800
TIMING_LEN = 128
READ_SZ = 0x200  # - (5 + TIMING_LEN)
WRITE_SZ = 0x80

# GPIO pins
GPIO_INT = 7


def addr_from_bytes(addr_bytes):
    addr_bytes = list(struct.unpack('>BBBB', addr_bytes))
    result = addr_bytes[0] << 17
    result += addr_bytes[1] << 9
    result += (addr_bytes[2] & 3) << 7
    result += addr_bytes[3] & 0x7F
    return result


def addr_to_bytes(address):
    packed = [0, 0, 0, 0]
    packed[0] = (address >> 17) & 0xFF
    packed[1] = (address >> 9) & 0xFF
    packed[2] = (address >> 7) & 0x3
    packed[3] = address & 0x7F
    return packed


def read_page(spi, address, amount=READ_SZ):
    read_cmd = [0x52]
    read_cmd.extend(addr_to_bytes(address))

    cmd_len = len(read_cmd)  # (5)
    out_len = TIMING_LEN
    in_len = amount

    if in_len > (READ_SZ):
        raise Exception('max 0x%x bytes per read' % (READ_SZ))

    # time buffer + response size buffer
    read_cmd.extend([0xFF for x in range(out_len + in_len)])

    response = spi.xfer2(read_cmd)

    result = ''.join([chr(x) for x in response])

    return result[cmd_len + out_len:]


def write_page(spi, address, data):
    ready = False

    GPIO.output(GPIO_INT, GPIO.LOW)

    status = get_status(spi)
    if status & 1:
        ready = True

    while not ready:
        # wait 3.5ms
        time.sleep(3.5 / 1000.0)

        status = get_status(spi)

        if status & 1 and not status & 0x80:
            ready = True
        else:
            print 'waiting for card ready...'

    clear_status(spi)

    GPIO.output(GPIO_INT, GPIO.HIGH)

    write_cmd = [0xf2]
    write_cmd.extend(addr_to_bytes(address))

    if len(data) > WRITE_SZ:
        raise Exception('max write size is 0x%02x' % (WRITE_SZ))

    write_cmd.extend([ord(d) for d in data])

    spi.xfer2(write_cmd)

    # wait 3.5ms
    time.sleep(3.5 / 1000.0)

    return None


def erase_sector(spi, address):
    erase_cmd = [0xf1]

    # upper two bytes of address indicate sector
    up_addr = addr_to_bytes(address)[:2]
    erase_cmd.extend(up_addr)

    #finished = False
    #last_status = None
    #while not finished:
    #    # wait 1.6ms
    #    time.sleep(1.62 / 1000.0)
    #
    #    status = get_status(spi)
    #
    #    if status & 2:
    #        print '0x2 set'
    #        finished = True
    #    elif (status >> 7) & 1:
    #        #if status != last_status:
    #        print 'erasing/busy?... (0x%02x)' % (status)
    #        finished = False
    #    elif status & 1:
    #        print 'card ready? (0x%02x)' % (status)
    #        finished = True
    #    #else:
    #    #    finished = True

    spi.xfer2(erase_cmd)

    # wait 1.9ms
    time.sleep(1.9 / 1000.0)

    return None


def clear_status(spi):
    spi.xfer2([0x89])
    time.sleep(3.5 / 1000.0)


def get_status(spi):
    cmd = [0x83, 0x00]

    cmd_len = len(cmd)
    out_len = cmd_len
    in_len = 1

    cmd.extend([0xFF for x in range(in_len)])

    response = spi.xfer2(cmd)

    return response[out_len:][0]


def set_interrupt(spi, enable=True):
    cmd = [0x81, 0x00, 0x00, 0x00]

    if enable:
        cmd[1] = 0x01

    spi.xfer2(cmd)
    time.sleep(3.5 / 1000.0)


def wake_up(spi):
    cmd = [0x87]
    spi.xfer2(cmd)
    time.sleep(3.5 / 1000.0)


def write_buffer(spi):
    cmd = [0x82]
    spi.xfer2(cmd)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-r', '--read', type=str)
    parser.add_argument('-w', '--write', type=str, nargs=2,
                        help='original_file updated_file - writes diffs')
    args = parser.parse_args()

    spi = spidev.SpiDev()
    spi.open(0, 0)
    spi.mode = 0b00
    spi.max_speed_hz = 1000000 * 12  # 12 MHz (12.5 MHz is average on console)
    spi.cshigh = False

    # GPIO setup
    GPIO.setmode(GPIO.BOARD)
    GPIO.setup(GPIO_INT, GPIO.OUT)
    GPIO.output(GPIO_INT, GPIO.LOW)

    # opening sequence?
    spi.xfer2([0x00, 0x00, 0xFF, 0xFF, 0x00, 0x00], 1000)

    GPIO.output(GPIO_INT, GPIO.HIGH)

    cleared_status = False
    while not cleared_status:
        print 'clearing status...'
        clear_status(spi)

        status = get_status(spi)
        print 'getting status: 0x%02x' % (status)
        print etao.get_bits(status)

        if status & 1:
            cleared_status = True

    print 'setting interrupt...'
    set_interrupt(spi)

    print 'unlock command?'
    read_page(spi, 0x7fec9, amount=29)

    print 'getting first page...'
    first_page = read_page(spi, 0, amount=38)
    print binascii.hexlify(first_page)

    header = GCMHeader()
    header.load_bytes(first_page)
    for k in vars(header):
        if k == 'serial':
            continue
        print k, vars(header)[k]

    print 'serial: %s' % (binascii.hexlify(header.serial))
    print 'size: %u Mb' % (header.sizeMb)

    if header.sizeMb > 128:
        print 'ERROR: maximum size is 128 Mb'
        GPIO.cleanup()
        return

    # total size of card in bytes
    # (16 blocks per Megabit, 0x2000 bytes per block)
    total_size = header.sizeMb * 0x10 * BLOCK_SZ

    if args.read is not None:
        output = open(args.read, 'wb')

        content = ''

        num_reads = total_size / READ_SZ
        if num_reads * READ_SZ < total_size:
            num_reads += 1
        # print '%u / %u = %u' % (total_size, READ_SZ, total_size / READ_SZ)
        # print 'total reads: %u' % (num_reads)
        for i in range(num_reads):
            start_addr = i * READ_SZ
            if start_addr + READ_SZ > total_size:
                read_amount = (total_size - start_addr) % READ_SZ
                # print 'final read size %u' % (read_amount)
            else:
                read_amount = READ_SZ
            # print 'read #%u @ 0x%08x' % (i, start_addr)
            get_page = read_page(spi, start_addr, amount=read_amount)
            # print binascii.hexlify(get_page)
            # print 'got %u bytes' % (len(get_page))
            content += get_page
            # print 'content length %u' % (len(content))

        output.write(content)
        output.close()
    elif args.write is not None:
        original = open(args.write[0], 'rb')
        inputf = open(args.write[1], 'rb')
        original_content = original.read()
        new_content = inputf.read()
        original.close()
        inputf.close()

        if len(original_content) != len(new_content) or len(new_content) != total_size:
            raise Exception('image size mismatch')

        diff_count = 0

        for i in range(total_size / BLOCK_SZ):
            pos = i * BLOCK_SZ
            original_sector = original_content[pos:pos + BLOCK_SZ]
            new_sector = new_content[pos:pos + BLOCK_SZ]

            print 'writing block %u of %u' % (i + 1, total_size / BLOCK_SZ)

            if original_sector != new_sector:
                diff_count += 1

                print 'erase sector @ 0x%04x' % (pos)
                erase_sector(spi, pos)

                for j in range(BLOCK_SZ / WRITE_SZ):
                    slice_pos = pos + (j * WRITE_SZ)
                    print 'write slice @ 0x%08x' % (slice_pos)
                    new_slice = new_sector[j * WRITE_SZ:(j + 1) * WRITE_SZ]
                    write_page(spi, slice_pos, new_slice)

            time.sleep(4.0 / 1000.0)
            GPIO.output(GPIO_INT, GPIO.LOW)
            get_status(spi)
            clear_status(spi)
            GPIO.output(GPIO_INT, GPIO.HIGH)
            time.sleep(14.0 / 1000.0)

        print 'updated %u blocks' % (diff_count)

    spi.close()
    GPIO.cleanup()


if __name__ == '__main__':
    main()
