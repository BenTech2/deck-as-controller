# Deck as Controller

A [Decky Loader](https://decky.xyz) plugin that turns your Steam Deck into a wireless Bluetooth
game controller for a Mac, PC, iPad or anything else that supports PS5 or Xbox controllers.
Nothing needs to be installed on the other device: the Deck simply appears as a controller.

- **Controller types:** PS5 (DualSense), PS5 Edge, or Xbox Elite Series 2. The Edge and Elite
  types carry all four Deck back buttons.
- **Low latency:** input changes are sent immediately, and presses are never dropped.
- **Deck screen off** while connected, with the screen and controls handed back when you stop.
- **Rumble**, **battery level** on the host, **gyro** (PS5 types), and both trackpads as the PS5
  touchpad (left and right halves).
- **Reconnects automatically** to the last device, and remembers several devices.

## Install

1. Install [Decky Loader](https://decky.xyz) on your Deck.
2. Download `deck-as-controller.zip` from the releases page.
3. In Game Mode, open **⋯ → Decky → ⚙ Settings → General**, turn on **Developer mode**, then go
   to **Developer → Install Plugin from ZIP File** and pick the zip.

## Use

1. Open **⋯ → Decky → Deck as Controller**, choose a **controller type**, and turn on
   **Use as controller**.
2. The first time, the Deck is ready to pair. On your Mac/PC/iPad, open Bluetooth settings and
   connect to the controller that appears (e.g. "DualSense Wireless Controller").
3. After that, turning the plugin on reconnects to the device automatically.

While connected, the Deck's controls go to your device:

| On the Deck | Does |
|---|---|
| Tap **⋯** (or tap the black screen) | Turn the Deck screen on or off |
| Hold **⋯** for 2 seconds | Stop and give the Deck its controls back |

Each controller type is paired separately, because your device remembers what kind of
controller the Deck is. To switch types, remove the Deck from your device's Bluetooth settings,
choose the new type, tap **Pair a new device**, and pair again.

### Button mapping

| Deck | PS5 / PS5 Edge | Xbox Elite |
|---|---|---|
| A B X Y | ✕ ○ □ △ | A B X Y |
| View / Menu | Create / Options | View / Menu |
| Steam | PS | Xbox |
| Trackpad click | Touchpad click | – |
| L4 / R4 | Edge left / right Fn | Paddles P3 / P1 |
| L5 / R5 | Edge left / right paddle | Paddles P4 / P2 |
| ⋯ | reserved for the plugin | reserved for the plugin |

### Options

- **Turn off screen while connected**
- **Let your device play sound on the Deck:** off by default, so the Deck never shows up as a
  speaker and sound stays on your device. Your device reads this when pairing, so remove the
  Deck from its Bluetooth settings and pair again after changing it.
- **Trackpad click feedback:** a small haptic tick when you click a trackpad.
- **Stick deadzone**

## Limitations

- While the plugin is on, Bluetooth controllers and keyboards paired to the Deck can't be
  used (the Deck's Bluetooth is busy being a controller). Bluetooth audio keeps working.
- Tested with macOS and iPadOS. Windows and Linux should work (both support these
  controllers) but haven't been tested yet.

## How it works

The plugin's backend runs a small daemon with the Deck's system Python:

- **Bluetooth:** restarts BlueZ with a runtime-only config (under `/run`) that frees the HID
  channels and gives the Deck a gamepad identity, then serves the Bluetooth HID profile itself.
  Stopping the plugin, or rebooting, restores the stock setup.
- **Input:** takes exclusive access to the built-in controller over USB, so Steam on the Deck
  stops reacting to it, and translates its reports into the chosen controller's format.
- **Profiles:** `py_modules/sdcd/profiles/` defines each controller type: its Bluetooth
  identity, HID descriptor, input encoding and rumble parsing.

## Development

```sh
pnpm install
scripts/deploy.sh deck@<deck-ip>   # build and install over SSH (needs passwordless sudo on the Deck)
scripts/package.sh                 # build out/deck-as-controller.zip
```
