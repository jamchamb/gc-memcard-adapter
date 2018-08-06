#!/usr/bin/env python
import argparse
import struct
from binascii import hexlify


def block_count(data_size, block_size):
    """The number of blocks of given size required to
    hold the data."""

    blocks = 0
    while (block_size * blocks) < data_size:
        blocks += 1

    return blocks


def block_align(data_size, block_size):
    """Return size of buffer that is a multiple of the
    block size that can contain the data."""
    return block_count(data_size, block_size) * block_size


# Command len indata len outdata len Description
# 0x8500    2 -      -   ID      2   get ID
# 0x8300    2 -      -   Status  1   get Card Status
# 0x89      1 -      -   -       -   clear Card Status
# 0x52      1 offset, 0x00000000 8 Block Data <=0x200 read Block
# 0xf40000  3----erase Card
# 0xf1      1 Sector2--erase Sector
# 0xf2      1 Block offset4Block Data<=0x80write Block


class MemCmd:

    def __init__(self, name, code, inlen, outlen, desc):
        self.name = name
        self.code = code
        self.inlen = inlen
        self.outlen = outlen
        self.desc = desc


ID_CMD = MemCmd('get_id', '\x85\x00', 0, 2, 'get ID')
STATUS_CMD = MemCmd('get_status', '\x83\x00', 0, 1, 'get card status')
CLEAR_STATUS_CMD = MemCmd('clear_status', '\x89', 0, 0, 'clear card status')
READ_BLOCK_CMD = MemCmd('read_block', '\x52', 4, 0x200, 'read block')
ERASE_CARD_CMD = MemCmd('erase_card', '\xf4\x00\x00\x00',  0, 0, 'erase card')
ERASE_SECTOR_CMD = MemCmd('erase_sector', '\xf1', 2, 0, 'erase sector')
WRITE_BLOCK_CMD = MemCmd('write_block', '\xf2', 4, 0x80, 'write block')

COMMANDS = [
    ID_CMD,
    STATUS_CMD,
    CLEAR_STATUS_CMD,
    READ_BLOCK_CMD,
    ERASE_CARD_CMD,
    ERASE_SECTOR_CMD,
    WRITE_BLOCK_CMD
]

COMMAND_TABLE = {}
for command in COMMANDS:
    lookup_byte = command.code[:1]
    COMMAND_TABLE[lookup_byte] = command


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('miso', type=str)
    parser.add_argument('mosi', type=str)
    args = parser.parse_args()

    miso_stream = open(args.miso, 'rb').read()
    mosi_stream = open(args.mosi, 'rb').read()

    i = 0
    while True:
        if i > len(mosi_stream) - 1:
            break
        if i > len(miso_stream) - 1:
            break

        byte = mosi_stream[i]

        if byte not in COMMAND_TABLE:
            if byte != '\x00' and byte != '\xff':
                print 'position: 0x%04x' % (i)
                print 'MOSI: unrecognized 0x%x' % (ord(byte))
            else:
                print '.',
            i += 1
            continue

        print 'position: 0x%04x' % (i)

        cmd = COMMAND_TABLE[byte]

        print 'MOSI: %s' % (cmd.desc)

        outlen = cmd.outlen

        i += len(cmd.code)

        if cmd.inlen > 0:
            start = i
            end = start + cmd.inlen
            request = mosi_stream[start:end]
            print '\t%s' % (hexlify(request))

            if cmd.name == 'read_block':
                # block, offset = struct.unpack('>HH', request)
                # print '\tblock: %u, offset: %u' % (block, offset)

                y1, y2, y3, y4 = struct.unpack('>BBBB', request)
                offset = (y1 << 17) + (y2 << 9) + ((y3 << 7) & 3) + (y4 & 0x7F)
                print '\toffset: %u' % (offset)
                aligned = block_align(offset, 512)
                print '\talign: %u (%u)' % (aligned, aligned - offset)

                align_diff = aligned - offset

                outlen = 0x200
            elif cmd.name == 'write_block':
                print '\tdata: %s' % (hexlify(mosi_stream[end:end+0x80]))

            i += cmd.inlen

        if cmd.outlen > 0:
            start = i
            end = start + outlen
            response = miso_stream[start:end]
            print 'MISO: %s' % (hexlify(response))

            i += outlen


if __name__ == '__main__':
    main()
