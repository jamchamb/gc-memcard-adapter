import binascii
import struct

class GCMHeader:

    def __init__(self):
        pass

    def load_bytes(self, buf):
        hdr_format = '>12sQIIIHHH'
        hdr_size = struct.calcsize(hdr_format)

        serial, time, bias, lang, unk1, deviceId, sizeMb, encoding = struct.unpack(hdr_format, buf[:hdr_size])

        self.serial = serial
        self.time = time
        self.bias = bias
        self.lang = lang
        self.deviceID = deviceId
        self.sizeMb = sizeMb
        self.encoding = encoding
