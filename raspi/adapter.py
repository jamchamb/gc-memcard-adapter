#!/usr/bin/env python
import argparse
import binascii
import etao
import spidev
import struct
import time

from card import GCMHeader

BLOCK_SZ = 0x2000
SECTOR_SZ = 0x800
WRITE_SZ = 0x80
TIMING_LEN = 128


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

    #print 'packed address 0x%08x: 0x%02x%02x%02x%02x' % (
    #    address, packed[0], packed[1], packed[2], packed[3])

    return packed


def read_page(spi, address, amount=0x200 - 5):
    read_cmd = [0x52]
    read_cmd.extend(addr_to_bytes(address))

    cmd_len = len(read_cmd)  # (5)
    out_len = cmd_len + TIMING_LEN
    in_len = amount

    if in_len > (0x200 - 5):
        raise Exception('max 0x200 - 5 bytes per read')

    # time buffer + response size buffer
    read_cmd.extend([0 for x in range(out_len + in_len)])

    response = spi.xfer2(read_cmd)

    result = ''.join([chr(x) for x in response])

    return result[out_len:]


def write_page(spi, address, data):
    write_cmd = [0xf2]
    write_cmd.extend(addr_to_bytes(address))

    if len(data) > WRITE_SZ:
        raise Exception('max write size is 0x%02x' % (WRITE_SZ))

    spi.xfer2(write_cmd)

    # wait 3.5ms
    time.sleep(3.5 / 1000.0)

    status = get_status(spi)
    clear_status(spi)

    return status


def erase_sector(spi, address):
    erase_cmd = [0xf1]

    # upper two bytes of address indicate sector
    up_addr = addr_to_bytes(address)[:2]
    erase_cmd.extend(up_addr)

    # wait 1.6ms
    time.sleep(1.62 / 1000.0)

    status = get_status(spi)
    clear_status(spi)

    return status


def clear_status(spi):
    spi.xfer2([0x89])


def get_status(spi):
    cmd = [0x83, 0x00]

    cmd_len = len(cmd)
    out_len = cmd_len
    in_len = 1

    cmd.extend([0xFF for x in range(in_len)])

    response = spi.xfer2(cmd)

    return response[out_len:][0]


def set_interrupt(spi):
    cmd = [0x81]
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
    spi.max_speed_hz = 1000000 * 31  # 31 megahertz

    print 'clearing status...'
    clear_status(spi)

    print 'getting status: 0x%02x' % (get_status(spi))

    print 'setting interrupt...'
    set_interrupt(spi)

    print 'getting first page...'
    first_page = read_page(spi, 0)

    header = GCMHeader()
    header.load_bytes(first_page)

    print 'serial (raw): %s' % (binascii.hexlify(header.serial))
    print 'size: %u Mb' % (header.sizeMb)

    # total size of card in bytes
    # (16 blocks per Megabit, 0x2000 bytes per block)
    total_size = header.sizeMb * 0x10 * BLOCK_SZ

    print binascii.hexlify(first_page)

    if args.read is not None:
        output = open(args.read, 'wb')

        content = ''

        for i in range(total_size / 0x200):
            start_addr = i * 0x200
            # print 'read #%u @ 0x%08x' % (i, start_addr)
            get_page = read_page(spi, start_addr)
            # print binascii.hexlify(get_page)
            content += get_page

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

            if original_sector != new_sector:
                diff_count += 1

                #print 'erase sector @ 0x%04x' % (pos)
                erase_sector(spi, pos)

                for j in range(BLOCK_SZ / WRITE_SZ):
                    slice_pos = pos + (j * WRITE_SZ)
                    #print 'write slice @ 0x%08x' % (slice_pos)
                    new_slice = new_sector[j * WRITE_SZ:(j + 1) * WRITE_SZ]
                    write_page(spi, slice_pos, new_slice)

        print 'updated %u blocks' % (diff_count)


if __name__ == '__main__':
    main()
