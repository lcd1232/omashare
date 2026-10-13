import ctypes.util
import io
import math
import shlex
import struct
import sys

import pytest

from omashare import audio

needs_fdk = pytest.mark.skipif(
    ctypes.util.find_library("fdk-aac") is None, reason="libfdk-aac not installed"
)


def sine_frame(n: int) -> bytes:
    samples = []
    for i in range(audio.SAMPLES_PER_FRAME):
        v = int(8000 * math.sin(2 * math.pi * 440 * (n * audio.SAMPLES_PER_FRAME + i) / 44100))
        samples += [v, v]
    return struct.pack(f"<{len(samples)}h", *samples)


@needs_fdk
def test_encoder_produces_aac_eld_44100_stereo_480():
    enc = audio.EldEncoder()
    try:
        # AudioSpecificConfig: AOT escape (31) -> 39 (ER AAC ELD), 44100 Hz,
        # 2 channels, frameLengthFlag=1 (480 samples).
        assert enc.audio_specific_config.hex() == "f8e85000"
        frames = [enc.encode(sine_frame(n)) for n in range(20)]
    finally:
        enc.close()
    produced = [f for f in frames if f]
    assert len(produced) >= 15
    # 128 kbps at 91.875 frames/s is ~174 bytes a frame.
    assert 50 < sum(map(len, produced)) / len(produced) < 400


@needs_fdk
def test_encoder_rejects_partial_frame():
    enc = audio.EldEncoder()
    try:
        with pytest.raises(ValueError):
            enc.encode(b"\0" * 100)
    finally:
        enc.close()


def test_read_frames_rechunks_into_whole_frames():
    data = bytes(range(256)) * ((audio.BYTES_PER_FRAME * 3) // 256 + 1)
    frames = list(audio.read_frames(io.BytesIO(data)))
    assert len(frames) == len(data) // audio.BYTES_PER_FRAME
    assert all(len(f) == audio.BYTES_PER_FRAME for f in frames)
    assert b"".join(frames) == data[: len(frames) * audio.BYTES_PER_FRAME]


def test_frame_record_is_big_endian_length_prefixed():
    assert audio.frame_record(b"abc") == b"\0\0\0\x03abc"


def test_audio_command_execs_this_interpreter():
    cmd = audio.audio_command("omashare.monitor", 96000)
    assert cmd.startswith("exec ")
    argv = shlex.split(cmd[len("exec ") :])
    assert argv[:3] == [sys.executable, "-m", "omashare.audio"]
    assert argv[argv.index("--source") + 1] == "omashare.monitor"
    assert argv[argv.index("--bitrate") + 1] == "96000"


def test_parec_captures_44100_stereo_s16():
    argv = audio.parec_argv("foo.monitor")
    assert argv[0] == "parec"
    for flag in ("--device=foo.monitor", "--format=s16le", "--rate=44100", "--channels=2", "--raw"):
        assert flag in argv
