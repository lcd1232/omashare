# Omashare

Mirror your Omarchy screen to an Apple TV over AirPlay, from the bar.

> **Prototype.** Uses the AirPlay 2 mirroring sender from
> [lcd1232/pyatv#1](https://github.com/lcd1232/pyatv/pull/1)
> (`airplay-mirror-fairplay` branch). It isn't in upstream pyatv yet.

## How it works

```
Panel.qml ── bin/omashare scan ───────────► pyatv.scan (mDNS, AirPlay)
          └─ bin/omashare mirror <host> ──► media pair-verify → FPLY handshake
                                            → MirrorSession (type-110 video over TCP)
                                                 ▲ video_command
                                                 ▲ video_command   ▲ audio_command
gpu-screen-recorder (VAAPI/NVENC H.264, focused monitor)     │
  └─ mpegts ─► ffmpeg -c copy -bsf h264_mp4toannexb ─► Annex-B H.264
parec <sink>.monitor (s16le 44.1 kHz stereo)                 │
  └─ libfdk-aac via ctypes (AAC-ELD, 480 samples/frame) ─► len-prefixed frames
```

- `bin/omashare` runs the Python helper with `uv`. On first use it installs
  pyatv from the fork into `~/.local/share/omashare/venv`. The venv lives
  outside the plugin folder because Omarchy refuses plugin folders that
  contain symlinks.
- `mirror` prints JSON lines (`{"event": "connecting" | "pairing" |
  "streaming" | "ended" | "error"}`) for the widget. SIGTERM stops it
  cleanly and sends RTSP `TEARDOWN`.
- The screen is scaled to fit the configured maximum size, keeping its aspect
  ratio. For example, a 2880×1920 panel streams at 1620×1080.
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
- The capture pipelines use `PR_SET_PDEATHSIG`. When the session kills it,
  `gpu-screen-recorder`, `ffmpeg` and `parec` exit too.

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

Optional hotkey in `~/.config/hypr/bindings.lua`:

```lua
o.bind("SUPER + CTRL + A", "Omashare", "omarchy-shell shell toggle lcd1232.omashare")
```

## Usage

Open the panel, then click a receiver or select one with the arrow keys and
press Enter. Clicking it again or using **Stop sharing** ends the mirror.
Right-clicking the bar icon also stops it.

From a terminal:

```sh
bin/omashare scan
bin/omashare mirror 192.168.88.63 --fps 30 --max-size 1920x1080 --audio tv
bin/omashare status
bin/omashare stop
```

## Known limitations

- A/V sync hasn't been checked on a real Apple TV. pyatv buffers about 2 s
  of audio before sending (`AUDIO_PREBUFFER`), and video isn't delayed to
  match.
- In `tv` mode, apps that pin themselves to a specific sink are moved to the
  null sink. When sharing stops they return to the restored default, not to
  the sink they had pinned.
- Only the focused monitor is mirrored, and the choice is made when sharing
  starts.
- The media pair-verify signs with the identity hard-coded in the pyatv
  branch. See `media_pair_verify.py` there.

## Development

```sh
uv sync
uv run pytest
```
