"""Entry point: python3 -m sdcd --settings-dir DIR [--options JSON] | --restore"""
import argparse
import json
import logging
import os
import sys

parser = argparse.ArgumentParser(prog="sdcd")
parser.add_argument("--settings-dir", default="/tmp/sdcd")
parser.add_argument("--options", default="{}", help="initial options as JSON")
parser.add_argument("--restore", action="store_true",
                    help="undo everything controller mode changes (bluetoothd, controller)")
args = parser.parse_args()

# Touch /run/sdcd-debug on the Deck to log host output reports and control messages.
level = logging.DEBUG if os.path.exists("/run/sdcd-debug") else logging.INFO
logging.basicConfig(stream=sys.stderr, level=level, format="%(name)s: %(message)s")

if args.restore:
    from . import ble, bluez, deck
    deck.rebind_all()
    ble.leave_le_mode()
    bluez.restore_stock()
else:
    from .daemon import Daemon
    Daemon(args.settings_dir, json.loads(args.options)).run()
