import struct
import threading
import time


MAGIC = b"ND"
VERSION = 1
HEADER = struct.Struct("<2sBBHH")
CRC = struct.Struct("<H")
INFO = struct.Struct("<HHHI")
PAGE_SIZE = 2048
OOB_SIZE = 64
PAGE_BYTES = PAGE_SIZE + OOB_SIZE
MAX_PAYLOAD = 4 + PAGE_BYTES

CMD_INFO = 0x01
CMD_READ_PAGE = 0x02
CMD_WRITE_PAGE = 0x03
CMD_ERASE_BLOCK = 0x04
CMD_CHECK_BLOCK = 0x05

STATUS_OK = 0
STATUS_NAMES = {
    1: "invalid request/address",
    2: "NAND ready timeout",
    3: "NAND program/erase failure",
    4: "invalid payload length",
    5: "request CRC mismatch",
    6: "unsupported protocol version",
}


class ProtocolError(IOError):
    pass


def crc16_ccitt(data):
    crc = 0xFFFF
    for value in data:
        crc ^= value << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def encode_frame(command, sequence, payload=b""):
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("Protocol payload exceeds maximum frame size")
    header = HEADER.pack(MAGIC, VERSION, command, sequence, len(payload))
    checksum = crc16_ccitt(header[2:] + payload)
    return header + payload + CRC.pack(checksum)


class NandProtocol:
    def __init__(self, serial_port, timeout=5.0):
        self.serial = serial_port
        self.timeout = timeout
        self.sequence = 0
        self._lock = threading.Lock()

    def _read_exact(self, length, deadline):
        result = bytearray()
        while len(result) < length:
            if time.monotonic() >= deadline:
                raise TimeoutError("Timed out waiting for NAND bridge response")
            chunk = self.serial.read(length - len(result))
            if chunk:
                result.extend(chunk)
        return bytes(result)

    def _read_response(self, expected_command, expected_sequence, deadline):
        sync = bytearray()
        while True:
            sync.extend(self._read_exact(1, deadline))
            if len(sync) > len(MAGIC):
                del sync[0]
            if bytes(sync) == MAGIC:
                break

        tail = self._read_exact(6, deadline)
        version, command, sequence, length = struct.unpack("<BBHH", tail)
        if version != VERSION:
            raise ProtocolError(f"Unsupported protocol version {version}")
        if command != (expected_command | 0x80) or sequence != expected_sequence:
            raise ProtocolError("Response command or sequence does not match request")
        if length < 1 or length > MAX_PAYLOAD + 1:
            raise ProtocolError(f"Invalid response payload length {length}")
        payload = self._read_exact(length, deadline)
        received_crc = CRC.unpack(self._read_exact(CRC.size, deadline))[0]
        crc_data = tail + payload
        if crc16_ccitt(crc_data) != received_crc:
            raise ProtocolError("Response CRC mismatch")
        status, body = payload[0], payload[1:]
        if status:
            reason = STATUS_NAMES.get(status, f"unknown status {status}")
            raise ProtocolError(f"NAND bridge error: {reason}")
        return body

    def request(self, command, payload=b""):
        with self._lock:
            sequence = self.sequence
            self.sequence = (self.sequence + 1) & 0xFFFF
            frame = encode_frame(command, sequence, payload)
            deadline = time.monotonic() + self.timeout
            written = self.serial.write(frame)
            if written != len(frame):
                raise ProtocolError(f"Short serial write: {written}/{len(frame)} bytes")
            return self._read_response(command, sequence, deadline)

    def get_info(self):
        body = self.request(CMD_INFO)
        if len(body) != INFO.size:
            raise ProtocolError(f"Invalid INFO response size: {len(body)}")
        page_size, oob_size, pages_per_block, total_blocks = INFO.unpack(body)
        return {
            "page_size": page_size,
            "oob_size": oob_size,
            "pages_per_block": pages_per_block,
            "total_blocks": total_blocks,
        }

    def read_page(self, page_id):
        body = self.request(CMD_READ_PAGE, struct.pack("<I", page_id))
        if len(body) != PAGE_BYTES:
            raise ProtocolError(f"Invalid page size returned: {len(body)}")
        return body

    def write_page(self, page_id, data):
        if len(data) != PAGE_BYTES:
            raise ValueError(f"Page data must be exactly {PAGE_BYTES} bytes")
        self.request(CMD_WRITE_PAGE, struct.pack("<I", page_id) + bytes(data))

    def erase_block(self, block_id):
        self.request(CMD_ERASE_BLOCK, struct.pack("<H", block_id))

    def check_block(self, block_id):
        body = self.request(CMD_CHECK_BLOCK, struct.pack("<H", block_id))
        if len(body) != 1 or body[0] not in (0, 1):
            raise ProtocolError("Invalid bad-block response")
        return body[0] == 1
