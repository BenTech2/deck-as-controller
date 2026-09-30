"""Steam Controller (2026, "Triton"/"Ibex") over Bluetooth LE (HID over GATT).

Steam (and SDL, and Linux's hid-steam) drive this controller themselves, so the
host gets the Deck's trackpads, gyro, grips and stick touch for Steam Input.

Report layouts follow SDL's HIDAPI Triton driver and linux drivers/hid/hid-steam.c;
the descriptor and identity replies are the real controller's (as captured by
openpuck and found in Valve's firmware image).
"""
import hashlib
import struct
import time

from ..deck import DeckInput
from .base import Battery, Encoder, Profile, radial_deadzone

# The controller's own report map: mouse 0x40 and keyboard 0x41 (lizard mode, which we
# never send), vendor inputs 0x42-0x45/0x79/0x7B, haptic outputs 0x80-0x89 and the
# feature reports 0x01 (commands to the controller) and 0x02 (commands to the dongle).
DESCRIPTOR = bytes([
    0x05, 0x01, 0x09, 0x02, 0xA1, 0x01, 0x85, 0x40, 0x09, 0x01, 0xA1, 0x00, 0x05, 0x09, 0x19, 0x01,
    0x29, 0x02, 0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x02, 0x81, 0x02, 0x75, 0x06, 0x95, 0x01,
    0x81, 0x01, 0x05, 0x01, 0x09, 0x30, 0x09, 0x31, 0x15, 0x81, 0x25, 0x7F, 0x75, 0x08, 0x95, 0x02,
    0x81, 0x06, 0x95, 0x01, 0x09, 0x38, 0x81, 0x06, 0x05, 0x0C, 0x0A, 0x38, 0x02, 0x95, 0x01, 0x81,
    0x06, 0xC0, 0xC0, 0x05, 0x01, 0x09, 0x06, 0xA1, 0x01, 0x85, 0x41, 0x05, 0x07, 0x19, 0xE0, 0x29,
    0xE7, 0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x08, 0x81, 0x02, 0x81, 0x01, 0x19, 0x00, 0x29,
    0x65, 0x15, 0x00, 0x25, 0x65, 0x75, 0x08, 0x95, 0x06, 0x81, 0x00, 0xC0, 0x06, 0x00, 0xFF, 0x09,
    0x01, 0xA1, 0x01, 0x85, 0x42, 0x15, 0x00, 0x26, 0xFF, 0x00, 0x75, 0x08, 0x95, 0x35, 0x09, 0x42,
    0x81, 0x02, 0x85, 0x44, 0x15, 0x00, 0x26, 0xFF, 0x00, 0x75, 0x08, 0x95, 0x05, 0x09, 0x44, 0x81,
    0x02, 0x85, 0x79, 0x15, 0x00, 0x26, 0xFF, 0x00, 0x75, 0x08, 0x95, 0x01, 0x09, 0x79, 0x81, 0x02,
    0x85, 0x43, 0x15, 0x00, 0x26, 0xFF, 0x00, 0x75, 0x08, 0x95, 0x0E, 0x09, 0x43, 0x81, 0x02, 0x85,
    0x7B, 0x15, 0x00, 0x26, 0xFF, 0x00, 0x75, 0x08, 0x95, 0x0C, 0x09, 0x7B, 0x81, 0x02, 0x85, 0x45,
    0x15, 0x00, 0x26, 0xFF, 0x00, 0x75, 0x08, 0x95, 0x2D, 0x09, 0x45, 0x81, 0x02, 0x85, 0x80, 0x15,
    0x00, 0x26, 0xFF, 0x00, 0x75, 0x08, 0x95, 0x09, 0x09, 0x80, 0x91, 0x02, 0x85, 0x81, 0x15, 0x00,
    0x26, 0xFF, 0x00, 0x75, 0x08, 0x95, 0x07, 0x09, 0x81, 0x91, 0x02, 0x85, 0x82, 0x15, 0x00, 0x26,
    0xFF, 0x00, 0x75, 0x08, 0x95, 0x03, 0x09, 0x82, 0x91, 0x02, 0x85, 0x83, 0x15, 0x00, 0x26, 0xFF,
    0x00, 0x75, 0x08, 0x95, 0x09, 0x09, 0x83, 0x91, 0x02, 0x85, 0x84, 0x15, 0x00, 0x26, 0xFF, 0x00,
    0x75, 0x08, 0x95, 0x08, 0x09, 0x84, 0x91, 0x02, 0x85, 0x85, 0x15, 0x00, 0x26, 0xFF, 0x00, 0x75,
    0x08, 0x95, 0x03, 0x09, 0x85, 0x91, 0x02, 0x85, 0x86, 0x15, 0x00, 0x26, 0xFF, 0x00, 0x75, 0x08,
    0x95, 0x03, 0x09, 0x86, 0x91, 0x02, 0x85, 0x87, 0x15, 0x00, 0x26, 0xFF, 0x00, 0x75, 0x08, 0x95,
    0x3F, 0x09, 0x87, 0x91, 0x02, 0x85, 0x89, 0x15, 0x00, 0x26, 0xFF, 0x00, 0x75, 0x08, 0x95, 0x3F,
    0x09, 0x89, 0x91, 0x02, 0x85, 0x88, 0x15, 0x00, 0x26, 0xFF, 0x00, 0x75, 0x08, 0x95, 0x3F, 0x09,
    0x88, 0x91, 0x02, 0x85, 0x01, 0x95, 0x3F, 0x09, 0x01, 0xB1, 0x02, 0x85, 0x02, 0x95, 0x3F, 0x09,
    0x01, 0xB1, 0x02, 0xC0,
])

REPORT_STATE = 0x45  # controller state over BLE (TritonMTUNoQuat_t), 45 bytes after the ID
REPORT_BATTERY = 0x43
REPORT_RUMBLE = 0x80
FEATURE_COMMANDS = 0x01
FEATURE_LEN = 63

# Feature command IDs (SDL's steam/controller_constants.h)
ID_CLEAR_DIGITAL_MAPPINGS = 0x81
ID_GET_ATTRIBUTES_VALUES = 0x83
ID_SET_SETTINGS_VALUES = 0x87
ID_GET_STRING_ATTRIBUTE = 0xAE
ID_DONGLE_GET_WIRELESS_STATE = 0xB4

# GET_ATTRIBUTES reply of a real controller (tag u8, value u32 LE): product 0x1302,
# capabilities 0, bootloader and firmware build times, board revision 0x48.
ATTRIBUTES = bytes.fromhex("0102130000" "0200000000" "0a2ef9d268" "0457d0186a" "0948000000")

# Button bits in the state report
_BUTTONS = {
    "a": 0x1, "b": 0x2, "x": 0x4, "y": 0x8,
    "r3": 0x20, "menu": 0x40, "r4": 0x80, "r5": 0x100, "r1": 0x200,
    "down": 0x400, "right": 0x800, "left": 0x1000, "up": 0x2000,
    "view": 0x4000, "l3": 0x8000, "steam": 0x10000, "l4": 0x20000, "l5": 0x40000, "l1": 0x80000,
    "rstick_touch": 0x100000, "rpad_touch": 0x200000, "rpad_click": 0x400000, "r2": 0x800000,
    "lstick_touch": 0x1000000, "lpad_touch": 0x2000000, "lpad_click": 0x4000000, "l2": 0x8000000,
}
# ⋯ (QAM, 0x10) stays reserved for the plugin, as with the other types.

# Charge states in the battery report
CHARGE_DISCHARGING, CHARGE_CHARGING, CHARGE_DONE = 1, 2, 4


def _stick(x: int, y: int, deadzone: float) -> tuple[int, int]:
    """Deck stick -> controller stick (int16, +y up, as on the Deck)."""
    fx, fy = radial_deadzone(x, y, deadzone)
    return round(fx * 32767), round(fy * 32767)


class SteamControllerEncoder(Encoder):
    def __init__(self, deadzone: float, serial: str):
        self.deadzone = deadzone
        self.serial = serial
        self.seq = 0
        self.reply = b""  # answer to the last feature command, read back by the host

    def encode(self, d: DeckInput | None, battery: Battery) -> bytes:
        r = bytearray(46)
        r[0] = REPORT_STATE
        r[1] = self.seq
        self.seq = (self.seq + 1) & 0xFF
        # IMU timestamp in microseconds; hosts derive the sensor rate from it.
        struct.pack_into("<I", r, 30, int(time.monotonic() * 1_000_000) & 0xFFFFFFFF)
        if d is None:
            return bytes(r)
        buttons = 0
        for name, bit in _BUTTONS.items():
            if getattr(d, name):
                buttons |= bit
        # Deck and controller share conventions (SDL reads both the same way):
        # triggers 0..32767, sticks and pads int16 with +y up, pad pressure,
        # accel 16384/g and gyro 16.384/(deg/s) in the same axis frame.
        lx, ly = _stick(d.lx, d.ly, self.deadzone)
        rx, ry = _stick(d.rx, d.ry, self.deadzone)
        struct.pack_into("<I2H4h", r, 2, buttons, d.lt, d.rt, lx, ly, rx, ry)
        struct.pack_into("<2hH2hH", r, 18, d.lpad_x, d.lpad_y, d.lpad_pressure,
                         d.rpad_x, d.rpad_y, d.rpad_pressure)
        struct.pack_into("<6h", r, 34, d.ax, d.ay, d.az, d.gx, d.gy, d.gz)
        return bytes(r)

    # ---- feature commands: SET_REPORT(0x01) [cmd, len, payload], then GET_REPORT(0x01) ----

    def set_feature(self, report_id: int, data: bytes):
        if report_id != FEATURE_COMMANDS or not data:
            return
        cmd = data[0]
        if cmd == ID_GET_ATTRIBUTES_VALUES:
            self.reply = bytes([cmd, len(ATTRIBUTES)]) + ATTRIBUTES
        elif cmd == ID_GET_STRING_ATTRIBUTE:
            # Steam drops the controller unless the unit serial (index 1) starts with 'F'
            # and the index is echoed in byte 2.
            length = data[1] if len(data) > 1 and data[1] else 0x15
            index = data[2] if len(data) > 2 else 1
            text = {0: "M" + self.serial[1:], 1: self.serial}.get(index, "")
            self.reply = bytes([cmd, length, index]) + text.encode().ljust(length - 1, b"\0")
        elif cmd == ID_DONGLE_GET_WIRELESS_STATE:
            self.reply = bytes([cmd, 1, 2])  # connected
        else:
            # Settings (lizard mode off, IMU on), mapping resets, etc.: nothing to do, since
            # we never emulate a mouse/keyboard and always send the IMU. Acknowledge them.
            self.reply = bytes([cmd, 0])

    def get_feature(self, report_id: int) -> bytes | None:
        if report_id not in (FEATURE_COMMANDS, 0x02):
            return None
        return self.reply.ljust(FEATURE_LEN, b"\0")[:FEATURE_LEN]


class SteamController(Profile):
    id = "steam_controller"
    label = "Steam Controller (Steam Input)"
    vendor_id = 0x28DE
    product_id = 0x1303  # Steam Controller (2026) over BLE
    version = 0x0100
    bt_name = "Steam Controller"
    service_name = "Steam Controller"  # GATT model number
    provider = "Valve Corporation"  # GATT manufacturer name
    descriptor = DESCRIPTOR
    transport = "ble"
    appearance = 0x03C4  # gamepad

    def __init__(self):
        self.serial = "FXA0000000000"
        self.static_address = "C0:00:00:00:00:01"

    def personalize(self, mac: bytes):
        # Stable per Deck, so hosts that paired it keep recognising it.
        digest = hashlib.sha256(b"deck-as-controller/steam-controller" + mac).digest()
        self.serial = "FXA" + digest[:5].hex().upper()  # 13 characters, like a real unit
        # A static random address (two top bits set) keeps this identity separate from the
        # Deck's public address, which hosts may know as a PS5/Xbox controller.
        addr = bytes([digest[5] | 0xC0]) + digest[6:11]
        self.static_address = ":".join(f"{b:02X}" for b in addr)
        self.bt_name = f"Steam Ctrl (BT) {self.serial}"

    def new_encoder(self, deadzone: float) -> Encoder:
        return SteamControllerEncoder(deadzone, self.serial)

    def parse_rumble(self, msg: bytes) -> tuple[int, int, float | None] | None:
        # a2 80 <type> <intensity u16> <left speed u16> <left gain> <right speed u16> <right gain>
        # Left is the low-frequency motor. Hosts repeat it every ~40 ms; the controller
        # stops by itself ~50 ms after the last one, so we do the same (with some slack).
        if len(msg) < 10 or msg[0] != 0xA2 or msg[1] != REPORT_RUMBLE:
            return None
        left, right = struct.unpack_from("<H", msg, 5)[0], struct.unpack_from("<H", msg, 8)[0]
        return left >> 8, right >> 8, 0.1

    def side_reports(self, battery: Battery) -> list[bytes]:
        if battery.charging:
            state = CHARGE_DONE if battery.percent >= 100 else CHARGE_CHARGING
        else:
            state = CHARGE_DISCHARGING
        r = bytearray(15)
        r[0], r[1], r[2] = REPORT_BATTERY, state, max(0, min(100, battery.percent))
        return [bytes(r)]
