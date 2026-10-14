# Omashare

Mirror your Omarchy screen to an Apple TV over AirPlay, from the bar.

> **Prototype.** Uses the AirPlay 2 mirroring sender from
> [lcd1232/pyatv#1](https://github.com/lcd1232/pyatv/pull/1)
> (`airplay-mirror-fairplay` branch). It isn't in upstream pyatv yet.

## How it works

```
Panel.qml ── bin/omashare scan ───────────► avahi-browse _airplay._tcp (pyatv.scan if no avahi)
          └─ bin/omashare mirror <host> ──► media pair-verify → FPLY handshake
                                            → MirrorSession (type-110 video over TCP)
                                                 ▲ video_command   ▲ audio_command
gpu-screen-recorder (VAAPI/NVENC H.264, picked source)       │
  └─ mpegts ─► ffmpeg -c copy -bsf h264_mp4toannexb ─► Annex-B H.264
parec <sink>.monitor (s16le 44.1 kHz stereo)                 │
  └─ libfdk-aac via ctypes (AAC-ELD, 480 samples/frame) ─► len-prefixed frames
```

- `bin/omashare` runs the Python helper with `uv`. On first use it installs
  pyatv from the fork into `~/.local/share/omashare/venv`. The venv lives
  outside the plugin folder because Omarchy refuses plugin folders that
  contain symlinks.
- `mirror` prints JSON lines (`{"event": "choosing" | "connecting" |
  "pairing" | "streaming" | "ended" | "error"}`) for the widget, and `pair`
  prints `"pin"` and `"paired"`. SIGTERM stops a mirror
  cleanly and sends RTSP `TEARDOWN`.
- **Choosing what to share:** by default the desktop's screen-share dialog
  opens, which on Omarchy is `hyprland-preview-share-picker` with Screens,
  Windows and Region tabs. You pick before anything connects to the TV.
  - A short probe capture reads the picked source's real size from its
    H.264 SPS.
  - The session's own capture then reopens the same source from the
    portal's restore token, so the dialog isn't shown twice.
  - Cancelling the dialog ends quietly.
  - To share the focused monitor without asking, set the widget's "What
    to share" setting to `monitor`, or pass `--source monitor`.
- The shared source is scaled to fit the configured maximum size, keeping its
  aspect ratio. For example, a 2880×1920 panel streams at 1620×1080.
- tvOS 27 plays mirrored audio only from a session set up in a particular
  way. The pyatv branch does this as of `d9be77bf`.
- Audio is encoded by calling `libfdk-aac` directly through `ctypes`. Arch's
  ffmpeg has no AAC-ELD encoder, but PipeWire already depends on the library.
- **Audio modes** (widget setting, or `--audio`):
  - `tv` (default): sound plays only on the receiver. While sharing, a null
    sink named `omashare` ("AirPlay") is the default output and its monitor
    is captured. The previous default is restored on stop. If the helper is
    killed, the next `scan`, `status` or `mirror` restores it.
  - `both`: the current output's monitor is captured, so sound also plays
    locally.
  - `off`: video only.
  - If EasyEffects is running, its input is captured: the apps' mix before
    any effects. A laptop-speaker preset otherwise reaches the TV too. With
    this machine's preset that measured +15 dB bass, mids 6–8 dB lower and
    +12 dB treble.
- The capture pipelines use `PR_SET_PDEATHSIG`. When the session kills it,
  `gpu-screen-recorder`, `ffmpeg` and `parec` exit too.

## Firewall

While sharing, the receiver sends clock-sync (timing) requests to UDP port
**47010** on this computer, and answers nothing else until they're answered.
Every other connection goes out from here. Omarchy turns ufw on with incoming
traffic denied, so that port needs one rule:

```sh
sudo ufw allow 47010/udp comment omashare
```

The panel checks for it without root, by reading `/etc/ufw/ufw.conf` and
`/etc/ufw/user.rules`, which anyone can read. When the rule is missing,
picking a receiver first shows **Allow in firewall**. It runs the command
above in a floating terminal, and the share starts once the rule is there.
A rule for that port from your local networks only counts too. The port is
a setting (`timingPort`, or `--timing-port`); the panel then offers the
rule for the new port. Other firewalls aren't checked. If a receiver
accepts the session and then stops answering, the panel says the timing
port may be blocked.

## Requirements

- Omarchy with the Quickshell-based shell; `gpu-screen-recorder`, `ffmpeg`,
  `uv`, `pactl`/`parec` and `libfdk-aac`, which Omarchy ships with
- An Apple TV on the same network. Set its AirPlay access to *Everyone on the
  same network* (Settings → AirPlay and HomeKit → Allow Access).

## Install (dev)

```sh
ln -s ~/Projects/lcd1232/omashare ~/.config/omarchy/plugins/lcd1232.omashare
omarchy-shell shell rescanPlugins
omarchy plugin enable lcd1232.omashare
```

After you edit `Panel.qml` or `manifest.json` through the symlink, run
`omarchy restart shell`. The shell's auto-reload uses `inotifywait -r`, which
doesn't follow a symlinked plugin folder. `rescanPlugins` and
disabling/enabling the plugin still load the stale compiled component.

Optional hotkey in `~/.config/hypr/bindings.lua`:

```lua
o.bind("SUPER + CTRL + A", "Omashare", "omarchy-shell shell toggle lcd1232.omashare")
```

## Usage

Open the panel, then click a receiver or select one with the arrow keys and
press Enter. Clicking it again or using **Stop sharing** ends the mirror.
Right-clicking the bar icon also stops it.

**Pairing:** the first time you pick a receiver, it shows a 4-digit PIN.
Type it into the panel and press **Pair**; the share then starts. omashare
registers a key of its own with that receiver and keeps it in
`~/.config/omashare/devices.json` (mode 600). Anyone with that file can
mirror to your receivers. Later shares don't ask again. If the receiver
forgets the pairing (a reset, for example), the panel marks it "not
paired" and asks for a PIN the next time.

From a terminal:

```sh
bin/omashare scan                               # "paired": true/false per receiver
bin/omashare pair 192.168.88.63                 # prints {"event": "pin"}, then type the PIN
bin/omashare unpair --id 9E:4D:0F:34:D4:7E      # forget a pairing
bin/omashare mirror 192.168.88.63 --fps 30 --max-size 1920x1080 --audio tv
bin/omashare mirror 192.168.88.63 --source monitor   # no dialog
bin/omashare firewall                           # is the timing port allowed
bin/omashare status
bin/omashare stop
```

## Known limitations

- Behaviour has only been checked on one Apple TV 4K (gen 2) on tvOS 27.
- A Mac receiver refuses pairing (HTTP 403, before any PIN) while its
  System Settings → General → AirDrop & Handoff → *Allow AirPlay for* is
  *Current User*, the macOS default. Set it to *Anyone on the Same
  Network*. Other senders are refused the same way.
- If EasyEffects is running but not processing every output (its "process
  all output streams" is off), the shared audio may be silent. In that case
  use the `both` audio mode or turn that EasyEffects option on.
- In `tv` mode, apps that pin themselves to a specific sink are moved to the
  null sink. When sharing stops they return to the restored default, not to
  the sink they had pinned.
- What is shared is fixed when sharing starts. A shared window that is
  resized keeps the size it had when it was picked.
- Picture delay is about 0.26 s, measured with a millisecond clock on an
  Apple TV 4K, and the sound keeps up with it. Every audio packet is sent
  twice, one frame apart, so a lost Wi-Fi packet doesn't leave a gap.

## Development

```sh
uv sync
uv run pytest
```
