#!/usr/bin/env python3
import argparse
import contextlib
import ctypes
import enum
import os
import select
import time
from gpiochip2 import GPIOChip, GPIO_V2_LINE_FLAG
from spidev2 import SPIBus, SPITransferList, SPIMode32
from tqdm import tqdm

READ_SZ = 0x200
WRITE_SZ = 0x80

class CardStatus(enum.IntFlag):
    """
    Gamecube card status flags
    """
    READY           = 1 << 0
    INT_EN          = 1 << 1
    #                = 1 << 2
    PROGRAMEERROR   = 1 << 3
    ERASEERROR      = 1 << 4
    SLEEP           = 1 << 5
    UNLOCKED        = 1 << 6
    BUSY            = 1 << 7

class GCMHeader(ctypes.BigEndianStructure):
    """
    Gamecube filesystem header block structure
    """
    _pack_ = 1
    _fields_ = (                            # http://www.gc-forever.com/yagcd/chap12.html#sec12
        ('serial', ctypes.c_uint8 * 12), # from card serial and time from PPC
        ('time', ctypes.c_uint64), # from PPC
        ('bias', ctypes.c_uint32), # from RTC
        ('lang', ctypes.c_uint32), # from RTC
        ('unk', ctypes.c_uint32),
        ('device_id', ctypes.c_uint16),
        ('size_megabits', ctypes.c_uint16),
        ('encoding', ctypes.c_uint16),
        ('padding', ctypes.c_uint8 * 0x1d6), # 0xff
        ('checksum1', ctypes.c_uint16), # Sum of all 16bits words above
        ('checksum2', ctypes.c_uint16), # Sum of the one's complement of all 16bits words above
    )

    def __str__(self):
        return (
            'serial:    %s (decoded: %s)\n'
            'time:      %016x\n'
            'bias:      %i\n'
            'lang:      %i\n'
            'device ID: %i\n'
            'size:      %i Mb\n'
            'encoding:  %i' % (
                bytes(self.serial).hex(),
                self.getDecodedSerial().hex(),
                self.time,
                self.bias,
                self.lang,
                self.device_id,
                self.size_megabits,
                self.encoding,
            ))

    def _iterSerialKey(self):
        key_value = self.time
        while True:
            key_value = ((key_value * 0x41c64e6d + 0x3039) >> 16)
            yield key_value
            key_value = ((key_value * 0x41c64e6d + 0x3039) >> 16) & 0x7fff

    def getDecodedSerial(self):
        """
        Decode serial number.
        """
        return bytes(
            (byte - key_value) & 0xff
            for byte, key_value in zip(self.serial, self._iterSerialKey())
        )

    def setEncodedSerial(self, serial, time):
        """
        Encode and set serial number with given timestamp.
        Also sets the timestamp.

        serial (12 bytes)
            The card's serial, as retrieved when unlocking it.
        time (int [0..2**60[)
            Some representation of the current time, in the PPC
            "Time Base Register" format, a monotonically increasing value at
            some model-dependent frequency.
            For more, see __ppc_get_timebase(3).
        """
        if len(serial) != 12:
            raise ValueError
        self.time = time
        self.serial = self.serial.__class__(*(
            (byte + key_value) & 0xff
            for byte, key_value in zip(serial, self._iterSerialKey())
        ))

    def getChecksum(self):
        """
        Checksum the current state of this instance.
        Returns the computed value of checksum1 and checksum2.
        """
        return _checksum(
            ctypes.cast(
                ctypes.pointer(self),
                ctypes.POINTER(
                    ctypes.c_uint8 *
                    self.__class__.checksum1.offset # XXX: is .offset portable ?
                ),
            ).contents,
        )

def _checksum(data):
    data_len = len(data)
    if data_len & 1:
        raise ValueError
    total = 0
    data_iterator = iter(data)
    while True:
        try:
            value = next(data_iterator) << 8
        except StopIteration:
            break
        total += value | next(data_iterator)
    checksum1 = total & 0xffff
    checksum2 = -(total + data_len // 2) & 0xffff
    return (
        (
            0
            if checksum1 == 0xffff else
            checksum1
        ),
        (
            0
            if checksum2 == 0xffff else
            checksum2
        ),
    )

class _HandshakeCipher:
    """
    This is a stream cipher based on a single 32bits LFSR with taps on bits
    8, 16, 24 and 31. Bit zero is set when the number of taps set is even.
    Output is bit 31 of the LFSR.
    """
    _tap_mask = 0x81010100

    def __init__(self, state):
        state       = ((state & 0xffff0000) >> 16) | ((state & 0x0000ffff) << 16)
        state       = ((state & 0xff00ff00) >>  8) | ((state & 0x00ff00ff) <<  8)
        state       = ((state & 0xf0f0f0f0) >>  4) | ((state & 0x0f0f0f0f) <<  4)
        state       = ((state & 0xcccccccc) >>  2) | ((state & 0x33333333) <<  2)
        self._state = ((state & 0xaaaaaaaa) >>  1) | ((state & 0x55555555) <<  1)

    def get_bits(self, count):
        result = 0
        state = self._state
        for _ in range(count):
            result <<= 1
            if state & 0x80000000:
                result |= 1
            if (state & self._tap_mask).bit_count() & 1 == 0:
                state |= 1
            state = (state & 0x7fffffff) << 1
        self._state = state
        return result

    def xor(self, value):
        value_len = len(value)
        return (
            self.get_bits(value_len << 3) ^ int.from_bytes(value, 'big')
        ).to_bytes(value_len, 'big')

_SECTOR_SIZE_LIST = (
    0x00002000,
    0x00004000,
    0x00008000,
    0x00010000,
    0x00020000,
    0x00040000,
)

_CARD_LATENCY_LIST = (
    0x00000004,
    0x00000008,
    0x00000010,
    0x00000020,
    0x00000040,
    0x00000080,
    0x00000100,
    0x00000200,
)

# ID:
#  3          2          1          0
# 10987654 32109876 54321098 76543210
# ........ ........ ........ xxxxxx.. card_size
# ........ ........ .....xxx ........ card_latency (index)
# ........ ........ ..xxx... ........ sector_size (index)
# ???????? ???????? ??...... ......??
ID_CARD_SIZE_SHIFT = 2
ID_CARD_SIZE_MASK = 0x3f
CARD_LATENCY_IDX_SHIFT = 8
CARD_LATENCY_IDX_MASK = 0x7
SECTOR_SIZE_IDX_SHIFT = 11
SECTOR_SIZE_IDX_MASK = 0x7

class GCM:
    """
    Gamecube memory card interface.
    """
    _flash_id = None

    def __init__(self, spi, gpio_int):
        """
        spi (SPIBus)
            The SPI device to which the memory card is attached.
        gpio_int (GPIOLines or None)
            The input GPIO pin to which the memory card's INT signal is
            connected. If None, the memory card will be polled to detect when
            it becomes idle.
        """
        self._spi = spi
        exi_id = int.from_bytes(self.get_exi_id(speed_hz=1_000_000), 'big')
        if not exi_id:
            raise ValueError('Nothing plugged ?')
        if exi_id & 0xffffc003:
            raise ValueError('Not a memory card ? id=%08x' % exi_id)
        self._card_size = (
            (exi_id >> ID_CARD_SIZE_SHIFT) & ID_CARD_SIZE_MASK
        ) << 19
        self._turnaround_bytes = b'\x00' * _CARD_LATENCY_LIST[
            (exi_id >> CARD_LATENCY_IDX_SHIFT) & CARD_LATENCY_IDX_MASK
        ]
        self._sector_size = _SECTOR_SIZE_LIST[
            (exi_id >> SECTOR_SIZE_IDX_SHIFT) & SECTOR_SIZE_IDX_MASK
        ]
        status = self.get_status()
        if status & CardStatus.SLEEP:
            self.wake_up()
            status = self.get_status()
        if gpio_int is None:
            if status & CardStatus.INT_EN:
                self.set_interrupt(enable=False)
            self._has_interrupt = False
        else:
            self._gpio_int = gpio_int
            gpio_int_fileno = gpio_int.fileno()
            os.set_blocking(gpio_int_fileno, False)
            self._int_epoll = int_epoll = select.epoll()
            int_epoll.register(gpio_int_fileno, select.EPOLLIN)
            self._has_interrupt = has_interrupt = bool(status & CardStatus.INT_EN)
            if not has_interrupt:
                self.set_interrupt(enable=True)
                status = self.get_status()
                self._has_interrupt = has_interrupt = bool(status & CardStatus.INT_EN)
        if status & CardStatus.UNLOCKED == 0:
            self._flash_id = self._unlock()

    def __len__(self):
        """
        Returns the number of bytes in this memory card.
        """
        return self._card_size

    @property
    def turnaround_bytes(self):
        """
        Returns the number of bytes which must be discarded between a read
        request and the actually read data.

        This is already taken into account by all methods reading the flash,
        and is only exposed for informational purposes.
        """
        return len(self._turnaround_bytes)

    @property
    def sector_size(self):
        """
        Returns the erase sector size of the flash chip inside the memory card.
        """
        return self._sector_size

    @property
    def has_interrupt(self):
        """
        Returns whether interrutps could be enabled on this card.

        Requires a card supporting interrupt signaling, and the INT signal to
        be made available via a GPIO pin.
        """
        return self._has_interrupt

    @property
    def flash_id(self):
        """
        Return the flash identifier, obtained when the memory card was unlocked,
        or None if the memory card was found already unlocked.
        """
        # XXX: is there a way to re-lock the card, or otherwise retrieve the
        # identifier later ?
        return self._flash_id

    def _waitForIdle(self, timeout=1):
        """
        Wait for the card to signal that it is idle.

        If interrupts are enabled, sleeps until the INT signal produces a
        falling edge.
        If interrutps are disabled, polls the card's status untol the BUSY flag
        is deasserted.
        """
        if self._has_interrupt:
            if self._int_epoll.poll(timeout=timeout):
                self._gpio_int.getEvent()
            else:
                raise TimeoutError
        else:
            for _ in range(timeout * 1000):
                if self.get_status() & CardStatus.BUSY == 0:
                    break
                time.sleep(0.001)
            else:
                raise TimeoutError

    def _unlock(self):
        """
        Do the magic unlocking dance.

        Returns the card id (12 bytes).
        """
        def _unlock_read(address_bytes):
            assert len(address_bytes) == 2
            self._spi.transfer(
                tx_buf=b'\x52' + (
                    handshake_cipher.xor(
                        address_bytes + b'\x00\x00' +
                        self._turnaround_bytes +
                        b'\x00\x00\x00\x00'
                    )
                ),
            )

        array_addr = 0x7fec8000
        handshake_cipher = _HandshakeCipher(array_addr)
        handshake_cipher.xor(self._raw_read_page(
            address_bytes=self._addr_to_bytes((array_addr >> 12) & 0x7ffff),
            length=4,
        ))
        handshake_cipher.get_bits(1)
        data = handshake_cipher.xor(self._raw_read_page(
            address_bytes=b'\x00\x00\x00\x00',
            length=24,
        ))
        handshake_cipher.get_bits(1)
        card_id = data[:12]
        challenge = data[12:20]

        challenge_sum = sum(challenge)
        running_sum = challenge_sum + 0x170a7489
        challenge_hash = 0x05efe0aa
        key0 = 0xdaf4b157
        key1 = 0x6bbec3b6
        def iter_nibble(value):
            for byte in value:
                yield (byte >> 4, byte & 0xf)
        def rr32(value, shift):
            # Only fractional rotations count, mask multiple-of-32 ones
            shift &= 0x1f
            return (
                ((value & 0xffffffff) >> shift) |
                (value << (32 - shift)) & 0xffffffff
            )
        nibble_iterator = iter_nibble(challenge)
        nibble0, nibble1 = next(nibble_iterator)
        for swap_offset in range(challenge_sum + 9, challenge_sum + 16):
            nibble2, nibble3 = next(nibble_iterator)
            running_sum = (
                running_sum + (
                    (
                        (
                            0xff00
                            if nibble3 & 0x8 else
                            0
                        ) | (
                            (nibble3 << 4) | nibble1
                        )
                    ) ^ (nibble0 << 8) ^ (nibble2 << 12)
                )
            ) & 0xffffffff
            challenge_hash = (
                challenge_hash +
                rr32(
                    value=((key0 ^ key1) + running_sum),
                    shift=swap_offset,
                )
            ) & 0xffffffff
            key0 = (
                running_sum ^ 0xffffffff
            ) & challenge_hash | (key1 >> 16) | (
                running_sum & key1 & 0xffff0000
            )
            key1 = running_sum ^ challenge_hash ^ key0
            nibble0 = nibble2
            nibble1 = nibble3
        response = challenge_hash.to_bytes(4, 'big')

        _unlock_read(address_bytes=response[:2])
        handshake_cipher.get_bits(1)
        _unlock_read(address_bytes=response[2:])
        if self.get_status() & CardStatus.UNLOCKED == 0:
            raise ValueError('Unlock failed, power-cycle card before trying again')
        return card_id

    def get_header(self):
        """
        Shorthand method to read the card's header.

        Returns a GCMHeader.
        """
        return GCMHeader.from_buffer_copy(
            self.read_page(0, length=ctypes.sizeof(GCMHeader)),
        )

    @staticmethod
    def _addr_to_bytes(address):
        """
        Convert an address into 4 address bytes.
        """
        return bytes((
            (address >> 17) & 0x7f,
            (address >>  9) & 0xff,
            (address >>  7) & 0x03,
            (address      ) & 0x7f,
        ))

    def _sendDuplexCommand(self, command, response_length, speed_hz=0):
        """
        Low-level SPI duplex transfer helper method.
        """
        result = bytearray(response_length)
        self._spi.submitTransferList(
            transfer_list=SPITransferList(
                kw_list=(
                    {
                        'tx_buf': command,
                        'speed_hz': speed_hz,
                    },
                    {
                        'rx_buf': result,
                        'speed_hz': speed_hz,
                    },
                ),
            ),
        )
        return result

    def get_exi_id(self, speed_hz=0):
        """
        Read the card's caracteristics:
        - size
        - number of tunraround bytes
        - erase block size

        Returns 4 bytes.
        """
        return self._sendDuplexCommand(
            command=b'\x00\x00',
            response_length=4,
            speed_hz=speed_hz,
        )

    def _raw_read_page(self, address_bytes, length, suffix_length=0):
        """
        Lowl-level page read method.
        Allows full control of the address bytes.
        Allows controlling a discarded read suffix.

        Returns length bytes.
        """
        result = bytearray(length)
        transfer_kw_list = [
            {
                'tx_buf': b'\x52' + address_bytes,
            },
            {
                'tx_buf': self._turnaround_bytes,
            },
            {
                'rx_buf': result,
            },
        ]
        if suffix_length:
            transfer_kw_list.append({
                'tx_buf': b'\x00' * suffix_length,
            })
        self._spi.submitTransferList(
            transfer_list=SPITransferList(
                kw_list=transfer_kw_list,
            ),
        )
        return result

    def read_page(self, address, length):
        """
        Read (up to) READ_SZ bytes from the memory card.

        address (int)
            Must be an integer multiple of READ_SZ.
        length (int)
            How many bytes to read, from 1 to READ_SZ (included).

        Returns length bytes.
        """
        if address % READ_SZ:
            raise ValueError(address % READ_SZ)
        if length > READ_SZ:
            raise ValueError(length)
        return self._raw_read_page(
            address_bytes=self._addr_to_bytes(address),
            length=length,
        )

    def set_interrupt(self, enable):
        """
        Control interrupt signal generation by the memory card.
        """
        self._spi.transfer(
            tx_buf=(
                b'\x81\x01\x00\x00'
                if enable else
                b'\x81\x00\x00\x00'
            ),
        )

    def write_buffer(self):
        """
        Flush some internal buffer ?
        """
        self._spi.transfer(
            tx_buf=b'\x82',
        )
        self._waitForIdle()

    def get_status(self):
        """
        Retrieve the current memory card's status.

        Returns a CardStatus.
        """
        return CardStatus(
            self._sendDuplexCommand(
                command=b'\x83\x00',
                response_length=1,
            )[0],
        )

    def get_id(self):
        """
        Some maker or model identifier ?

        Returns 2 bytes.
        """
        return self._sendDuplexCommand(
            command=b'\x85\x00',
            response_length=2,
        )

    def wake_up(self):
        """
        Wake memory card up from sleep.
        """
        self._spi.transfer(
            tx_buf=b'\x87',
        )

    def sleep(self):
        """
        Puts the memory card to sleep.
        """
        # XXX: what does this actually changes ?
        self._spi.transfer(
            tx_buf=b'\x88',
        )

    def clear_status(self):
        """
        Clears PROGRAMEERROR and ERASEERROR.
        """
        self._spi.transfer(
            tx_buf=b'\x89',
        )

    def erase_sector(self, address):
        """
        Erase a sector of the memory card.

        address (int)
            Must be an integer multiple of sector_size .
        """
        if address % self._sector_size:
            raise ValueError(address % self._sector_size)
        self.clear_status()
        self._spi.transfer(
            tx_buf=b'\xf1' + self._addr_to_bytes(address)[:2],
        )
        self._waitForIdle()
        if self.get_status() & CardStatus.ERASEERROR:
            raise ValueError('Erase failed')

    def write_page(self, address, data):
        """
        Write (up to) a page of the memory card.
        Only zeroes are written to the card, so usually this is preceded by an
        erase_sector call which sets all bytes to 0xff.

        address (int)
            Must be an integer multiple of WRITE_SZ.
        data (bytes)
            Must have a length less than or equal to WRITE_SZ.
        """
        if len(data) > WRITE_SZ:
            raise ValueError(len(data))
        if address % WRITE_SZ:
            raise ValueError(address % WRITE_SZ)
        self.clear_status()
        self._spi.transfer(
            tx_buf=b'\xf2' + self._addr_to_bytes(address) + data,
        )
        self._waitForIdle()
        if self.get_status() & CardStatus.PROGRAMEERROR:
            raise ValueError('Erase failed')

    def erase_card(self):
        """
        Erases the entire card.
        """
        self.clear_status()
        self._spi.transfer(
            tx_buf=b'\xf4\x00\x00',
        )
        self._waitForIdle()
        if self.get_status() & CardStatus.ERASEERROR:
            raise ValueError('Erase failed')

@contextlib.contextmanager
def _optionalGPIO(gpiochip, gpio_int_line):
    if gpiochip is None or gpio_int_line is None:
        yield None
    else:
        with GPIOChip(gpiochip, 'w+b') as gpiochip_file:
            with gpiochip_file.openLines(
                line_list=[gpio_int_line],
                consumer='gc-memcard'.encode('ascii'),
                flags=(
                    GPIO_V2_LINE_FLAG.INPUT |
                    GPIO_V2_LINE_FLAG.EDGE_FALLING
                ),
            ) as gpio_pin_file:
                yield gpio_pin_file

def main():
    parser = argparse.ArgumentParser(
        description='GC memory card reader/writer',
    )
    parser.add_argument(
        '--spi',
        required=True,
        help='SPI device the memory card is connected to.'
    )
    parser.add_argument(
        '--gpiochip',
        help='GPIO device the memory card INT line is connected to. '
        'If not provided, (slightly) slower status-polling will be used.',
    )
    parser.add_argument(
        '--gpio-int-line',
        type=int,
        help='GPIO line the card INT line is connected to. '
        'If not provided, (slightly) slower status-polling will be used.',
    )
    action_parser = parser.add_mutually_exclusive_group()
    action_parser.add_argument(
        '-r', '--read',
        help='Read entire card and save to given file.',
    )
    action_parser.add_argument(
        '-w', '--write',
        nargs=2,
        metavar=('OLD', 'NEW'),
        help='Writes any page from NEW which differs from OLD',
    )
    args = parser.parse_args()
    with \
        SPIBus(
            args.spi,
            'w+b',
            bits_per_word=8,
            speed_hz=16_000_000,
            spi_mode=(
                SPIMode32.SPI_MODE_0
                # CS active low
                # MSb first
                # not 3-wire
                # not loopback
                # has CS
                # no "ready" handshaking
                # no dual/quad/oct
                # no per-word CS toggle
                # no 3-wire hi-Z
            ),
        ) as spi,\
        _optionalGPIO(
            gpiochip=args.gpiochip,
            gpio_int_line=args.gpio_int_line,
        ) as gpio_int\
    :
        card = GCM(spi=spi, gpio_int=gpio_int)
        total_size = len(card)
        print('card size (B):   ', total_size)
        print('turnaround bytes:', card.turnaround_bytes)
        print('sector size:     ', card.sector_size)
        print('sector count:    ', total_size / card.sector_size)
        print('flash id:        ', (
            '(unknown)'
            if card.flash_id is None else
            card.flash_id.hex()
        ))
        print('id:              ', card.get_id().hex())
        status = card.get_status()
        print('status:          ', status)
        header = card.get_header()
        print('header:')
        print(' ', str(header).replace('\n', '\n  '))
        if card.flash_id is None:
            print('cannot check serial consistency')
        else:
            if card.flash_id == header.getDecodedSerial():
                print('header serial is consistent with card id')
            else:
                print('header serial is NOT consistent with card id')
        if header.getChecksum() == (header.checksum1, header.checksum2):
            print('header checksum consistent')
        else:
            print('header checksum NOT consistent')
        pos = 0
        if args.read:
            num_blocks, remainder = divmod(total_size, READ_SZ)
            assert remainder == 0, remainder
            with open(args.read, 'wb') as output:
                for _ in tqdm(range(num_blocks)):
                    read_amount = min(READ_SZ, total_size - pos)
                    output.write(card.read_page(
                        address=pos,
                        length=read_amount,
                    ))
                    pos += read_amount
        elif args.write:
            with \
                open(args.write[0], 'rb') as original, \
                open(args.write[1], 'rb') as inputf \
            :
                original_content = original.read(total_size + 1)
                new_content = inputf.read(total_size + 1)
            if (
                len(original_content) != len(new_content) or
                len(new_content) != total_size
            ):
                raise ValueError('image size mismatch')
            sector_size = card.sector_size
            num_blocks, remainder = divmod(total_size, sector_size)
            assert remainder == 0, remainder
            writes_per_block, remainder = divmod(sector_size, WRITE_SZ)
            assert remainder == 0, remainder
            diff_count = 0
            for _ in tqdm(range(num_blocks)):
                new_sector = new_content[pos:pos + sector_size]
                if original_content[pos:pos + sector_size] != new_sector:
                    diff_count += 1
                    card.erase_sector(pos)
                    slice_pos = 0
                    for _ in range(writes_per_block):
                        card.write_page(
                            address=pos + slice_pos,
                            data=new_sector[slice_pos:slice_pos + WRITE_SZ],
                        )
                        slice_pos += WRITE_SZ
                pos += sector_size
            print('updated %u blocks' % (diff_count, ))
        card.sleep()

if __name__ == '__main__':
    main()
