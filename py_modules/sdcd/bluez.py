"""BlueZ setup: run bluetoothd as a gamepad, register the HID profile and a pairing agent.

bluetoothd is restarted with a runtime-only drop-in (under /run), so a reboot or
restore_stock() returns the Deck to its normal Bluetooth configuration.
"""
import json
import os
import shutil
import subprocess
import time

import dbus
import dbus.service

from .profiles import Profile

HID_UUID = "00001124-0000-1000-8000-00805f9b34fb"
GAMEPAD_CLASS = "0x002508"

RUN_DIR = "/run/sd-controller"
DROPIN_DIR = "/run/systemd/system/bluetooth.service.d"
DROPIN = f"{DROPIN_DIR}/sd-controller.conf"
BLUETOOTHD = "/usr/lib/bluetooth/bluetoothd"

# Stock adapter settings we change, saved so restore_stock() can put them back.
SAVED_ADAPTER = "/run/sd-controller-adapter.json"
SAVED_PROPS = ("Pairable", "PairableTimeout", "Discoverable", "DiscoverableTimeout")

PROFILE_PATH = "/sdc/profile"
AGENT_PATH = "/sdc/agent"


def gamepad_mode_active() -> bool:
    return os.path.exists(DROPIN)


def _adapter_props() -> dbus.Interface:
    return dbus.Interface(dbus.SystemBus().get_object("org.bluez", "/org/bluez/hci0"),
                          "org.freedesktop.DBus.Properties")


def set_adapter_prop(props: dbus.Interface, name: str, value, attempts: int = 25):
    """Set an Adapter1 property, retrying while bluetoothd is still settling (Busy)."""
    for i in range(attempts):
        try:
            props.Set("org.bluez.Adapter1", name, value)
            return
        except dbus.DBusException as e:
            if i == attempts - 1 or e.get_dbus_name() not in (
                    "org.bluez.Error.Busy", "org.bluez.Error.InProgress"):
                raise
            time.sleep(0.2)


def enter_gamepad_mode(profile: Profile):
    """Restart bluetoothd without the input/hostname plugins and with the profile's identity."""
    if not gamepad_mode_active() and not os.path.exists(SAVED_ADAPTER):
        props = _adapter_props()
        saved = {name: props.Get("org.bluez.Adapter1", name) for name in SAVED_PROPS}
        with open(SAVED_ADAPTER, "w") as f:
            json.dump({k: (bool(v) if isinstance(v, dbus.Boolean) else int(v)) for k, v in saved.items()}, f)
    os.makedirs(RUN_DIR, exist_ok=True)
    os.makedirs(DROPIN_DIR, exist_ok=True)
    with open("/etc/bluetooth/main.conf") as f:
        conf = f.read()
    identity = (f"[General]\nClass = {GAMEPAD_CLASS}\n"
                f"DeviceID = usb:{profile.vendor_id:04X}:{profile.product_id:04X}:{profile.version:04X}\n"
                f"Name = {profile.bt_name}\n")
    conf = conf.replace("[General]\n", identity, 1) if "[General]\n" in conf else identity + conf
    with open(f"{RUN_DIR}/main.conf", "w") as f:
        f.write(conf)
    # -P input: frees L2CAP PSMs 0x11/0x13 so we can serve HID ourselves.
    # -P hostname: stops it overriding our Class/Name from the chassis type.
    with open(DROPIN, "w") as f:
        f.write("[Service]\nExecStart=\n"
                f"ExecStart={BLUETOOTHD} -P input,hostname -f {RUN_DIR}/main.conf\n")
    _restart_bluetoothd()


def restore_stock():
    """Undo enter_gamepad_mode(). Safe to call when not active."""
    if gamepad_mode_active():
        os.remove(DROPIN)
        shutil.rmtree(RUN_DIR, ignore_errors=True)
        _restart_bluetoothd()
    if os.path.exists(SAVED_ADAPTER):
        with open(SAVED_ADAPTER) as f:
            saved = json.load(f)
        props = _adapter_props()
        for name in SAVED_PROPS:
            if name in saved:
                value = saved[name] if isinstance(saved[name], bool) else dbus.UInt32(saved[name])
                set_adapter_prop(props, name, value)
        os.remove(SAVED_ADAPTER)


def _restart_bluetoothd():
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "restart", "bluetooth"], check=True)
    # Wait for the adapter to come back on D-Bus.
    bus = dbus.SystemBus()
    for _ in range(50):
        try:
            props = dbus.Interface(bus.get_object("org.bluez", "/org/bluez/hci0"),
                                   "org.freedesktop.DBus.Properties")
            if props.Get("org.bluez.Adapter1", "Powered"):
                return
            props.Set("org.bluez.Adapter1", "Powered", True)
        except dbus.DBusException:
            pass
        time.sleep(0.2)
    raise RuntimeError("Bluetooth adapter did not come back after restarting bluetoothd")


class _Profile(dbus.service.Object):
    @dbus.service.method("org.bluez.Profile1", in_signature="", out_signature="")
    def Release(self):
        pass

    @dbus.service.method("org.bluez.Profile1", in_signature="oha{sv}", out_signature="")
    def NewConnection(self, device, fd, props):
        pass

    @dbus.service.method("org.bluez.Profile1", in_signature="o", out_signature="")
    def RequestDisconnection(self, device):
        pass


class _Agent(dbus.service.Object):
    """Accepts pairing (Just Works) only while the pairing window is open."""

    def __init__(self, bus, path, adapter):
        super().__init__(bus, path)
        self.adapter = adapter

    def _check(self):
        if not self.adapter.pairing_open():
            raise dbus.DBusException("Pairing window closed", name="org.bluez.Error.Rejected")

    @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
    def Release(self):
        pass

    @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="")
    def AuthorizeService(self, device, uuid):
        pass

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="s")
    def RequestPinCode(self, device):
        self._check()
        return "0000"

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="u")
    def RequestPasskey(self, device):
        self._check()
        return dbus.UInt32(0)

    @dbus.service.method("org.bluez.Agent1", in_signature="ouq", out_signature="")
    def DisplayPasskey(self, device, passkey, entered):
        pass

    @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="")
    def DisplayPinCode(self, device, pincode):
        pass

    @dbus.service.method("org.bluez.Agent1", in_signature="ou", out_signature="")
    def RequestConfirmation(self, device, passkey):
        self._check()

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="")
    def RequestAuthorization(self, device):
        self._check()

    @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
    def Cancel(self):
        pass


class Adapter:
    """The Deck's Bluetooth adapter while acting as a gamepad. Needs a GLib main loop."""

    def __init__(self, bus, profile: Profile):
        self.bus = bus
        self.pairing_until = 0.0
        obj = bus.get_object("org.bluez", "/org/bluez/hci0")
        self.props = dbus.Interface(obj, "org.freedesktop.DBus.Properties")
        self.address = str(self.props.Get("org.bluez.Adapter1", "Address"))

        _Profile(bus, PROFILE_PATH)
        _Agent(bus, AGENT_PATH, self)
        bluez = bus.get_object("org.bluez", "/org/bluez")
        dbus.Interface(bluez, "org.bluez.ProfileManager1").RegisterProfile(
            PROFILE_PATH, HID_UUID, {
                "ServiceRecord": profile.sdp_record(),
                "Role": "server",
                "RequireAuthentication": False,
                "RequireAuthorization": False,
                "AutoConnect": False,
            })
        agents = dbus.Interface(bluez, "org.bluez.AgentManager1")
        agents.RegisterAgent(AGENT_PATH, "NoInputNoOutput")
        agents.RequestDefaultAgent(AGENT_PATH)
        # Stored alias would override the Name from main.conf.
        self._set("Alias", "")
        self.close_pairing()

    @property
    def mac_bytes(self) -> bytes:
        return bytes(int(b, 16) for b in self.address.split(":"))

    def open_pairing(self, seconds: int):
        self.pairing_until = time.monotonic() + seconds
        self._set("Pairable", True)
        self._set("DiscoverableTimeout", dbus.UInt32(seconds))
        self._set("Discoverable", True)

    def close_pairing(self):
        self.pairing_until = 0.0
        self._set("Discoverable", False)
        self._set("Pairable", False)

    def pairing_open(self) -> bool:
        return time.monotonic() < self.pairing_until

    def remove_device(self, address: str):
        """Drop the pairing (link key) for `address`, if any."""
        path = "/org/bluez/hci0/dev_" + address.upper().replace(":", "_")
        adapter = dbus.Interface(self.bus.get_object("org.bluez", "/org/bluez/hci0"), "org.bluez.Adapter1")
        try:
            adapter.RemoveDevice(path)
        except dbus.DBusException:
            pass

    def is_paired(self, address: str) -> bool:
        path = "/org/bluez/hci0/dev_" + address.upper().replace(":", "_")
        try:
            props = dbus.Interface(self.bus.get_object("org.bluez", path), "org.freedesktop.DBus.Properties")
            return bool(props.Get("org.bluez.Device1", "Paired"))
        except dbus.DBusException:
            return False

    def device_name(self, address: str) -> str:
        path = "/org/bluez/hci0/dev_" + address.upper().replace(":", "_")
        try:
            props = dbus.Interface(self.bus.get_object("org.bluez", path), "org.freedesktop.DBus.Properties")
            return str(props.Get("org.bluez.Device1", "Alias"))
        except dbus.DBusException:
            return address

    def _set(self, name, value):
        set_adapter_prop(self.props, name, value)
