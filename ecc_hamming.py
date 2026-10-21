class HammingECC:
    """Legacy NAND 3-byte Hamming ECC for each 512-byte data step.

    The byte order and bit layout match the Linux software-Hamming NAND
    convention (non-SmartMedia order): ECC byte 1 protects address bits 0-3,
    byte 0 protects address bits 4-7, and byte 2 protects address bit 8 plus
    the data-bit address. Existing OOB data using the former custom layout
    must be treated as incompatible.
    """

    @staticmethod
    def calculate_512(data):
        if len(data) != 512:
            raise ValueError("NAND Hamming ECC requires exactly 512 data bytes")

        ecc = bytearray((0xFF, 0xFF, 0xFF))
        for byte_addr, value in enumerate(data):
            if not value:
                continue

            for bit_addr in range(8):
                if not (value & (1 << bit_addr)):
                    continue

                # Each address bit is represented by a complementary pair.
                # With erased/all-zero input the ECC bytes remain 0xFF.
                ecc[1] ^= 1 << (2 * 0 + ((byte_addr >> 0) & 1))
                ecc[1] ^= 1 << (2 * 1 + ((byte_addr >> 1) & 1))
                ecc[1] ^= 1 << (2 * 2 + ((byte_addr >> 2) & 1))
                ecc[1] ^= 1 << (2 * 3 + ((byte_addr >> 3) & 1))

                ecc[0] ^= 1 << (2 * 0 + ((byte_addr >> 4) & 1))
                ecc[0] ^= 1 << (2 * 1 + ((byte_addr >> 5) & 1))
                ecc[0] ^= 1 << (2 * 2 + ((byte_addr >> 6) & 1))
                ecc[0] ^= 1 << (2 * 3 + ((byte_addr >> 7) & 1))

                ecc[2] ^= 1 << (2 * 0 + ((byte_addr >> 8) & 1))
                ecc[2] ^= 1 << (2 * 1 + ((bit_addr >> 0) & 1))
                ecc[2] ^= 1 << (2 * 2 + ((bit_addr >> 1) & 1))
                ecc[2] ^= 1 << (2 * 3 + ((bit_addr >> 2) & 1))

        return bytes(ecc)

    @staticmethod
    def correct_512(data, read_ecc, calc_ecc):
        """Return (status, corrected_bytes).

        Status is 0 for clean data, 1 for a corrected data-bit error, 2 when
        only a single ECC bit differs, and -1 for an uncorrectable syndrome.
        """
        if len(data) != 512:
            raise ValueError("NAND Hamming ECC requires exactly 512 data bytes")
        if len(read_ecc) != 3 or len(calc_ecc) != 3:
            raise ValueError("NAND Hamming ECC values must be exactly 3 bytes")

        # Linux legacy NAND ECC places the lower address nibble in byte 1.
        b0 = read_ecc[1] ^ calc_ecc[1]
        b1 = read_ecc[0] ^ calc_ecc[0]
        b2 = read_ecc[2] ^ calc_ecc[2]
        if (b0 | b1 | b2) == 0:
            return 0, bytes(data)

        def is_complementary_pairs(value, mask=0x55):
            return ((value ^ (value >> 1)) & mask) == mask

        if (
            is_complementary_pairs(b0)
            and is_complementary_pairs(b1)
            and is_complementary_pairs(b2)
        ):
            def decode_address(value):
                return (
                    ((value >> 1) & 0x01)
                    | ((value >> 2) & 0x02)
                    | ((value >> 3) & 0x04)
                    | ((value >> 4) & 0x08)
                )

            byte_addr = (
                (decode_address(b2 & 0x03) << 8)
                | (decode_address(b1) << 4)
                | decode_address(b0)
            )
            bit_addr = decode_address(b2 >> 2)
            if byte_addr >= 512 or bit_addr >= 8:
                return -1, bytes(data)

            corrected = bytearray(data)
            corrected[byte_addr] ^= 1 << bit_addr
            if HammingECC.calculate_512(corrected) != bytes(read_ecc):
                return -1, bytes(data)
            return 1, bytes(corrected)

        if (b0.bit_count() + b1.bit_count() + b2.bit_count()) == 1:
            return 2, bytes(data)

        return -1, bytes(data)
