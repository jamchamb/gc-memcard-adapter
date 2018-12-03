# gc-memcard-adapter

Code for reading and writing GameCube memory cards with a Raspberry Pi.

See https://jamchamb.github.io/2018/12/03/gamecube-memory-card-raspi.html

## Usage

To read entire memory card contents to file:

`$ ./adapter.py -r original_dump.bin`

You can use this file with a memory card manager program such as the
Dolphin emulator's memory card manager. To write the updated file back
to the memory card, provide the original file as well as the modified one
(this prevents unneccessary writes to blocks that haven't changed):

`$ ./adapter.py -w original_dump.bin updated_dump.bin`

## Connection

The standard Raspberry Pi SPI pins should be connected to the corresponding
pins on the memory card, and GPIO pin 7 is used for the INT pin on the memory card.

