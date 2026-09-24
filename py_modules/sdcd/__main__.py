"""Entry point: python3 -m sdcd --settings-dir DIR [--options JSON] | --restore"""
import argparse
import json
import logging
import sys

parser = argparse.ArgumentParser(prog="sdcd")
parser.add_argument("--settings-dir", default="/tmp/sdcd")
parser.add_argument("--options", default="{}", help="initial options as JSON")
parser.add_argument("--restore", action="store_true",
                    help="undo everything controller mode changes (bluetoothd, controller, screen)")
args = parser.parse_args()

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(name)s: %(message)s")

if args.restore:
    from . import bluez, deck, screen
    screen.restore()
    deck.rebind_all()
    bluez.restore_stock()
else:
    from .daemon import Daemon
    Daemon(args.settings_dir, json.loads(args.options)).run()
