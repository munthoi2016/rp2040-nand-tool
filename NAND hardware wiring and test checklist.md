# RP2040 NAND bridge: wiring and hardware checklist

This is a bring-up wiring proposal for the GPIO assignment in `rp2040 nand.py`.
It is not a confirmation of the package pin numbers or electrical limits for a
particular NAND. Identify each signal by name in the exact NAND package
datasheet before wiring.

```text
RP2040                                  NAND (signal names, not package pins)
GPIO2  -------------------------------- I/O0 / D0
GPIO3  -------------------------------- I/O1 / D1
GPIO4  -------------------------------- I/O2 / D2
GPIO5  -------------------------------- I/O3 / D3
GPIO6  -------------------------------- I/O4 / D4
GPIO7  -------------------------------- I/O5 / D5
GPIO8  -------------------------------- I/O6 / D6
GPIO9  -------------------------------- I/O7 / D7
GPIO10 -------------------------------- CLE
GPIO11 -------------------------------- ALE
GPIO12 -------------------------------- WE#
GPIO13 -------------------------------- RE#
GPIO14 -------------------------------- CE#
GPIO15 <-------------------------------- R/B# -- pull-up to NAND I/O supply
GND    -------------------------------- VSS
Supply per datasheet ------------------ VCC / VCCQ
```

## Signal map

| NAND signal | RP2040 GPIO | Notes |
|---|---:|---|
| I/O0-D0 through I/O7-D7 | GPIO2 through GPIO9, same order | 8-bit bidirectional bus |
| CLE | GPIO10 | Command latch enable |
| ALE | GPIO11 | Address latch enable |
| WE# | GPIO12 | Active low |
| RE# | GPIO13 | Active low |
| CE# | GPIO14 | Active low |
| R/B# | GPIO15 | Open-drain/busy output; pull up to NAND I/O supply |
| VCC / VCCQ | Regulated supply per NAND datasheet | Never power from a GPIO |
| VSS | Common ground | Connect RP2040 and NAND ground |
| WP# | Per datasheet, normally held inactive-high for normal programming | Do not leave floating |

Use a 4.7-10 kOhm pull-up from R/B# to the NAND's specified I/O supply. Add
local decoupling at the NAND (at least 100 nF, with bulk capacitance appropriate
to the board). Keep the parallel bus short and use a common ground. Do not
connect a 5 V NAND interface to RP2040 GPIO; RP2040 GPIO is not 5 V tolerant.
Check whether the exact NAND variant has separate VCC and VCCQ rails and whether
its logic-high thresholds are compatible with 3.3 V before connecting it.

## Before applying power

- [ ] Confirm the exact NAND part number, package, pinout, VCC/VCCQ range,
      I/O voltage, and WP#/R/B# behavior from its datasheet.
- [ ] Confirm the actual page size, OOB size, pages per block, block count,
      column/row address-cycle count, and bad-block marker location. The firmware
      currently assumes 2048+64 bytes, 64 pages/block, 1024 blocks, two row
      cycles, and marker byte 0 of OOB on the first two pages. These are
      configuration assumptions that must be checked against the exact part.
- [ ] Confirm the board is RP2040 (not RP2350) and the selected GPIO are exposed
      and not used by other circuitry.
- [ ] Check continuity, shorts between adjacent bus signals, supply polarity,
      pull-up voltage, and common ground with power removed.
- [ ] Keep NAND WP# asserted during first power-up if the package permits it.
- [ ] Verify NAND supply rails with a meter before connecting the RP2040 bus.

## Read-only bring-up

- [ ] Load MicroPython `rp2040 nand.py` as the board's main script; do not leave
      REPL output or other applications sharing the serial port while operating.
- [ ] Connect the PC host to the board's USB serial port and verify protocol v1
      INFO reports geometry matching the confirmed datasheet.
- [ ] With no NAND connected, verify the bridge reports an error/timeout rather
      than hanging. Do not issue program or erase commands during this check.
- [ ] With NAND connected and write-protect asserted, read known-erased pages
      and confirm the data/OOB are stable across repeated reads.
- [ ] Use a logic analyzer to verify CLE/ALE setup, WE#/RE# pulse polarity,
      CE# behavior, address-cycle count, and R/B# timeout against the datasheet.
- [ ] Confirm bad-block marker handling on known-good and known-bad blocks without
      erasing or programming them.

## Destructive-operation qualification

- [ ] Use a sacrificial NAND first; do not test on data that is not backed up.
- [ ] Confirm block/page addressing with a datasheet-approved test address.
- [ ] Erase only one sacrificial block; verify status and read back all pages.
- [ ] Program a known pattern only after confirming erase; read it back and
      compare all 2112 bytes, including OOB.
- [ ] Verify program/erase failure and R/B timeout are returned as protocol
      errors and do not cause automatic retries or writes to another block.
- [ ] Repeat at the lowest and highest supported supply/temperature conditions
      only after timing and electrical limits are established.

The firmware intentionally uses slow GPIO bit-banging instead of DMA/PIO for
initial validation. It is a conservative bring-up baseline, not a performance
driver or production-qualified implementation. The geometry and timing must be
validated on the exact NAND and board before any destructive operation.

## Serial protocol v1

All multi-byte integers are little-endian. Requests and responses use:

```text
"ND" | version:u8 | command:u8 | sequence:u16 | length:u16 |
payload[length] | CRC16-CCITT:u16
```

CRC-16/CCITT uses polynomial `0x1021`, initial value `0xFFFF`, and covers
`version` through the final payload byte (not the `"ND"` sync bytes). A response
sets command bit 7 and starts its payload with a status byte: `0` success, `1`
invalid command/address, `2` ready timeout, `3` NAND program/erase failure,
`4` invalid payload length, `5` request CRC mismatch, and `6` unsupported
protocol version.

| Command | ID | Request payload | Success response body |
|---|---:|---|---|
| INFO | `0x01` | empty | page size:u16, OOB size:u16, pages/block:u16, blocks:u32 |
| READ_PAGE | `0x02` | page:u32 | 2112 raw data+OOB bytes |
| WRITE_PAGE | `0x03` | page:u32 + 2112 raw data+OOB bytes | empty |
| ERASE_BLOCK | `0x04` | block:u16 | empty |
| CHECK_BLOCK | `0x05` | block:u16 | one byte: `1` good, `0` bad |

The bridge serializes requests on the host side and checks response command,
sequence, length, status, and CRC before returning data to the application.
