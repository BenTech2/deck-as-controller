# Deck as Controller

A [Decky Loader](https://decky.xyz) plugin that turns your Steam Deck into a wireless Bluetooth
controller for your Mac or iPad. Nothing needs to be installed on the other device: the Deck
pairs as a PS5 or Xbox controller.

Tested on a Steam Deck OLED (SteamOS, Decky Loader 3.2) with a Mac running macOS 27 and an
iPad Mini 7 running ios 27.

## Features

- **Three controller types:** PS5 (DualSense), PS5 Edge, and Xbox Elite Series 2. PS5 Edge
  carries all four Deck back buttons.
- **Deck screen off while connected.** Tap **⋯** to toggle it; hold **⋯** for 2 seconds to stop
  and get the Deck's screen and controls back.
- **Reconnects automatically** to the last device when you turn the plugin on.
- **Remembers your devices:** pair more than one (e.g. a Mac and an iPad).
- **Rumble** (see the table below for where it works).
- **Trackpads:** the Deck trackpads act as the PS5 touchpad, with a small haptic tick when you
  click them.
- **Gyro** on the PS5 types.
- **Battery level** of the Deck shown on the Mac (PS5 types).

## Install

1. Install [Decky Loader](https://decky.xyz) on your Deck.
2. Download `deck-as-controller.zip` from the releases page.
3. In Game Mode, open **⋯ → Decky → ⚙ Settings → General**, turn on **Developer mode**, then go
   to **Developer → Install Plugin from ZIP File** and pick the zip.

## Use

1. Open **⋯ → Decky → Deck as Controller**, choose a **controller type**, and turn on
   **Use as controller**.
2. The first time, the Deck is ready to pair. On your Mac or iPad, open Bluetooth settings and
   connect to the controller that appears (e.g. "DualSense Edge Wireless Controller").
3. After that, turning the plugin on reconnects to the device automatically.

While connected, the Deck's controls go to your device:

| On the Deck                         | Does                                     |
| ----------------------------------- | ---------------------------------------- |
| Tap **⋯** (or tap the black screen) | Turn the Deck screen on or off           |
| Hold **⋯** for 2 seconds            | Stop and give the Deck its controls back |

**Switching controller types:** your device remembers the Deck as the type it was paired as.
To switch, remove the Deck from your device's Bluetooth settings, choose the new type, tap
**Pair a new device**, and pair again.

### Which type to pick

On a Mac, **PS5 Edge** is the best all-round choice. Tested results:

|                                                                         | PS5 / PS5 Edge | Xbox Elite      |
| ----------------------------------------------------------------------- | -------------- | --------------- |
| Steam and games played through it (e.g. Baldur's Gate 3)                | ✅             | ✅              |
| Mac games that read controllers directly (e.g. Hollow Knight: Silksong) | ✅             | ❌ not detected |
| Rumble from Steam (Steam's test page, Steam Input games)                | ❌             | ✅              |
| Rumble from Mac games using Apple's controller support                  | ✅             | ✅              |
| Back buttons                                                            | PS5 Edge       | not tested      |
| iPad                                                                    | not tested     | ✅              |

### Button mapping

| Deck           | PS5 / PS5 Edge           | Xbox Elite              |
| -------------- | ------------------------ | ----------------------- |
| A B X Y        | ✕ ○ □ △                  | A B X Y                 |
| View / Menu    | Create / Options         | View / Menu             |
| Steam          | PS                       | Xbox                    |
| Trackpad click | Touchpad click           | –                       |
| L4 / R4        | Edge left / right Fn     | Paddles P3 / P1         |
| L5 / R5        | Edge left / right paddle | Paddles P4 / P2         |
| ⋯              | reserved for the plugin  | reserved for the plugin |

### Options

- **Turn off screen while connected**
- **Trackpad click feedback:** the haptic tick when you click a trackpad.
- **Stick deadzone**

## Known limitations

- **Steam doesn't rumble the PS5 types.** Steam never sends rumble to them over Bluetooth, so
  Steam's rumble test and games that rumble through Steam Input stay silent. Use Xbox Elite if
  you need rumble in those.
- **Some Mac games don't detect Xbox Elite.** macOS keeps the name the Deck first paired with
  (e.g. "DualSense Wireless Controller") for its Bluetooth address, even after you forget it,
  and passes that name to games. Games that recognize controllers by name, like Silksong, then
  ignore the Deck. Renaming it in Bluetooth settings doesn't help.
- **The Deck's own Bluetooth devices are unavailable while the plugin is on.** Headphones,
  controllers and keyboards paired to the Deck can't be used, and your device never uses the
  Deck as a speaker.
- **Turning the plugin on or off restarts the Deck's Bluetooth**, which drops any Bluetooth
  devices connected to the Deck for a moment.

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

To log what the host sends (rumble, setup requests), run `sudo touch /run/sdcd-debug` on the
Deck before turning the plugin on; the output appears in Decky's plugin log.
