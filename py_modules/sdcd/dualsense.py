"""DualSense (Bluetooth) protocol: descriptors, SDP record and report encoding."""
import math
import struct
import time
import zlib

from .deck import DeckInput

SONY_VID = 0x054C
DUALSENSE_PID = 0x0CE6

INPUT_REPORT_LEN = 78  # report 0x31 including report ID and CRC
TOUCH_W, TOUCH_H = 1920, 1080

# Bluetooth HID descriptor: simple report 0x01, full input/output report 0x31
# and the vendor feature reports hosts read during initialization.
DESCRIPTOR = bytes([
    0x05, 0x01, 0x09, 0x05, 0xA1, 0x01,
    # Report 0x01: simple state (9 bytes)
    0x85, 0x01,
    0x09, 0x30, 0x09, 0x31, 0x09, 0x32, 0x09, 0x35,
    0x15, 0x00, 0x26, 0xFF, 0x00, 0x75, 0x08, 0x95, 0x04, 0x81, 0x02,
    0x09, 0x39, 0x15, 0x00, 0x25, 0x07, 0x35, 0x00, 0x46, 0x3B, 0x01, 0x65, 0x14,
    0x75, 0x04, 0x95, 0x01, 0x81, 0x42, 0x65, 0x00,
    0x05, 0x09, 0x19, 0x01, 0x29, 0x0E, 0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x0E, 0x81, 0x02,
    0x06, 0x00, 0xFF, 0x09, 0x20, 0x15, 0x00, 0x25, 0x3F, 0x75, 0x06, 0x95, 0x01, 0x81, 0x02,
    0x05, 0x01, 0x09, 0x33, 0x09, 0x34, 0x15, 0x00, 0x26, 0xFF, 0x00, 0x75, 0x08, 0x95, 0x02, 0x81, 0x02,
    # Vendor reports
    0x06, 0x00, 0xFF, 0x15, 0x00, 0x26, 0xFF, 0x00, 0x75, 0x08,
    0x85, 0x31, 0x09, 0x31, 0x95, 0x4D, 0x81, 0x02,  # input 0x31, 77 bytes
    0x85, 0x31, 0x09, 0x3B, 0x95, 0x4D, 0x91, 0x02,  # output 0x31, 77 bytes
    0x85, 0x05, 0x09, 0x33, 0x95, 0x28, 0xB1, 0x02,  # feature 0x05 calibration
    0x85, 0x08, 0x09, 0x34, 0x95, 0x2F, 0xB1, 0x02,
    0x85, 0x09, 0x09, 0x24, 0x95, 0x13, 0xB1, 0x02,  # feature 0x09 pairing info
    0x85, 0x20, 0x09, 0x26, 0x95, 0x3F, 0xB1, 0x02,  # feature 0x20 firmware info
    0x85, 0x22, 0x09, 0x40, 0x95, 0x3F, 0xB1, 0x02,
    0xC0,
])

SDP_RECORD = f"""<?xml version="1.0" encoding="UTF-8" ?>
<record>
  <attribute id="0x0001"><sequence><uuid value="0x1124" /></sequence></attribute>
  <attribute id="0x0004"><sequence>
    <sequence><uuid value="0x0100" /><uint16 value="0x0011" /></sequence>
    <sequence><uuid value="0x0011" /></sequence>
  </sequence></attribute>
  <attribute id="0x0005"><sequence><uuid value="0x1002" /></sequence></attribute>
  <attribute id="0x0006"><sequence>
    <uint16 value="0x656e" /><uint16 value="0x006a" /><uint16 value="0x0100" />
  </sequence></attribute>
  <attribute id="0x0009"><sequence>
    <sequence><uuid value="0x1124" /><uint16 value="0x0101" /></sequence>
  </sequence></attribute>
  <attribute id="0x000d"><sequence><sequence>
    <sequence><uuid value="0x0100" /><uint16 value="0x0013" /></sequence>
    <sequence><uuid value="0x0011" /></sequence>
  </sequence></sequence></attribute>
  <attribute id="0x0100"><text value="Wireless Controller" /></attribute>
  <attribute id="0x0101"><text value="Game Controller" /></attribute>
  <attribute id="0x0102"><text value="Sony Interactive Entertainment" /></attribute>
  <attribute id="0x0201"><uint16 value="0x0111" /></attribute>
  <attribute id="0x0202"><uint8 value="0x08" /></attribute>
  <attribute id="0x0203"><uint8 value="0x00" /></attribute>
  <attribute id="0x0204"><boolean value="true" /></attribute>
  <attribute id="0x0205"><boolean value="true" /></attribute>
  <attribute id="0x0206"><sequence><sequence>
    <uint8 value="0x22" /><text encoding="hex" value="{DESCRIPTOR.hex()}" />
  </sequence></sequence></attribute>
  <attribute id="0x0207"><sequence><sequence>
    <uint16 value="0x0409" /><uint16 value="0x0100" />
  </sequence></sequence></attribute>
  <attribute id="0x020b"><uint16 value="0x0100" /></attribute>
  <attribute id="0x020c"><uint16 value="0x0c80" /></attribute>
  <attribute id="0x020d"><boolean value="false" /></attribute>
  <attribute id="0x020e"><boolean value="false" /></attribute>
</record>
"""

# Deck IMU: gyro 16.384 LSB/(deg/s), accel 16384 LSB/g.
# The advertised calibration makes gyro pass through 1:1 and accel at half scale.
GYRO_PLUS, GYRO_SPEED, ACCEL_PLUS = 8847, 540, 8192


def _with_crc(seed: int, report: bytearray) -> bytes:
    """Fill the trailing 4 bytes with the DualSense Bluetooth CRC32."""
    crc = zlib.crc32(bytes([seed]) + bytes(report[:-4]))
    struct.pack_into("<I", report, len(report) - 4, crc)
    return bytes(report)


def feature_report(rid: int, mac: bytes) -> bytes | None:
    """Feature report `rid` with CRC, or None if unsupported. `mac` is our BT address."""
    if rid == 0x05:
        r = bytearray(41)
        r[0] = 0x05
        for axis in range(3):
            struct.pack_into("<hh", r, 7 + axis * 4, GYRO_PLUS, -GYRO_PLUS)
        struct.pack_into("<hh", r, 19, GYRO_SPEED, GYRO_SPEED)
        for axis in range(3):
            struct.pack_into("<hh", r, 23 + axis * 4, ACCEL_PLUS, -ACCEL_PLUS)
    elif rid == 0x09:
        r = bytearray(20)
        r[0] = 0x09
        r[1:7] = mac[::-1]  # little-endian
    elif rid == 0x20:
        r = bytearray(64)
        r[0] = 0x20
        r[1:20] = b"Jun 19 202314:47:34"
        r[24:32] = bytes([0x03, 0x00, 0x04, 0x00, 0x03, 0x06, 0x01, 0x01])
        r[44:46] = bytes([0x30, 0x06])
    else:
        return None
    return _with_crc(0xA3, r)


def _stick(x: int, y: int, deadzone: float) -> tuple[int, int]:
    """Deck stick (int16, +y up) -> DualSense bytes (0..255, +y down), radial deadzone."""
    fx, fy = x / 32768.0, y / 32768.0
    mag = math.hypot(fx, fy)
    if mag <= deadzone:
        return 128, 128
    scale = min(1.0, (mag - deadzone) / (1.0 - deadzone)) / mag
    fx, fy = fx * scale, fy * scale
    return (max(0, min(255, round(128 + fx * 127.5))),
            max(0, min(255, round(128 - fy * 127.5))))


def _touch(touching: bool, touch_id: int, x: int, y: int, left_half: bool) -> bytes:
    """One DualSense touch point from a Deck trackpad, mapped onto half the touchpad."""
    if not touching:
        return bytes([0x80 | touch_id, 0, 0, 0])
    half = TOUCH_W // 2
    tx = int((x + 32768) / 65536 * half) + (0 if left_half else half)
    ty = int((32767 - y) / 65536 * TOUCH_H)
    tx, ty = max(0, min(TOUCH_W - 1, tx)), max(0, min(TOUCH_H - 1, ty))
    return bytes([touch_id & 0x7F, tx & 0xFF, ((tx >> 8) & 0x0F) | ((ty & 0x0F) << 4), ty >> 4])


_HAT = {
    (1, 0, 0, 0): 0, (1, 1, 0, 0): 1, (0, 1, 0, 0): 2, (0, 1, 1, 0): 3,
    (0, 0, 1, 0): 4, (0, 0, 1, 1): 5, (0, 0, 0, 1): 6, (1, 0, 0, 1): 7,
}


class InputEncoder:
    """Builds DualSense 0x31 input reports from Deck controller state."""

    def __init__(self, deadzone: float = 0.08):
        self.deadzone = deadzone
        self.counter = 0
        self.touch_ids = [0, 1]  # incremented on each new contact
        self.was_touching = [False, False]

    def encode(self, d: DeckInput | None) -> bytes:
        r = bytearray(INPUT_REPORT_LEN)
        r[0] = 0x31
        r[1] = (self.counter << 4) & 0xF0
        st = memoryview(r)[2:65]  # the 63-byte common state block
        st[0:4] = bytes([128, 128, 128, 128])
        st[7] = 0x08  # hat neutral
        st[6] = self.counter
        st[32:40] = bytes([0x80, 0, 0, 0, 0x81, 0, 0, 0])  # no touches
        st[52] = 0x08  # battery level
        struct.pack_into("<I", st, 27, int(time.monotonic() * 3_000_000) & 0xFFFFFFFF)
        self.counter = (self.counter + 1) & 0xFF

        if d is not None:
            self._fill(st, d)
        return _with_crc(0xA1, r)

    def _fill(self, st: memoryview, d: DeckInput):
        st[0], st[1] = _stick(d.lx, d.ly, self.deadzone)
        st[2], st[3] = _stick(d.rx, d.ry, self.deadzone)
        st[4], st[5] = min(255, d.lt >> 7), min(255, d.rt >> 7)

        hat = _HAT.get((d.up, d.right, d.down, d.left), 8)
        st[7] = (hat
                 | (0x10 if d.x else 0) | (0x20 if d.a else 0)
                 | (0x40 if d.b else 0) | (0x80 if d.y else 0))
        st[8] = ((0x01 if d.l1 else 0) | (0x02 if d.r1 else 0)
                 | (0x04 if d.l2 or st[4] > 30 else 0) | (0x08 if d.r2 or st[5] > 30 else 0)
                 | (0x10 if d.view else 0) | (0x20 if d.menu else 0)
                 | (0x40 if d.l3 else 0) | (0x80 if d.r3 else 0))
        st[9] = ((0x01 if d.steam else 0)
                 | (0x02 if d.lpad_click or d.rpad_click else 0))

        # Deck axes -> DualSense axes: (x, z, -y)
        struct.pack_into("<3h", st, 15, d.gx, d.gz, max(-32768, min(32767, -d.gy)))
        struct.pack_into("<3h", st, 21, d.ax // 2, d.az // 2, max(-32768, min(32767, -d.ay // 2)))

        for i, (touching, x, y) in enumerate(((d.lpad_touch, d.lpad_x, d.lpad_y),
                                              (d.rpad_touch, d.rpad_x, d.rpad_y))):
            if touching and not self.was_touching[i]:
                self.touch_ids[i] = (self.touch_ids[i] + 2) & 0x7F
            self.was_touching[i] = touching
            st[32 + 4 * i:36 + 4 * i] = _touch(touching, self.touch_ids[i], x, y, left_half=(i == 0))


def parse_rumble(msg: bytes) -> tuple[int, int] | None:
    """(low_freq, high_freq) motor levels 0..255 from an 0xA2 0x31 output report."""
    # a2 31 <seq_tag> <tag> <valid_flag0> <valid_flag1> <motor_right> <motor_left> ...
    if len(msg) < 8 or msg[0] != 0xA2 or msg[1] != 0x31:
        return None
    if not msg[4] & 0x03:  # neither compatible vibration nor haptics select
        return None
    return msg[7], msg[6]
