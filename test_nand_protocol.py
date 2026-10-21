import importlib.util
import io
import os
import struct
import sys
import types
import unittest
from unittest.mock import patch

from nand_protocol import (
    CMD_INFO,
    CMD_READ_PAGE,
    CMD_WRITE_PAGE,
    CMD_ERASE_BLOCK,
    CMD_CHECK_BLOCK,
    INFO,
    MAGIC,
    PAGE_BYTES,
    VERSION,
    NandProtocol,
    ProtocolError,
    crc16_ccitt,
    encode_frame,
)


def decode_request(frame):
    magic, version, command, sequence, length = struct.unpack("<2sBBHH", frame[:8])
    payload = frame[8:8 + length]
    checksum = struct.unpack("<H", frame[8 + length:10 + length])[0]
    if magic != MAGIC or version != VERSION:
        raise AssertionError("invalid frame prefix")
    if crc16_ccitt(frame[2:8] + payload) != checksum:
        raise AssertionError("invalid request CRC")
    return command, sequence, payload


def make_response(command, sequence, payload):
    header_tail = struct.pack("<BBHH", VERSION, command | 0x80, sequence, len(payload))
    checksum = crc16_ccitt(header_tail + payload)
    return MAGIC + header_tail + payload + struct.pack("<H", checksum)


class FakeSerial:
    def __init__(self, responder):
        self.responder = responder
        self.incoming = bytearray()

    def write(self, data):
        command, sequence, payload = decode_request(data)
        self.incoming.extend(self.responder(command, sequence, payload))
        return len(data)

    def read(self, length):
        data = bytes(self.incoming[:length])
        del self.incoming[:length]
        return data


class NandProtocolTests(unittest.TestCase):
    def test_frame_crc_known_vector(self):
        self.assertEqual(crc16_ccitt(b"123456789"), 0x29B1)

    def test_info_and_read_responses(self):
        page = bytes((index & 0xFF for index in range(PAGE_BYTES)))

        def respond(command, sequence, payload):
            if command == CMD_INFO:
                body = INFO.pack(2048, 64, 64, 1024)
            elif command == CMD_READ_PAGE:
                self.assertEqual(payload, struct.pack("<I", 7))
                body = page
            else:
                self.fail(f"unexpected command {command}")
            return make_response(command, sequence, b"\x00" + body)

        client = NandProtocol(FakeSerial(respond), timeout=0.5)
        self.assertEqual(
            client.get_info(),
            {
                "page_size": 2048,
                "oob_size": 64,
                "pages_per_block": 64,
                "total_blocks": 1024,
            },
        )
        self.assertEqual(client.read_page(7), page)

    def test_device_error_is_reported(self):
        serial = FakeSerial(
            lambda command, sequence, payload: make_response(
                command, sequence, b"\x03"
            )
        )
        with self.assertRaisesRegex(ProtocolError, "program/erase failure"):
            NandProtocol(serial, timeout=0.5).request(CMD_INFO)

    def test_write_erase_and_bad_block_payloads(self):
        page = b"\xA5" * PAGE_BYTES
        requests = []

        def respond(command, sequence, payload):
            requests.append((command, payload))
            response_body = b"\x01" if command == CMD_CHECK_BLOCK else b""
            return make_response(command, sequence, b"\x00" + response_body)

        client = NandProtocol(FakeSerial(respond), timeout=0.5)
        client.write_page(0x12345, page)
        client.erase_block(0x321)
        self.assertTrue(client.check_block(0x123))
        self.assertEqual(
            requests,
            [
                (CMD_WRITE_PAGE, struct.pack("<I", 0x12345) + page),
                (CMD_ERASE_BLOCK, struct.pack("<H", 0x321)),
                (CMD_CHECK_BLOCK, struct.pack("<H", 0x123)),
            ],
        )

    def test_encode_rejects_oversized_payload(self):
        with self.assertRaises(ValueError):
            encode_frame(CMD_READ_PAGE, 0, b"x" * (PAGE_BYTES + 5))

    def test_firmware_and_host_frame_compatibility(self):
        class FakePin:
            OUT = 1
            IN = 0

        machine_stub = types.ModuleType("machine")
        machine_stub.Pin = FakePin
        firmware_path = os.path.join(os.path.dirname(__file__), "rp2040 nand.py")
        spec = importlib.util.spec_from_file_location("nand_firmware", firmware_path)
        firmware = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"machine": machine_stub}):
            spec.loader.exec_module(firmware)

        request = encode_frame(CMD_READ_PAGE, 0x1234, struct.pack("<I", 19))
        self.assertEqual(
            firmware.read_request(io.BytesIO(request)),
            (CMD_READ_PAGE, 0x1234, struct.pack("<I", 19)),
        )

        output = io.BytesIO()
        firmware.write_response(output, CMD_READ_PAGE, 0x1234, 0, b"page")
        client = NandProtocol(FakeSerial(lambda *_: output.getvalue()), timeout=0.5)
        client.sequence = 0x1234
        self.assertEqual(
            client.request(CMD_READ_PAGE),
            b"page",
        )

    def test_firmware_reports_bad_request_crc(self):
        class FakePin:
            OUT = 1
            IN = 0

        machine_stub = types.ModuleType("machine")
        machine_stub.Pin = FakePin
        firmware_path = os.path.join(os.path.dirname(__file__), "rp2040 nand.py")
        spec = importlib.util.spec_from_file_location("nand_firmware_crc", firmware_path)
        firmware = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"machine": machine_stub}):
            spec.loader.exec_module(firmware)

        bad_request = bytearray(encode_frame(CMD_INFO, 22))
        bad_request[-1] ^= 0x80
        with self.assertRaises(firmware.FrameError) as error:
            firmware.read_request(io.BytesIO(bad_request))
        self.assertEqual(error.exception.command, CMD_INFO)
        self.assertEqual(error.exception.sequence, 22)
        self.assertEqual(error.exception.status, firmware.STATUS_BAD_CRC)


if __name__ == "__main__":
    unittest.main()
