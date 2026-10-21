import sys
import time
from machine import Pin


PAGE_SIZE = 2048
OOB_SIZE = 64
PAGE_TOTAL_SIZE = PAGE_SIZE + OOB_SIZE
PAGES_PER_BLOCK = 64
TOTAL_BLOCKS = 1024
ROW_ADDRESS_CYCLES = 2
NAND_READY_TIMEOUT_MS = 500

PIN_DATA_BASE = 2
PIN_CLE = 10
PIN_ALE = 11
PIN_WE = 12
PIN_RE = 13
PIN_CE = 14
PIN_RB = 15

PROTOCOL_MAGIC = b"ND"
PROTOCOL_VERSION = 1
CMD_INFO = 0x01
CMD_READ_PAGE = 0x02
CMD_WRITE_PAGE = 0x03
CMD_ERASE_BLOCK = 0x04
CMD_CHECK_BLOCK = 0x05

STATUS_OK = 0
STATUS_INVALID = 1
STATUS_TIMEOUT = 2
STATUS_NAND_FAIL = 3
STATUS_BAD_LENGTH = 4
STATUS_BAD_CRC = 5
STATUS_BAD_VERSION = 6
MAX_FRAME_PAYLOAD = 4 + PAGE_TOTAL_SIZE


class FrameError(Exception):
    def __init__(self, command, sequence, status):
        self.command = command
        self.sequence = sequence
        self.status = status


def crc16_ccitt(data):
    crc = 0xFFFF
    for value in data:
        crc ^= value << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def read_exact(stream, length):
    if hasattr(stream, "readinto"):
        result = bytearray(length)
        offset = 0
        while offset < length:
            count = stream.readinto(memoryview(result)[offset:])
            if count:
                offset += count
        return bytes(result)
    result = bytearray()
    while len(result) < length:
        chunk = stream.read(length - len(result))
        if chunk:
            if isinstance(chunk, str):
                chunk = chunk.encode("latin-1")
            result.extend(chunk)
    return bytes(result)


def read_request(stream):
    sync = bytearray()
    while True:
        sync.extend(read_exact(stream, 1))
        if len(sync) > len(PROTOCOL_MAGIC):
            del sync[0]
        if bytes(sync) == PROTOCOL_MAGIC:
            break

    header_tail = read_exact(stream, 6)
    version = header_tail[0]
    command = header_tail[1]
    sequence = header_tail[2] | (header_tail[3] << 8)
    payload_length = header_tail[4] | (header_tail[5] << 8)
    if payload_length > MAX_FRAME_PAYLOAD:
        raise FrameError(command, sequence, STATUS_BAD_LENGTH)
    payload = read_exact(stream, payload_length)
    received_crc = int.from_bytes(read_exact(stream, 2), "little")
    crc_data = header_tail + payload
    if crc16_ccitt(crc_data) != received_crc:
        raise FrameError(command, sequence, STATUS_BAD_CRC)
    if version != PROTOCOL_VERSION:
        raise FrameError(command, sequence, STATUS_BAD_VERSION)
    return command, sequence, payload


def write_response(stream, command, sequence, status, payload=b""):
    response_payload = bytes((status,)) + payload
    header = (
        PROTOCOL_MAGIC
        + bytes((PROTOCOL_VERSION, command | 0x80))
        + sequence.to_bytes(2, "little")
        + len(response_payload).to_bytes(2, "little")
    )
    crc = crc16_ccitt(header[2:] + response_payload)
    frame = header + response_payload + crc.to_bytes(2, "little")
    try:
        stream.write(frame)
    except TypeError:
        stream.write(frame.decode("latin-1"))
    stream.flush()


class NandError(Exception):
    def __init__(self, status):
        self.status = status
        super().__init__("NAND operation failed with status %d" % status)


class TC58Controller:
    def __init__(self):
        self.data_pins = [
            Pin(PIN_DATA_BASE + index, Pin.OUT, value=0) for index in range(8)
        ]
        self.cle = Pin(PIN_CLE, Pin.OUT, value=0)
        self.ale = Pin(PIN_ALE, Pin.OUT, value=0)
        self.we = Pin(PIN_WE, Pin.OUT, value=1)
        self.re = Pin(PIN_RE, Pin.OUT, value=1)
        self.ce = Pin(PIN_CE, Pin.OUT, value=1)
        self.rb = Pin(PIN_RB, Pin.IN)

    def _set_bus_direction(self, output):
        mode = Pin.OUT if output else Pin.IN
        for pin in self.data_pins:
            pin.init(mode)

    def _write_cycle(self, value, command=False, address=False):
        self.cle.value(1 if command else 0)
        self.ale.value(1 if address else 0)
        for bit, pin in enumerate(self.data_pins):
            pin.value((value >> bit) & 1)
        time.sleep_us(1)
        self.we.value(0)
        time.sleep_us(1)
        self.we.value(1)
        time.sleep_us(1)
        self.cle.value(0)
        self.ale.value(0)

    def _read_cycle(self):
        self.re.value(0)
        time.sleep_us(1)
        value = 0
        for bit, pin in enumerate(self.data_pins):
            value |= pin.value() << bit
        self.re.value(1)
        time.sleep_us(1)
        return value

    def _deselect(self):
        self.cle.value(0)
        self.ale.value(0)
        self.we.value(1)
        self.re.value(1)
        self.ce.value(1)

    def _validate_page_id(self, page_id):
        if page_id < 0 or page_id >= TOTAL_BLOCKS * PAGES_PER_BLOCK:
            raise ValueError("page address is out of range")

    def _address(self, page_id, column=0):
        self._validate_page_id(page_id)
        self._set_bus_direction(True)
        self._write_cycle(column & 0xFF, address=True)
        self._write_cycle((column >> 8) & 0xFF, address=True)
        for index in range(ROW_ADDRESS_CYCLES):
            self._write_cycle((page_id >> (8 * index)) & 0xFF, address=True)

    def _wait_ready(self):
        started = time.ticks_ms()
        while self.rb.value() == 0:
            if time.ticks_diff(time.ticks_ms(), started) >= NAND_READY_TIMEOUT_MS:
                raise NandError(STATUS_TIMEOUT)
            time.sleep_ms(1)

    def _status(self):
        self.ce.value(0)
        try:
            self._write_cycle(0x70, command=True)
            self._set_bus_direction(False)
            return self._read_cycle()
        finally:
            self._deselect()

    def read_page(self, page_id):
        self._validate_page_id(page_id)
        self.ce.value(0)
        try:
            self._write_cycle(0x00, command=True)
            self._address(page_id)
            self._write_cycle(0x30, command=True)
            self._wait_ready()
            self._set_bus_direction(False)
            data = bytearray(PAGE_TOTAL_SIZE)
            for index in range(PAGE_TOTAL_SIZE):
                data[index] = self._read_cycle()
            return bytes(data)
        finally:
            self._deselect()

    def read_oob_marker(self, page_id):
        self._validate_page_id(page_id)
        self.ce.value(0)
        try:
            self._write_cycle(0x00, command=True)
            self._address(page_id, PAGE_SIZE)
            self._write_cycle(0x30, command=True)
            self._wait_ready()
            self._set_bus_direction(False)
            return self._read_cycle()
        finally:
            self._deselect()

    def write_page(self, page_id, data):
        if len(data) != PAGE_TOTAL_SIZE:
            raise NandError(STATUS_BAD_LENGTH)
        self._validate_page_id(page_id)
        self.ce.value(0)
        try:
            self._write_cycle(0x80, command=True)
            self._address(page_id)
            self._set_bus_direction(True)
            for value in data:
                self._write_cycle(value)
            self._write_cycle(0x10, command=True)
            self.ce.value(1)
            self._wait_ready()
            status = self._status()
            if not status & 0x40:
                raise NandError(STATUS_TIMEOUT)
            if status & 0x01:
                raise NandError(STATUS_NAND_FAIL)
        finally:
            self._deselect()

    def erase_block(self, block_id):
        if block_id < 0 or block_id >= TOTAL_BLOCKS:
            raise ValueError("block address is out of range")
        first_page = block_id * PAGES_PER_BLOCK
        self.ce.value(0)
        try:
            self._write_cycle(0x60, command=True)
            self._set_bus_direction(True)
            for index in range(ROW_ADDRESS_CYCLES):
                self._write_cycle((first_page >> (8 * index)) & 0xFF, address=True)
            self._write_cycle(0xD0, command=True)
            self.ce.value(1)
            self._wait_ready()
            status = self._status()
            if not status & 0x40:
                raise NandError(STATUS_TIMEOUT)
            if status & 0x01:
                raise NandError(STATUS_NAND_FAIL)
        finally:
            self._deselect()

    def check_block(self, block_id):
        if block_id < 0 or block_id >= TOTAL_BLOCKS:
            raise ValueError("block address is out of range")
        for page_offset in (0, 1):
            page_id = block_id * PAGES_PER_BLOCK + page_offset
            if self.read_oob_marker(page_id) != 0xFF:
                return False
        return True


def dispatch(nand, command, payload):
    if command == CMD_INFO:
        if payload:
            raise NandError(STATUS_BAD_LENGTH)
        info = (
            PAGE_SIZE.to_bytes(2, "little")
            + OOB_SIZE.to_bytes(2, "little")
            + PAGES_PER_BLOCK.to_bytes(2, "little")
            + TOTAL_BLOCKS.to_bytes(4, "little")
        )
        return info

    if command == CMD_READ_PAGE:
        if len(payload) != 4:
            raise NandError(STATUS_BAD_LENGTH)
        return nand.read_page(int.from_bytes(payload, "little"))

    if command == CMD_WRITE_PAGE:
        if len(payload) != 4 + PAGE_TOTAL_SIZE:
            raise NandError(STATUS_BAD_LENGTH)
        page_id = int.from_bytes(payload[:4], "little")
        nand.write_page(page_id, payload[4:])
        return b""

    if command == CMD_ERASE_BLOCK:
        if len(payload) != 2:
            raise NandError(STATUS_BAD_LENGTH)
        nand.erase_block(int.from_bytes(payload, "little"))
        return b""

    if command == CMD_CHECK_BLOCK:
        if len(payload) != 2:
            raise NandError(STATUS_BAD_LENGTH)
        return bytes((1 if nand.check_block(int.from_bytes(payload, "little")) else 0,))

    raise NandError(STATUS_INVALID)


def main():
    nand = TC58Controller()
    input_stream = getattr(sys.stdin, "buffer", sys.stdin)
    output_stream = getattr(sys.stdout, "buffer", sys.stdout)
    while True:
        try:
            command, sequence, payload = read_request(input_stream)
        except FrameError as exc:
            write_response(
                output_stream, exc.command, exc.sequence, exc.status
            )
            continue
        except (ValueError, OverflowError):
            continue
        try:
            response = dispatch(nand, command, payload)
        except NandError as exc:
            write_response(output_stream, command, sequence, exc.status)
        except ValueError:
            write_response(output_stream, command, sequence, STATUS_INVALID)
        except Exception:
            nand._deselect()
            write_response(output_stream, command, sequence, STATUS_INVALID)
        else:
            write_response(output_stream, command, sequence, STATUS_OK, response)


if __name__ == "__main__":
    main()
