"""Keep the Deck from auto-suspending while it is someone's controller.

We take the Deck's controller away from Steam, so Steam sees no input at all and
its idle timer suspends the Deck mid-session, dropping the host. Steam's suspend
goes through logind, so a sleep inhibitor stops it.

The inhibitor would also stop a deliberate suspend: in Game Mode the power button
then only blanks the screen. So we watch the power button and let go as soon as
it is pressed, before Steam asks to suspend. (Same approach as mcnearcj/deck-awake.)
"""
import glob
import logging
import os
import select
import struct
import threading

import dbus

log = logging.getLogger("sdcd.awake")

KEY_POWER, EV_KEY = 116, 1
INPUT_EVENT = struct.Struct("llHHi")  # struct input_event on 64-bit


class StayAwake:
    def __init__(self):
        self.lock = threading.Lock()
        self.fd: int | None = None

    def hold(self):
        with self.lock:
            if self.fd is not None:
                return
            try:
                login = dbus.Interface(dbus.SystemBus().get_object("org.freedesktop.login1", "/org/freedesktop/login1"),
                                       "org.freedesktop.login1.Manager")
                self.fd = login.Inhibit("sleep", "Deck as Controller",
                                        "The Deck is being used as a controller", "block").take()
            except dbus.DBusException as e:
                log.warning("could not keep the Deck awake: %s", e)

    def release(self):
        with self.lock:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None

    def watch_power_button(self):
        """Release the inhibitor whenever the power button is pressed. Runs forever."""
        fds = []
        for event in glob.glob("/sys/class/input/event*"):
            try:
                with open(f"{event}/device/name") as f:
                    if "Power Button" not in f.read():
                        continue
                fds.append(os.open(f"/dev/input/{os.path.basename(event)}", os.O_RDONLY))
            except OSError:
                continue
        if not fds:
            log.warning("no power button found; suspending from the power button needs the plugin off")
            return
        while True:
            ready, _, _ = select.select(fds, [], [])
            for fd in ready:
                data = os.read(fd, INPUT_EVENT.size * 16)
                for i in range(0, len(data) - INPUT_EVENT.size + 1, INPUT_EVENT.size):
                    _, _, kind, code, value = INPUT_EVENT.unpack_from(data, i)
                    if kind == EV_KEY and code == KEY_POWER and value == 1 and self.fd is not None:
                        log.info("power button pressed: letting the Deck suspend")
                        self.release()
