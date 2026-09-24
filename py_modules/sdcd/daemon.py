"""Controller-mode daemon: makes the Deck a Bluetooth DualSense for a paired host.

Talks to the Decky plugin over stdio using JSON lines:
  stdin  commands: {"cmd": "pair"} | {"cmd": "stop"} | {"cmd": "screen"}
                   {"cmd": "options", "screen_off": bool, "deadzone": float}
  stdout events:   {"type": "state", ...} | {"type": "stopped", "reason": str}
"""
import json
import logging
import os
import signal
import socket
import sys
import threading
import time

import dbus
import dbus.mainloop.glib
from gi.repository import GLib

from . import bluez, hid, screen
from .deck import DeckController, DeckInput, rebind_all
from .dualsense import InputEncoder

log = logging.getLogger("sdcd")

PAIRING_SECONDS = 180
RECONNECT_INTERVAL = 5.0
QAM_TAP_MAX = 0.6  # seconds: tap ⋯ toggles the screen
QAM_HOLD_STOP = 2.0  # seconds: hold ⋯ stops controller mode


class Hosts:
    """Remembered hosts, most recent first."""

    def __init__(self, settings_dir: str):
        self.path = os.path.join(settings_dir, "hosts.json")
        try:
            with open(self.path) as f:
                self.items = json.load(f)
        except (OSError, ValueError):
            self.items = []

    def remember(self, address: str, name: str):
        self.items = [{"address": address, "name": name}] + [h for h in self.items if h["address"] != address]
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(self.items, f)

    def forget(self, address: str):
        self.items = [h for h in self.items if h["address"] != address]
        with open(self.path, "w") as f:
            json.dump(self.items, f)


class Daemon:
    def __init__(self, settings_dir: str, options: dict):
        self.options = {"screen_off": True, "deadzone": 0.08, **options}
        self.hosts = Hosts(settings_dir)
        self.loop = GLib.MainLoop()
        self.out_lock = threading.Lock()
        self.link_lock = threading.Lock()
        self.link: hid.Link | None = None
        self.encoder: InputEncoder | None = None
        self.session_thread: threading.Thread | None = None
        self.link_name = ""
        self.stopping = threading.Event()
        self.stop_reason = "stopped"
        self.adapter: bluez.Adapter | None = None
        self.last_state: dict | None = None

    # ---- IPC -------------------------------------------------------------

    def emit(self, **event):
        with self.out_lock:
            sys.stdout.write(json.dumps(event) + "\n")
            sys.stdout.flush()

    def emit_state(self):
        if self.link:
            state = "connected"
        elif self.adapter and self.adapter.pairing_open():
            state = "pairing"
        else:
            state = "waiting"
        event = dict(type="state", state=state, host=self.link_name,
                     hosts=self.hosts.items, screen_off=screen.is_off(), options=self.options)
        if event != self.last_state:
            self.last_state = json.loads(json.dumps(event))  # deep copy
            self.emit(**event)

    def _stdin_loop(self):
        for line in sys.stdin:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            cmd = msg.get("cmd")
            if cmd == "stop":
                self.stop("stopped")
            elif cmd == "pair":
                GLib.idle_add(self._open_pairing)
            elif cmd == "screen":
                if self.link:
                    screen.toggle()
                self.emit_state()
            elif cmd == "options":
                self.options.update({k: v for k, v in msg.items() if k in ("screen_off", "deadzone")})
                if self.encoder:
                    self.encoder.deadzone = self.options["deadzone"]
                self.emit_state()
        self.stop("plugin went away")

    # ---- lifecycle -------------------------------------------------------

    def run(self):
        dbus.mainloop.glib.threads_init()
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        signal.signal(signal.SIGTERM, lambda *_: self.stop("terminated"))
        self.emit(type="state", state="starting", host="", hosts=self.hosts.items,
                  screen_off=False, options=self.options)
        try:
            bluez.enter_gamepad_mode()
            self.adapter = bluez.Adapter(dbus.SystemBus())
            if not self.hosts.items:
                self.adapter.open_pairing(PAIRING_SECONDS)
            for target in (self._stdin_loop, self._listen_loop, self._reconnect_loop):
                threading.Thread(target=target, daemon=True, name=target.__name__).start()
            GLib.timeout_add_seconds(2, self._tick)
            self.emit_state()
            self.loop.run()
        except Exception as e:
            log.exception("daemon failed")
            self.stop_reason = f"error: {e}"
        finally:
            self._cleanup()
            self.emit(type="stopped", reason=self.stop_reason)

    def stop(self, reason: str):
        if self.stopping.is_set():
            return
        self.stop_reason = reason
        self.stopping.set()
        if self.link:
            self.link.close()
        GLib.idle_add(self.loop.quit)

    def _cleanup(self):
        if self.link:
            self.link.close()
        if self.session_thread:
            self.session_thread.join(timeout=3)
        rebind_all()
        screen.restore()
        try:
            bluez.restore_stock()
        except Exception:
            log.exception("restoring bluetoothd failed")

    def _tick(self):
        """Periodic: close an expired pairing window and refresh the UI state."""
        if self.adapter and self.adapter.pairing_until and not self.adapter.pairing_open():
            self.adapter.close_pairing()
        self.emit_state()
        return True

    def _open_pairing(self):
        self.adapter.open_pairing(PAIRING_SECONDS)
        self.emit_state()
        return False

    # ---- connections -----------------------------------------------------

    def _listen_loop(self):
        """Hosts connecting to us: first pairing, or a host that reconnects itself."""
        ctrl_srv, intr_srv = hid.listen(hid.PSM_CTRL), hid.listen(hid.PSM_INTR)
        while not self.stopping.is_set():
            ctrl, (address, _) = ctrl_srv.accept()
            intr_srv.settimeout(10)
            try:
                intr, (address2, _) = intr_srv.accept()
            except socket.timeout:
                ctrl.close()
                continue
            if address2 != address:
                ctrl.close()
                intr.close()
                continue
            self._start_session(ctrl, intr, address)

    def _reconnect_loop(self):
        """Reconnect to the most recent host while not connected (like a real controller)."""
        while not self.stopping.wait(RECONNECT_INTERVAL):
            if self.link or not self.hosts.items or self.adapter.pairing_open():
                continue
            address = self.hosts.items[0]["address"]
            try:
                ctrl, intr = hid.connect(address)
            except OSError:
                continue
            self._start_session(ctrl, intr, address)

    def _start_session(self, ctrl, intr, address: str):
        with self.link_lock:
            if self.link or self.stopping.is_set():
                ctrl.close()
                intr.close()
                return
            deck = DeckController()
            encoder = InputEncoder(self.options["deadzone"])
            latest: list[DeckInput | None] = [None]
            link = hid.Link(ctrl, intr, address, self.adapter.mac_bytes,
                            get_report=lambda: encoder.encode(latest[0]),
                            on_rumble=lambda low, high: _safe(deck.rumble, low, high))
            self.link = link
            self.encoder = encoder
            self.session_thread = threading.Thread(target=self._session, args=(link, deck, latest),
                                                   daemon=True, name="session")
            self.session_thread.start()

    def _session(self, link: hid.Link, deck: DeckController, latest: list):
        name = self.adapter.device_name(link.address)
        self.link_name = name
        self.hosts.remember(link.address, name)
        GLib.idle_add(self.adapter.close_pairing)
        log.info("connected to %s (%s)", name, link.address)
        try:
            deck.grab()
        except Exception as e:
            log.exception("could not grab the Deck controller")
            self.emit(type="error", message=f"Could not take over the Deck controller: {e}")
            link.close()
        else:
            if self.options["screen_off"]:
                screen.turn_off()
            self.emit_state()
            threading.Thread(target=self._read_deck, args=(link, deck, latest), daemon=True,
                             name="deck-reader").start()
            link.run()
        finally:
            deck.release()
            screen.restore()
            with self.link_lock:
                self.link = None
                self.encoder = None
                self.link_name = ""
            log.info("disconnected from %s", name)
            self.emit_state()

    def _read_deck(self, link: hid.Link, deck: DeckController, latest: list):
        qam_down_at = None
        last_config = time.monotonic()
        while link.alive.is_set():
            try:
                raw = deck.read()
            except OSError as e:
                log.warning("Deck controller read failed: %s", e)
                link.close()
                return
            now = time.monotonic()
            if now - last_config > 5:  # re-assert settings in case the controller reset
                _safe(deck.configure)
                last_config = now
            state = DeckInput.parse(raw) if raw else None
            if state is None:
                continue
            latest[0] = state
            link.notify_input()

            # ⋯ (QAM) is reserved for us: tap toggles the screen, hold stops.
            if state.qam and qam_down_at is None:
                qam_down_at = now
            elif state.qam and now - qam_down_at >= QAM_HOLD_STOP:
                _safe(deck.rumble, 0, 200)
                time.sleep(0.15)
                self.stop("stopped from the Deck")
                return
            elif not state.qam and qam_down_at is not None:
                if now - qam_down_at <= QAM_TAP_MAX:
                    screen.toggle()
                    self.emit_state()
                qam_down_at = None


def _safe(fn, *args):
    try:
        fn(*args)
    except OSError as e:
        log.debug("%s failed: %s", getattr(fn, "__name__", fn), e)
