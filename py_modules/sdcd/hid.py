"""Bluetooth HID transport: L2CAP control/interrupt channels to one host."""
import fcntl
import logging
import socket
import struct
import termios
import threading
import time
from typing import Callable

from . import dualsense

log = logging.getLogger("sdcd.hid")

PSM_CTRL, PSM_INTR = 0x11, 0x13
SOL_BLUETOOTH, BT_SECURITY, BT_SECURITY_MEDIUM = 274, 4, 2

# Reports that only carry new IMU data are rate limited so they never crowd out
# button/stick changes, which are sent as soon as the link can take them.
IMU_INTERVAL = 0.012
# Reports allowed to sit unacknowledged in the socket. More than this and the
# host sees stale input, so we wait and send the newest state instead.
MAX_IN_FLIGHT = 2


def l2cap_socket() -> socket.socket:
    return socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)


def listen(psm: int) -> socket.socket:
    s = l2cap_socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((socket.BDADDR_ANY, psm))
    s.listen(1)
    return s


def connect(address: str, timeout: float = 8.0) -> tuple[socket.socket, socket.socket]:
    """Reconnect to a paired host (HID device-initiated reconnection)."""
    socks = []
    try:
        for psm in (PSM_CTRL, PSM_INTR):
            s = l2cap_socket()
            socks.append(s)
            s.setsockopt(SOL_BLUETOOTH, BT_SECURITY, struct.pack("BB", BT_SECURITY_MEDIUM, 0))
            s.settimeout(timeout)
            s.connect((address, psm))
            s.settimeout(None)
        return socks[0], socks[1]
    except OSError:
        for s in socks:
            s.close()
        raise


def _queued_bytes(sock: socket.socket, sndbuf: int) -> int:
    # TIOCOUTQ on Bluetooth sockets reports free send space, not queued bytes.
    free = struct.unpack("i", fcntl.ioctl(sock, termios.TIOCOUTQ, b"\0" * 4))[0]
    return sndbuf - free


class Link:
    """One connected host. run() blocks until the host disconnects or close() is called."""

    def __init__(self, ctrl: socket.socket, intr: socket.socket, address: str, mac: bytes,
                 get_report: Callable[[], bytes], has_urgent: Callable[[], bool],
                 on_rumble: Callable[[int, int], None]):
        self.ctrl, self.intr, self.address, self.mac = ctrl, intr, address, mac
        self.get_report = get_report
        self.has_urgent = has_urgent
        self.on_rumble = on_rumble
        self.alive = threading.Event()
        self.alive.set()  # set before any helper thread starts watching it
        self.new_input = threading.Condition()
        self.sent = self.skipped = 0
        self.unplugged = False  # host removed the pairing (virtual cable unplug)
        self.closed_by_host = False  # host disconnected on purpose (vs. link loss)

    def notify_input(self):
        with self.new_input:
            self.new_input.notify()

    def close(self):
        self.alive.clear()
        self.notify_input()
        for s in (self.ctrl, self.intr):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()

    def run(self):
        threading.Thread(target=self._control_loop, daemon=True, name="hid-ctrl").start()
        threading.Thread(target=self._interrupt_rx, daemon=True, name="hid-intr-rx").start()
        try:
            self._send_loop()
        finally:
            self.close()

    def _send_loop(self):
        try:
            sndbuf = self.intr.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF)
            per_report = None
            last_send = 0.0
            while self.alive.is_set():
                imu_due = time.monotonic() - last_send >= IMU_INTERVAL
                if not (imu_due or self.has_urgent()):
                    with self.new_input:
                        self.new_input.wait(timeout=IMU_INTERVAL)
                    continue
                queued = _queued_bytes(self.intr, sndbuf)
                if per_report and queued >= per_report * MAX_IN_FLIGHT:
                    self.skipped += 1
                    time.sleep(0.0005)  # poll for the controller's ack
                    continue
                try:
                    self.intr.send(b"\xA1" + self.get_report(), socket.MSG_DONTWAIT)
                except BlockingIOError:
                    self.skipped += 1
                    time.sleep(0.0005)
                    continue
                if per_report is None:
                    per_report = max(1, _queued_bytes(self.intr, sndbuf) - queued)
                last_send = time.monotonic()
                self.sent += 1
        except (OSError, ValueError) as e:  # ValueError: socket closed under us
            if self.alive.is_set():
                log.info("interrupt channel closed: %s", e)

    def _control_loop(self):
        try:
            while self.alive.is_set():
                msg = self.ctrl.recv(1024)
                if not msg:
                    self._host_closed()
                    break
                self.ctrl.send(self._handle_control(msg))
        except OSError:
            pass
        self.alive.clear()
        self.notify_input()

    def _host_closed(self):
        # An orderly close while we still want the link means the host disconnected us.
        if self.alive.is_set():
            self.closed_by_host = True

    def _handle_control(self, msg: bytes) -> bytes:
        kind, param = msg[0] >> 4, msg[0] & 0x0F
        if kind == 0x4:  # GET_REPORT
            rtype, rid = param & 0x3, msg[1] if len(msg) > 1 else 0
            report = dualsense.feature_report(rid, self.mac) if rtype == 3 else None
            return b"\xA3" + report if report else b"\x02"  # DATA | ERR_INVALID_REPORT_ID
        if kind == 0x5:  # SET_REPORT
            return b"\x00"
        if kind == 0x6:  # GET_PROTOCOL: report protocol
            return b"\xA0\x01"
        if kind == 0x7:  # SET_PROTOCOL
            return b"\x00"
        if kind == 0x1 and param == 0x5:  # HID_CONTROL: virtual cable unplug
            self.unplugged = True
            self.alive.clear()
            return b"\x00"
        return b"\x03"  # ERR_UNSUPPORTED_REQUEST

    def _interrupt_rx(self):
        last = None
        logged = set()
        try:
            while self.alive.is_set():
                msg = self.intr.recv(1024)
                if not msg:
                    self._host_closed()
                    break
                header = msg[:9]
                if header not in logged and len(logged) < 40:  # diagnostics: each distinct header once
                    logged.add(header)
                    log.debug("output report %dB: %s", len(msg), msg[:16].hex(" "))
                rumble = dualsense.parse_rumble(msg)
                if rumble is not None and rumble != last:
                    last = rumble
                    self.on_rumble(*rumble)
        except OSError:
            pass
        self.alive.clear()
        self.notify_input()
