"""Deck display power via the backlight class device.

The original brightness is saved under /run so restore() works even after a crash.
"""
import glob
import os

SAVED = "/run/sd-controller-screen"
FB_BLANK_UNBLANK, FB_BLANK_POWERDOWN = 0, 4


def _backlight() -> str | None:
    devices = sorted(glob.glob("/sys/class/backlight/*"))
    return devices[0] if devices else None


def _write(path: str, value: int) -> bool:
    try:
        with open(path, "w") as f:
            f.write(str(value))
        return True
    except OSError:
        return False


def is_off() -> bool:
    return os.path.exists(SAVED)


def turn_off():
    bl = _backlight()
    if not bl or is_off():
        return
    with open(f"{bl}/brightness") as f:
        brightness = int(f.read())
    with open(SAVED, "w") as f:
        f.write(str(brightness))
    _write(f"{bl}/brightness", 0)
    _write(f"{bl}/bl_power", FB_BLANK_POWERDOWN)


def restore():
    bl = _backlight()
    if not bl or not is_off():
        return
    with open(SAVED) as f:
        brightness = int(f.read())
    _write(f"{bl}/bl_power", FB_BLANK_UNBLANK)
    _write(f"{bl}/brightness", brightness)
    os.remove(SAVED)


def toggle():
    restore() if is_off() else turn_off()
