"""Bluetooth LE transport: the Deck as a HID-over-GATT peripheral.

bluetoothd still runs pairing (the kernel does SMP) and advertising, but we serve
GATT ourselves on the ATT channel, bound to a static random address:
- input reports go out as notifications straight from our socket, like the HID
  interrupt channel for the Classic profiles;
- the static address keeps this identity apart from the Deck's public address,
  which hosts may already know as a PS5/Xbox controller. BR/EDR is off meanwhile.
bluetoothd's own GATT server can't take connections on the static address
(found by phly95/spoofdeck, which does the same), so we don't use it.
"""
import ctypes
import json
import logging
import os
import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import Callable

import dbus
import dbus.service

from . import bluez, hid
from .profiles import Battery, Encoder, Profile

log = logging.getLogger("sdcd.ble")

LE_MODE_MARKER = "/run/sd-controller-le"
ADVERT_PATH = "/sdc/advertisement"

SOL_BLUETOOTH, BT_SECURITY, BT_SECURITY_MEDIUM = 274, 4, 2
ATT_CID, BDADDR_LE_RANDOM = 4, 2
HCI_DEV_NONE, HCI_CHANNEL_CONTROL = 0xFFFF, 3
MGMT_INDEX = 0  # hci0
MGMT_SET_POWERED, MGMT_SET_LE, MGMT_SET_BREDR, MGMT_SET_STATIC_ADDRESS = 0x05, 0x0D, 0x2A, 0x2B
MGMT_EV_CMD_COMPLETE, MGMT_EV_CMD_STATUS = 0x01, 0x02

# Connection interval we ask hosts for, in 1.25 ms units (11.25-15 ms: the lowest macOS
# grants HID devices), slave latency, and supervision timeout in 10 ms units.
CONN_PARAMS = (9, 12, 0, 400)

# bluetoothd applies these as the defaults the kernel requests from hosts.
MAIN_CONF = ("[LE]\nMinConnectionInterval = {}\nMaxConnectionInterval = {}\n"
             "ConnectionLatency = {}\nConnectionSupervisionTimeout = {}\n").format(*CONN_PARAMS)

SERVER_MTU = 517

# ATT opcodes and errors
ATT_ERROR, ATT_MTU_REQ, ATT_MTU_RSP = 0x01, 0x02, 0x03
ATT_FIND_INFO_REQ, ATT_FIND_INFO_RSP = 0x04, 0x05
ATT_FIND_BY_TYPE_REQ, ATT_FIND_BY_TYPE_RSP = 0x06, 0x07
ATT_READ_BY_TYPE_REQ, ATT_READ_BY_TYPE_RSP = 0x08, 0x09
ATT_READ_REQ, ATT_READ_RSP, ATT_READ_BLOB_REQ, ATT_READ_BLOB_RSP = 0x0A, 0x0B, 0x0C, 0x0D
ATT_READ_BY_GROUP_REQ, ATT_READ_BY_GROUP_RSP = 0x10, 0x11
ATT_WRITE_REQ, ATT_WRITE_RSP, ATT_WRITE_CMD = 0x12, 0x13, 0x52
ATT_PREPARE_WRITE_REQ, ATT_EXECUTE_WRITE_REQ, ATT_EXECUTE_WRITE_RSP = 0x16, 0x18, 0x19
ATT_NOTIFY = 0x1B
ERR_INVALID_HANDLE, ERR_READ_NOT_PERMITTED, ERR_WRITE_NOT_PERMITTED = 0x01, 0x02, 0x03
ERR_INVALID_PDU, ERR_INSUFFICIENT_AUTHENTICATION, ERR_REQUEST_NOT_SUPPORTED = 0x04, 0x05, 0x06
ERR_INVALID_OFFSET, ERR_ATTRIBUTE_NOT_FOUND, ERR_UNSUPPORTED_GROUP_TYPE = 0x07, 0x0A, 0x10

# GATT declarations and characteristic properties
UUID_PRIMARY_SERVICE, UUID_CHARACTERISTIC = 0x2800, 0x2803
UUID_CCCD, UUID_REPORT_REFERENCE = 0x2902, 0x2908
PROP_READ, PROP_WRITE_NO_RESPONSE, PROP_WRITE, PROP_NOTIFY, PROP_INDICATE = 0x02, 0x04, 0x08, 0x10, 0x20
# Bluetooth base UUID (0000xxxx-0000-1000-8000-00805f9b34fb) below the 16-bit part, little-endian
BASE_UUID_TAIL = bytes.fromhex("00001000800000805f9b34fb")[::-1]

REPORT_INPUT, REPORT_OUTPUT, REPORT_FEATURE = 1, 2, 3

_libc = ctypes.CDLL(None, use_errno=True)


def _bind(sock: socket.socket, sockaddr: bytes):
    # Python's socket module can't express these addresses (HCI control channel,
    # L2CAP fixed channel with an LE address type), so bind through libc.
    if _libc.bind(sock.fileno(), sockaddr, len(sockaddr)) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


def _addr_bytes(address: str) -> bytes:
    return bytes.fromhex(address.replace(":", ""))[::-1]


# ---- adapter mode ------------------------------------------------------------


def _mgmt(*commands: tuple[int, bytes]):
    """Run Bluetooth management commands on hci0, failing on the first error."""
    with socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI) as s:
        _bind(s, struct.pack("<HHH", socket.AF_BLUETOOTH, HCI_DEV_NONE, HCI_CHANNEL_CONTROL))
        s.settimeout(5)
        for op, params in commands:
            s.send(struct.pack("<HHH", op, MGMT_INDEX, len(params)) + params)
            while True:  # skip unrelated events (settings changes, other indexes)
                ev = s.recv(1024)
                if len(ev) < 9:
                    continue
                code, index, _, ev_op, status = struct.unpack_from("<HHHHB", ev)
                if code in (MGMT_EV_CMD_COMPLETE, MGMT_EV_CMD_STATUS) and index == MGMT_INDEX and ev_op == op:
                    break
            if status:
                raise OSError(f"Bluetooth management command 0x{op:04x} failed (status 0x{status:02x})")


def enter_le_mode(static_address: str):
    """LE only, on our static random address. Call after bluez.enter_gamepad_mode()."""
    open(LE_MODE_MARKER, "w").close()
    _mgmt((MGMT_SET_POWERED, b"\0"), (MGMT_SET_LE, b"\1"), (MGMT_SET_BREDR, b"\0"),
          (MGMT_SET_STATIC_ADDRESS, _addr_bytes(static_address)), (MGMT_SET_POWERED, b"\1"))


def leave_le_mode():
    """Undo enter_le_mode(). Safe to call when not active."""
    if not os.path.exists(LE_MODE_MARKER):
        return
    _mgmt((MGMT_SET_POWERED, b"\0"), (MGMT_SET_STATIC_ADDRESS, bytes(6)), (MGMT_SET_BREDR, b"\1"),
          (MGMT_SET_POWERED, b"\1"))
    os.remove(LE_MODE_MARKER)


class _Advertisement(dbus.service.Object):
    def __init__(self, bus, profile: Profile):
        super().__init__(bus, ADVERT_PATH)
        self.profile = profile
        self.discoverable = False

    @dbus.service.method("org.freedesktop.DBus.Properties", in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface):
        return {
            "Type": "peripheral",
            "ServiceUUIDs": dbus.Array(["1812"], signature="s"),
            "Appearance": dbus.UInt16(self.profile.appearance),
            "LocalName": self.profile.bt_name,
            "Discoverable": dbus.Boolean(self.discoverable),
        }

    @dbus.service.method("org.bluez.LEAdvertisement1", in_signature="", out_signature="")
    def Release(self):
        pass


class Adapter(bluez.Adapter):
    """The adapter as an LE peripheral: advertises while no host is connected.

    Paired hosts reconnect by themselves when they see us advertise, so there is
    no reconnect loop; outside the pairing window we advertise non-discoverable.
    """

    def __init__(self, bus, profile: Profile):
        self.advert = _Advertisement(bus, profile)
        self.advertising = False
        self.connected = False
        self.ads = dbus.Interface(bus.get_object("org.bluez", "/org/bluez/hci0"),
                                  "org.bluez.LEAdvertisingManager1")
        super().__init__(bus, profile)

    def _register(self, profile: Profile):
        pass  # no SDP record: HID over GATT is found through the GATT services

    def open_pairing(self, seconds: int):
        self.pairing_until = time.monotonic() + seconds
        self._set("Pairable", True)
        self._refresh()

    def close_pairing(self):
        self.pairing_until = 0.0
        self._set("Pairable", False)
        self._refresh()

    def set_connected(self, connected: bool):
        self.connected = connected
        self._refresh()

    def _refresh(self):
        discoverable = self.pairing_open()
        if self.advertising and (self.connected or discoverable != self.advert.discoverable):
            self.advertising = False
            self.ads.UnregisterAdvertisement(ADVERT_PATH, reply_handler=lambda: None,
                                             error_handler=lambda e: log.debug("unregister: %s", e))
        if not self.connected and not self.advertising:
            self.advertising = True
            self.advert.discoverable = discoverable
            # Async: bluetoothd calls back into our advertisement object before replying.
            self.ads.RegisterAdvertisement(ADVERT_PATH, {}, reply_handler=lambda: None,
                                           error_handler=self._advertise_failed)

    def _advertise_failed(self, e):
        log.error("could not advertise: %s", e)
        self.advertising = False


# ---- GATT database -------------------------------------------------------------


def report_ids(descriptor: bytes) -> list[tuple[int, int]]:
    """(report ID, REPORT_INPUT/OUTPUT/FEATURE) for each report in a HID descriptor."""
    found, report_id, i = [], 0, 0
    kinds = {0x80: REPORT_INPUT, 0x90: REPORT_OUTPUT, 0xB0: REPORT_FEATURE}
    while i < len(descriptor):
        prefix = descriptor[i]
        size = (0, 1, 2, 4)[prefix & 0x03]
        value = int.from_bytes(descriptor[i + 1:i + 1 + size], "little")
        tag = prefix & 0xFC
        if tag == 0x84:
            report_id = value
        elif tag in kinds and (report_id, kinds[tag]) not in found:
            found.append((report_id, kinds[tag]))
        i += 1 + size
    return found


class AttError(Exception):
    def __init__(self, handle: int, code: int):
        super().__init__(f"ATT error 0x{code:02x} on handle 0x{handle:04x}")
        self.handle, self.code = handle, code


@dataclass
class Attribute:
    type: int  # 16-bit UUID; everything we serve is a Bluetooth SIG UUID
    value: bytes = b""
    read: Callable[[], bytes] | None = None
    write: Callable[[bytes], None] | None = None
    readable: bool = True
    secure: bool = False  # needs an encrypted (paired) link
    handle: int = 0
    end: int = 0  # services: their last handle

    def get(self) -> bytes:
        return self.read() if self.read else self.value


class Gatt:
    def __init__(self):
        self.attrs: list[Attribute] = []

    def _add(self, attr: Attribute) -> Attribute:
        attr.handle = len(self.attrs) + 1
        self.attrs.append(attr)
        return attr

    def service(self, uuid: int):
        self._add(Attribute(UUID_PRIMARY_SERVICE, struct.pack("<H", uuid)))

    def characteristic(self, uuid: int, props: int, value: bytes = b"", *, read=None, write=None,
                       secure: bool = False) -> Attribute:
        decl = self._add(Attribute(UUID_CHARACTERISTIC))
        attr = self._add(Attribute(uuid, value, read, write, readable=bool(props & PROP_READ), secure=secure))
        decl.value = struct.pack("<BHH", props, attr.handle, uuid)
        return attr

    def descriptor(self, uuid: int, value: bytes = b"", *, read=None, write=None, secure: bool = False):
        return self._add(Attribute(uuid, value, read, write, secure=secure))

    def finish(self):
        services = [a for a in self.attrs if a.type == UUID_PRIMARY_SERVICE]
        for service, following in zip(services, services[1:] + [None]):
            service.end = following.handle - 1 if following else len(self.attrs)

    def get(self, handle: int) -> Attribute:
        if not 1 <= handle <= len(self.attrs):
            raise AttError(handle, ERR_INVALID_HANDLE)
        return self.attrs[handle - 1]

    def range(self, start: int, end: int) -> list[Attribute]:
        if start == 0 or start > end:
            raise AttError(start, ERR_INVALID_HANDLE)
        return self.attrs[start - 1:end]


class Subscriptions:
    """Notifications each paired host turned on. GATT keeps these across connections,
    so hosts don't necessarily enable them again when they reconnect."""

    def __init__(self, path: str):
        self.path = path
        try:
            with open(path) as f:
                self.items = json.load(f)
        except (OSError, ValueError):
            self.items = {}

    def get(self, address: str) -> set[int]:
        return set(self.items.get(address, []))

    def set(self, address: str, handles: set[int]):
        if self.get(address) != handles:
            self.items[address] = sorted(handles)
            self._save()

    def forget(self, address: str):
        if self.items.pop(address, None) is not None:
            self._save()

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(self.items, f)


# ---- connections ----------------------------------------------------------------


def listen(static_address: str) -> socket.socket:
    """Server socket for hosts connecting to our ATT channel."""
    s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)
    # struct sockaddr_l2: family, psm, bdaddr, cid, bdaddr_type (+ padding)
    _bind(s, struct.pack("<HH6sHBx", socket.AF_BLUETOOTH, 0, _addr_bytes(static_address),
                         ATT_CID, BDADDR_LE_RANDOM))
    s.listen(1)
    return s


def _uuid16(raw: bytes) -> int | None:
    if len(raw) == 2:
        return struct.unpack("<H", raw)[0]
    if len(raw) == 16 and raw[:12] == BASE_UUID_TAIL and raw[14:] == b"\0\0":
        return struct.unpack_from("<H", raw, 12)[0]
    return None  # a vendor UUID: we serve none


class Link(hid.Link):
    """One connected host over LE. Serves GATT from its own thread as soon as it's made;
    wait_ready() returns once the host has paired and enabled input notifications."""

    def __init__(self, sock: socket.socket, address: str, profile: Profile, encoder: Encoder,
                 get_report: Callable[[], bytes], has_urgent: Callable[[], bool],
                 on_rumble: Callable[[int, int, float | None], None],
                 battery: Callable[[], Battery], subscriptions: Subscriptions):
        # One socket carries everything; it stands in for both HID channels.
        super().__init__(sock, sock, address, b"", profile, get_report, has_urgent, on_rumble)
        self.sock = sock
        self.encoder = encoder
        self.battery = battery
        self.subscriptions = subscriptions
        self.mtu = 23
        self.encrypted = False
        self.subscribed: set[int] = set()  # value handles with notifications on
        self.report_handles: dict[int, int] = {}  # input report ID -> value handle
        self.prepared: list[tuple[int, int, bytes]] = []  # queued long write
        self.ready_event = threading.Event()
        self.gatt = self._build_gatt()
        threading.Thread(target=self._att_loop, daemon=True, name="att").start()

    @property
    def ready(self) -> bool:
        return self.ready_event.is_set()

    def wait_ready(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while self.alive.is_set() and time.monotonic() < deadline:
            if self._is_encrypted() and not self.subscribed:
                # A paired host reconnecting: restore what it had turned on.
                self.subscribed = self.subscriptions.get(self._peer())
                self._subscriptions_changed()
            if self.ready_event.wait(0.2):
                self.address = self._peer()
                return True
        return False

    def run(self):
        try:
            self._send_loop()
        finally:
            self.close()

    def _packet(self, report: bytes) -> bytes:
        # HID over GATT reports carry no report ID: the characteristic says which it is.
        return struct.pack("<BH", ATT_NOTIFY, self.report_handles[report[0]]) + report[1:]

    def send_extra(self, report: bytes):
        self._notify(self.report_handles.get(report[0], 0), report[1:])
        self._notify(self.battery_handle, bytes([self.battery().percent]))

    def _notify(self, handle: int, value: bytes):
        if handle in self.subscribed:
            try:
                self.sock.send(struct.pack("<BH", ATT_NOTIFY, handle) + value, socket.MSG_DONTWAIT)
            except (OSError, ValueError):
                pass

    def _peer(self) -> str:
        # Once a host pairs, the kernel replaces its private address with its identity
        # address, which is what it reconnects as and bluetoothd knows it by.
        try:
            return self.sock.getpeername()[0]
        except OSError:
            return self.address

    def _is_encrypted(self) -> bool:
        if not self.encrypted:
            try:
                level = self.sock.getsockopt(SOL_BLUETOOTH, BT_SECURITY, 2)[0]
            except OSError:
                return False
            self.encrypted = level >= BT_SECURITY_MEDIUM
        return self.encrypted

    # ---- database ----------------------------------------------------------------

    def _build_gatt(self) -> Gatt:
        p = self.profile
        g = Gatt()
        g.service(0x1800)  # Generic Access
        g.characteristic(0x2A00, PROP_READ, p.bt_name.encode())
        g.characteristic(0x2A01, PROP_READ, struct.pack("<H", p.appearance))
        g.characteristic(0x2A04, PROP_READ, struct.pack("<4H", *CONN_PARAMS))
        g.service(0x1801)  # Generic Attribute
        self._cccd(g, g.characteristic(0x2A05, PROP_INDICATE))  # service changed (never is)
        g.service(0x180A)  # Device Information
        g.characteristic(0x2A29, PROP_READ, p.provider.encode())
        g.characteristic(0x2A24, PROP_READ, p.service_name.encode())
        # PnP ID: vendor ID source 2 (USB), which is how hosts learn our VID/PID
        g.characteristic(0x2A50, PROP_READ, struct.pack("<BHHH", 2, p.vendor_id, p.product_id, p.version))
        g.service(0x180F)  # Battery
        level = g.characteristic(0x2A19, PROP_READ | PROP_NOTIFY, read=lambda: bytes([self.battery().percent]))
        self.battery_handle = level.handle
        self._cccd(g, level)
        g.service(0x1812)  # HID
        g.characteristic(0x2A4A, PROP_READ, bytes([0x11, 0x01, 0x00, 0x02]))  # HID 1.11, normally connectable
        g.characteristic(0x2A4B, PROP_READ, p.descriptor)
        g.characteristic(0x2A4C, PROP_WRITE_NO_RESPONSE, write=lambda v: None)  # host suspend: ignored
        g.characteristic(0x2A4E, PROP_READ | PROP_WRITE_NO_RESPONSE, b"\x01", write=lambda v: None)  # report mode
        for report_id, kind in report_ids(p.descriptor):
            if kind == REPORT_INPUT:
                attr = g.characteristic(0x2A4D, PROP_READ | PROP_NOTIFY, secure=True)
                self._cccd(g, attr)
                self.report_handles[report_id] = attr.handle
            elif kind == REPORT_OUTPUT:
                g.characteristic(0x2A4D, PROP_READ | PROP_WRITE | PROP_WRITE_NO_RESPONSE, secure=True,
                                 write=lambda v, rid=report_id: self._handle_output(b"\xA2" + bytes([rid]) + v))
            else:
                g.characteristic(0x2A4D, PROP_READ | PROP_WRITE, secure=True,
                                 read=lambda rid=report_id: self._get_feature(rid),
                                 write=lambda v, rid=report_id: self._set_feature(rid, v))
            g.descriptor(UUID_REPORT_REFERENCE, bytes([report_id, kind]))
        g.finish()
        return g

    def _cccd(self, g: Gatt, attr: Attribute):
        handle = attr.handle

        def write(value: bytes):
            if value[:1] and value[0] & 0x03:
                self.subscribed.add(handle)
            else:
                self.subscribed.discard(handle)
            self.subscriptions.set(self._peer(), self.subscribed)
            self._subscriptions_changed()

        g.descriptor(UUID_CCCD, read=lambda: struct.pack("<H", 1 if handle in self.subscribed else 0),
                     write=write, secure=attr.secure)

    def _subscriptions_changed(self):
        if self._is_encrypted() and self.subscribed & set(self.report_handles.values()):
            self.ready_event.set()
            self.notify_input()

    def _get_feature(self, report_id: int) -> bytes:
        reply = self.encoder.get_feature(report_id) or b""
        log.debug("feature 0x%02x read: %s", report_id, reply[:16].hex(" "))
        return reply

    def _set_feature(self, report_id: int, value: bytes):
        log.debug("feature 0x%02x write: %s", report_id, value[:16].hex(" "))
        self.encoder.set_feature(report_id, value)

    # ---- ATT server ----------------------------------------------------------------

    def _att_loop(self):
        try:
            while self.alive.is_set():
                pdu = self.sock.recv(SERVER_MTU)
                if not pdu:
                    break
                reply = self._handle_att(pdu)
                if reply:
                    self.sock.send(reply)
        except OSError:
            pass
        self.alive.clear()
        self.notify_input()

    def _handle_att(self, pdu: bytes) -> bytes | None:
        op = pdu[0]
        try:
            handler = self._HANDLERS.get(op)
            if handler:
                return handler(self, pdu)
            if op == ATT_WRITE_CMD:
                attr = self.gatt.get(struct.unpack_from("<H", pdu, 1)[0])
                self._check(attr, writing=True)
                attr.write(pdu[3:])
                return None
            if op & 0x40 or op == 0x1E:  # other commands, and confirmations: no reply
                return None
            return self._error(op, 0, ERR_REQUEST_NOT_SUPPORTED)
        except AttError as e:
            return None if op & 0x40 else self._error(op, e.handle, e.code)
        except (struct.error, IndexError):
            return None if op & 0x40 else self._error(op, 0, ERR_INVALID_PDU)

    @staticmethod
    def _error(op: int, handle: int, code: int) -> bytes:
        return struct.pack("<BBHB", ATT_ERROR, op, handle, code)

    def _check(self, attr: Attribute, writing: bool = False):
        if writing and attr.write is None:
            raise AttError(attr.handle, ERR_WRITE_NOT_PERMITTED)
        if not writing and not attr.readable:
            raise AttError(attr.handle, ERR_READ_NOT_PERMITTED)
        if attr.secure and not self._is_encrypted():
            # The host pairs (or re-encrypts with its stored key) and retries.
            raise AttError(attr.handle, ERR_INSUFFICIENT_AUTHENTICATION)

    def _mtu(self, pdu: bytes) -> bytes:
        self.mtu = max(23, min(SERVER_MTU, struct.unpack_from("<H", pdu, 1)[0]))
        return struct.pack("<BH", ATT_MTU_RSP, SERVER_MTU)

    def _find_info(self, pdu: bytes) -> bytes:
        start, end = struct.unpack_from("<HH", pdu, 1)
        attrs = self.gatt.range(start, end)[:(self.mtu - 2) // 4]
        if not attrs:
            raise AttError(start, ERR_ATTRIBUTE_NOT_FOUND)
        return bytes([ATT_FIND_INFO_RSP, 1]) + b"".join(struct.pack("<HH", a.handle, a.type) for a in attrs)

    def _find_by_type(self, pdu: bytes) -> bytes:
        start, end, uuid = struct.unpack_from("<HHH", pdu, 1)
        found = [a for a in self.gatt.range(start, end) if a.type == uuid == UUID_PRIMARY_SERVICE
                 and a.value == pdu[7:]][:(self.mtu - 1) // 4]
        if not found:
            raise AttError(start, ERR_ATTRIBUTE_NOT_FOUND)
        return bytes([ATT_FIND_BY_TYPE_RSP]) + b"".join(struct.pack("<HH", a.handle, a.end) for a in found)

    def _read_by_type(self, pdu: bytes) -> bytes:
        start, end = struct.unpack_from("<HH", pdu, 1)
        uuid = _uuid16(pdu[5:])
        entries = []
        for attr in self.gatt.range(start, end):
            if attr.type != uuid:
                continue
            try:
                self._check(attr)
            except AttError:
                if entries:
                    break
                raise
            value = attr.get()[:min(253, self.mtu - 4)]
            entry = struct.pack("<H", attr.handle) + value
            if entries and (len(entry) != len(entries[0]) or 2 + len(entry) * (len(entries) + 1) > self.mtu):
                break
            entries.append(entry)
        if not entries:
            raise AttError(start, ERR_ATTRIBUTE_NOT_FOUND)
        return bytes([ATT_READ_BY_TYPE_RSP, len(entries[0])]) + b"".join(entries)

    def _read(self, pdu: bytes) -> bytes:
        attr = self.gatt.get(struct.unpack_from("<H", pdu, 1)[0])
        self._check(attr)
        return bytes([ATT_READ_RSP]) + attr.get()[:self.mtu - 1]

    def _read_blob(self, pdu: bytes) -> bytes:
        handle, offset = struct.unpack_from("<HH", pdu, 1)
        attr = self.gatt.get(handle)
        self._check(attr)
        value = attr.get()
        if offset > len(value):
            raise AttError(handle, ERR_INVALID_OFFSET)
        return bytes([ATT_READ_BLOB_RSP]) + value[offset:offset + self.mtu - 1]

    def _read_by_group(self, pdu: bytes) -> bytes:
        start, end = struct.unpack_from("<HH", pdu, 1)
        uuid = _uuid16(pdu[5:])
        if uuid not in (UUID_PRIMARY_SERVICE, 0x2801):
            raise AttError(start, ERR_UNSUPPORTED_GROUP_TYPE)
        found = [a for a in self.gatt.range(start, end) if a.type == uuid][:(self.mtu - 2) // 6]
        if not found:
            raise AttError(start, ERR_ATTRIBUTE_NOT_FOUND)
        return (bytes([ATT_READ_BY_GROUP_RSP, 6])
                + b"".join(struct.pack("<HH", a.handle, a.end) + a.value for a in found))

    def _write(self, pdu: bytes) -> bytes:
        attr = self.gatt.get(struct.unpack_from("<H", pdu, 1)[0])
        self._check(attr, writing=True)
        attr.write(pdu[3:])
        return bytes([ATT_WRITE_RSP])

    def _prepare_write(self, pdu: bytes) -> bytes:
        handle, offset = struct.unpack_from("<HH", pdu, 1)
        self._check(self.gatt.get(handle), writing=True)
        self.prepared.append((handle, offset, pdu[5:]))
        return bytes([ATT_PREPARE_WRITE_REQ + 1]) + pdu[1:]

    def _execute_write(self, pdu: bytes) -> bytes:
        prepared, self.prepared = self.prepared, []
        if pdu[1]:  # 0 cancels
            values: dict[int, bytearray] = {}
            for handle, offset, part in prepared:
                value = values.setdefault(handle, bytearray())
                if offset != len(value):
                    raise AttError(handle, ERR_INVALID_OFFSET)
                value += part
            for handle, value in values.items():
                self.gatt.get(handle).write(bytes(value))
        return bytes([ATT_EXECUTE_WRITE_RSP])

    _HANDLERS = {
        ATT_MTU_REQ: _mtu, ATT_FIND_INFO_REQ: _find_info, ATT_FIND_BY_TYPE_REQ: _find_by_type,
        ATT_READ_BY_TYPE_REQ: _read_by_type, ATT_READ_REQ: _read, ATT_READ_BLOB_REQ: _read_blob,
        ATT_READ_BY_GROUP_REQ: _read_by_group, ATT_WRITE_REQ: _write,
        ATT_PREPARE_WRITE_REQ: _prepare_write, ATT_EXECUTE_WRITE_REQ: _execute_write,
    }
