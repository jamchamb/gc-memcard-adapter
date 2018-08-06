#!/usr/bin/env python
import argparse
import binascii


def get_data(line, field):
    mosi_pos = line.find(field)
    if mosi_pos != -1:
        hex_pos = line.find('0x', mosi_pos)
        if hex_pos != -1:
            data = binascii.unhexlify(line[hex_pos+2:hex_pos+4])
            return data

    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('csv', type=str)
    parser.add_argument('output', type=str)
    args = parser.parse_args()

    with open(args.csv, 'r') as csv_file:
        csv_content = [x.rstrip('\r\n') for x in csv_file.readlines()]
        mosi_file = open(args.output + '_mosi', 'wb')
        miso_file = open(args.output + '_miso', 'wb')

        for line in csv_content:
            mosi = get_data(line, 'MOSI')
            miso = get_data(line, 'MISO')

            if mosi is not None:
                mosi_file.write(mosi)
            else:
                print 'error getting mosi'

            if miso is not None:
                miso_file.write(miso)
            else:
                print 'error getting miso'

        mosi_file.close()
        miso_file.close()


if __name__ == '__main__':
    main()
