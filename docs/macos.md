# Arctis Sound Manager on macOS

ASM runs on macOS for **headset control** and the **equaliser**. The other audio features are Linux only for now.

## What works on macOS

- **Headset settings**: sidetone, mic volume, auto shutdown, and the other settings on the Headset page.
- **Headset status**: battery, power state, the ChatMix dial position.
- **DAC page and OLED**: on base stations that have them. With **Turn screen off while the session is locked or idle**, the base's screen goes dark while the Mac is locked, its display asleep, or nobody has used the keyboard or mouse for 5 minutes.
- **Sleep**: after the Mac wakes, ASM takes the headset back and sends its settings again, as after a replug.
- **Equaliser** (Equalizer page), see [Equaliser](#equaliser):
  - the headset's own 10-band EQ (Custom EQ), on headsets that have one (Arctis Nova family, GameBuds);
  - the Sonar EQ on the Mac's sound, on every headset: parametric curve, Sonar presets, Bass / Voice / Treble, Boost, Smart Volume and Spatial Audio, for stereo and 5.1 / 7.1 sources;
  - the Micro channel on the headset's microphone: curve, presets, noise reduction, noise gate and compressor.
- **Tray**: battery and status in the menu bar, and the Sonar presets.
- **CLI**: `asm-cli status` and the `asm-cli tools` probes.

The ChatMix dial still mixes game and chat as usual. On headsets that have one, such as the Arctis 9, the mix is done in the dongle itself. ASM only shows the dial position.

## What does not work on macOS

Everything built on PipeWire is hidden on macOS:

- the Game / Chat / Media / Aux channels and their mixer (Home page): the Sonar EQ runs on the Mac's whole sound, as one channel;
- the microphone's noise cancelling (RNNoise, DeepFilterNet): they are Linux plugins;
- Clips, the media router and Stream Guard.

macOS has no equivalent of PipeWire's virtual sinks and filter-chain. Splitting the sound per app would mean writing a CoreAudio driver.

Also not supported yet:

- the GameBuds charging case;
- "Now playing" on the base's screen: it reads MPRIS, which macOS does not have.

## How it works

macOS keeps every HID interface for itself, so ASM cannot claim the headset through libusb the way it does on Linux. On macOS it goes through **hidapi** (IOHIDManager) instead, and shares the interfaces with the system.

The daemon and the GUI talk over D-Bus. On macOS that is Homebrew's session bus.

## Install

1. Install the requirements:

   ```bash
   brew install libusb dbus
   ```

   You also need [uv](https://docs.astral.sh/uv/).

2. Start the D-Bus session bus. Homebrew ships its LaunchAgent, but `brew services` does not load it, so load it by hand:

   ```bash
   cp "$(brew --prefix dbus)/org.freedesktop.dbus-session.plist" ~/Library/LaunchAgents/
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/org.freedesktop.dbus-session.plist
   ```

   `Bootstrap failed: 5: Input/output error` is launchctl's catch-all.
   Most often the agent is already loaded
   (`launchctl print gui/$(id -u)/org.freedesktop.dbus-session` shows it,
   and `launchctl getenv DBUS_LAUNCHD_SESSION_BUS_SOCKET` prints its
   socket). Otherwise check that the plist is valid
   (`plutil -lint ~/Library/LaunchAgents/org.freedesktop.dbus-session.plist`)
   and that the agent is not disabled
   (`launchctl enable gui/$(id -u)/org.freedesktop.dbus-session`).

3. Get ASM and install its dependencies (this pulls `hidapi` on macOS):

   ```bash
   git clone https://github.com/loteran/Arctis-Sound-Manager.git
   cd Arctis-Sound-Manager
   uv sync
   ```

## Run

Start the daemon in one terminal:

```bash
uv run asm-daemon
```

Then, in another terminal, start the GUI:

```bash
uv run asm-gui
```

To keep only the menu bar icon, start the GUI with `--systray`.

To check the headset from the command line:

```bash
uv run asm-cli status
```

When the headset is found, the daemon log shows `Using the hidapi transport for <headset>: interface …`.

## Equaliser

The Equalizer page works as on Linux, with what macOS allows:

- **Custom EQ** writes the 10 bands into the headset itself. Only the
  headsets that have an on-device EQ offer it (the Arctis Nova family, the
  GameBuds). Nothing has to run on the Mac.
- **Sonar** runs the Sonar EQ on the Mac's sound. An app cannot filter what
  the Mac plays without a driver, so this goes through
  [BlackHole](https://github.com/ExistentialAudio/BlackHole), a free loopback
  driver:

  ```bash
  brew install blackhole-2ch
  ```

  Then, at the top of the Sonar EQ, turn on **Apply the EQ to the Mac's
  sound**. The daemon moves the system output to BlackHole 2ch, reads it,
  runs the Game tab's curve (presets, bands, Bass / Voice / Treble, Boost,
  Smart Volume, Spatial Audio)
  and plays the result on the headset. **Play on** picks another output,
  for instance a headset whose name does not contain Arctis or SteelSeries.

  Turning it off, switching to Custom EQ, unplugging the headset or
  stopping the daemon puts the system output back where it was. If you
  pick another output in the menu bar while it runs, ASM leaves it there.

  Reading BlackHole counts as using the microphone for macOS. From a
  terminal, the terminal's own permission covers it. The app asks for it
  the first time the EQ starts: allow it, or the EQ plays silence. A denied
  or stale permission is under **System Settings › Privacy & Security ›
  Microphone**, or reset with
  `tccutil reset Microphone com.github.arctis-sound-manager`. A rebuilt
  app is a new app to macOS and may ask again.

  **Spatial Audio** places the Mac's sound on virtual speakers around you,
  through the HRIR chosen under **Settings › Audio › HRIR** (the same
  HeSuVi set as on Linux). Immersion, Distance (a plate reverb, as on
  Linux) and the speakers' levels apply. A stereo source has only the two
  front speakers; for the others, see 7.1 sources below. Speakers mode
  folds the speakers to stereo, with their levels, as on Linux.

  The EQ adds about 10 ms of latency, 15 ms with Spatial Audio.

- **7.1 sources.** BlackHole 2ch only carries stereo, so for films and
  games in 5.1 or 7.1 the Mac's sound goes through BlackHole 16ch instead
  (`brew install blackhole-16ch`):

  1. Open **Audio MIDI Setup**, select **BlackHole 16ch**, click
     **Configure Speakers**, and pick **7.1 Surround** (or 5.1) under
     **Multichannel**. Apps only play surround to a device set up for it.
  2. Turn on **7.1 sources** at the top of the Sonar EQ.

  The daemon reads the speakers set for BlackHole 16ch and runs the EQ on
  each of them. With Spatial Audio, each speaker gets its own place, as
  with HeSuVi on Linux; without it, the speakers are folded to stereo (the
  centre and surrounds at -3 dB, the LFE left out, as on Linux). "Only for
  surround sources" works too: macOS does not tell what an app plays, so
  ASM treats the sound as surround while anything comes out of the
  speakers beyond the front pair, and for 10 seconds after.

  The microphone then goes through BlackHole 2ch instead of 16ch (the next
  item): both BlackHoles are needed for both.

- **The Micro tab** runs on the headset's microphone, through a second
  BlackHole: whatever is written to a BlackHole comes out of it, and the
  2ch one already carries the Mac's sound (with 7.1 sources on, the two
  swap: the microphone goes through BlackHole 2ch).

  ```bash
  brew install blackhole-16ch
  ```

  Turn on **Process the microphone** at the top of the Sonar EQ. The daemon
  reads the headset's microphone (or the one picked next to the switch),
  runs the Micro tab's curve, Background and Impact Noise Reduction, the
  noise gate and the compressor, and writes the result to BlackHole 16ch,
  which becomes the Mac's input. Apps that use the system microphone hear
  the processed voice; in an app with its own microphone setting, pick
  BlackHole 16ch. Turning it off puts the input back, as for the output. The headset's ChatMix dial and its
  chat output are not affected: only the system output goes through the
  EQ.

## Build the app

`macos/build-app.sh` builds **Arctis Sound Manager.app**, a self-contained
app with its own Python, Qt, hidapi and libusb: copy it to `/Applications`
and start it like any other app. It still needs Homebrew's D-Bus session bus
(step 2 of the install). Building it needs the checkout, `uv` and
`brew install libusb`. The script builds in its own environment
(`build/app-venv`) on a Python 3.13 that uv downloads if needed, because
PyInstaller needs a Python with a shared `libpython`, which the Python
behind your `.venv` may not have:

```bash
./macos/build-app.sh
cp -R "dist/Arctis Sound Manager.app" /Applications/
```

The app lives in the menu bar, without a Dock icon. Opening it also starts
the daemon in the background, when none is running; quitting the app leaves
the daemon running. The daemon logs to `~/Library/Logs/asm-daemon.log`. The
CLI is inside the app:

```bash
"/Applications/Arctis Sound Manager.app/Contents/MacOS/asm-cli" status
```

The app is built for the Mac that builds it (Apple silicon or Intel) and is
not signed with a developer certificate. Built on this Mac, it opens
directly. Copied to another Mac through a download or AirDrop, Gatekeeper
blocks it: right-click it and choose **Open**, or run
`xattr -dr com.apple.quarantine "/Applications/Arctis Sound Manager.app"`.

If you move the app after turning on **Launch at startup**, turn it off and
on again so the LaunchAgents point at the new place.

## Start at login

In the GUI, open the **Settings** page and turn on **Launch at startup**.
It writes two LaunchAgents in `~/Library/LaunchAgents`, one for the daemon
(`com.github.arctis-sound-manager.daemon`) and one for the menu bar icon
(`com.github.arctis-sound-manager.gui`). They point at the scripts of the
environment the GUI runs from: the app, or your checkout when you run
`uv run asm-gui`.

The agents start at the next login: turning the switch on does not start a
second daemon or icon next to the ones already running. Turning it off
deletes the agents and leaves the running processes alone.

The daemon logs to `~/Library/Logs/asm-daemon.log`.

### By hand

To install only the daemon's agent, and start it right away, fill in the
template and load it. Set `REPO` to your checkout (the one where you ran
`uv sync`, on a branch that has the `macos/` folder):

```bash
REPO=~/path/to/Arctis-Sound-Manager
sed -e "s|__REPO__|$REPO|g" -e "s|__HOME__|$HOME|g" \
    "$REPO/macos/com.github.arctis-sound-manager.daemon.plist" \
    > ~/Library/LaunchAgents/com.github.arctis-sound-manager.daemon.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.github.arctis-sound-manager.daemon.plist
```

To stop it and remove it from login:

```bash
launchctl bootout gui/$(id -u)/com.github.arctis-sound-manager.daemon
rm ~/Library/LaunchAgents/com.github.arctis-sound-manager.daemon.plist
```

## Restart the daemon

**Maintenance › Restart the daemon** in the menu bar, or **Restart** next to
**Daemon** on the Settings page, stops asm-daemon and starts it again: after
replugging a dongle, or when the equaliser or the headset settings stop
answering. It stops the daemon cleanly (the equaliser gives the system
output and input back), kills it if it does not stop within 10 seconds, then
starts it through its LaunchAgent when **Launch at startup** is on, or from
the app otherwise. From a terminal, with the LaunchAgent loaded:

```bash
launchctl kickstart -k gui/$(id -u)/com.github.arctis-sound-manager.daemon
```

## Troubleshooting

- **`No D-Bus session bus` in the log.** The session bus LaunchAgent is not loaded: redo step 2 of the install. To check that it is loaded, run `launchctl getenv DBUS_LAUNCHD_SESSION_BUS_SOCKET`. It should print a path.
- **`hidapi: no HID device for command interface N`.** macOS does not list the interface ASM expects. To see what it does list, run:

  ```bash
  uv run python -c "import hid; print(*hid.enumerate(0x1038), sep='\n')"
  ```

  Attach that output to a bug report.
- **macOS asks for Input Monitoring.** Grant it to your terminal (or to Python). Reading some HID interfaces needs it, depending on the headset.
