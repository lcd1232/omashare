"""Screen audio: PipeWire sink monitor → AAC-ELD frames on stdout.

The mirror session's ``audio_command`` must write AAC-ELD (44100 Hz stereo,
480 samples/frame) as 4-byte big-endian length + raw frame. Arch's ffmpeg
has no AAC-ELD encoder, but libfdk-aac itself ships with PipeWire, so this
module drives it through ctypes. Like :mod:`omashare.capture` it is started
as ``exec python -m omashare.audio`` and takes its ``parec`` down with it.
"""

from __future__ import annotations

import argparse
import ctypes as C
import shlex
import signal
import subprocess
import sys
from typing import BinaryIO, Iterator

from omashare.capture import _die_with_parent

SAMPLE_RATE = 44100
CHANNELS = 2
SAMPLES_PER_FRAME = 480  # screen_audio.AUDIO_SAMPLES_PER_FRAME
BYTES_PER_FRAME = SAMPLES_PER_FRAME * CHANNELS * 2  # s16le

# aacenc_lib.h
_AACENC_AOT = 0x0100
_AACENC_BITRATE = 0x0101
_AACENC_SAMPLERATE = 0x0103
_AACENC_GRANULE_LENGTH = 0x0105
_AACENC_CHANNELMODE = 0x0106
_AACENC_TRANSMUX = 0x0300
_AOT_ER_AAC_ELD = 39
_MODE_2 = 2  # stereo
_TT_MP4_RAW = 0
_IN_AUDIO_DATA = 0
_OUT_BITSTREAM_DATA = 3


class _Info(C.Structure):
    _fields_ = [
        (name, C.c_uint)
        for name in (
            "maxOutBufBytes", "maxAncBytes", "inBufFillLevel", "inputChannels",
            "frameLength", "nDelay", "nDelayCore",
        )
    ] + [("confBuf", C.c_ubyte * 64), ("confSize", C.c_uint)]  # fmt: skip


class _BufDesc(C.Structure):
    _fields_ = [
        ("numBufs", C.c_int),
        ("bufs", C.POINTER(C.c_void_p)),
        ("bufferIdentifiers", C.POINTER(C.c_int)),
        ("bufSizes", C.POINTER(C.c_int)),
        ("bufElSizes", C.POINTER(C.c_int)),
    ]


class _InArgs(C.Structure):
    _fields_ = [("numInSamples", C.c_int), ("numAncBytes", C.c_int)]


class _OutArgs(C.Structure):
    _fields_ = [
        ("numOutBytes", C.c_int),
        ("numInSamples", C.c_int),
        ("numAncBytes", C.c_int),
        ("bitResState", C.c_int),
    ]


class EldEncoder:
    """libfdk-aac AAC-ELD encoder: 480-sample s16le stereo in, raw frame out."""

    def __init__(self, bitrate: int = 128_000) -> None:
        self._lib = C.CDLL("libfdk-aac.so.2")
        self._handle = C.c_void_p()
        self._check(self._lib.aacEncOpen(C.byref(self._handle), 0, CHANNELS), "open")
        for param, value in (
            (_AACENC_AOT, _AOT_ER_AAC_ELD),
            (_AACENC_SAMPLERATE, SAMPLE_RATE),
            (_AACENC_CHANNELMODE, _MODE_2),
            (_AACENC_GRANULE_LENGTH, SAMPLES_PER_FRAME),
            (_AACENC_BITRATE, bitrate),
            (_AACENC_TRANSMUX, _TT_MP4_RAW),
        ):
            self._check(
                self._lib.aacEncoder_SetParam(self._handle, param, value),
                f"set param {param:#06x}",
            )
        # A call with no buffers applies the parameters.
        self._check(self._lib.aacEncEncode(self._handle, None, None, None, None), "init")
        info = _Info()
        self._check(self._lib.aacEncInfo(self._handle, C.byref(info)), "info")
        if info.frameLength != SAMPLES_PER_FRAME:
            raise RuntimeError(f"encoder frame length {info.frameLength}")
        self.audio_specific_config = bytes(info.confBuf[: info.confSize])
        self._out = (C.c_ubyte * max(info.maxOutBufBytes, 8192))()

    @staticmethod
    def _check(err: int, what: str) -> None:
        if err != 0:
            raise RuntimeError(f"libfdk-aac {what} failed: error {err:#x}")

    @staticmethod
    def _desc(buf, size: int, ident: int, el_size: int) -> _BufDesc:
        return _BufDesc(
            1,
            C.pointer(C.c_void_p(C.addressof(buf))),
            C.pointer(C.c_int(ident)),
            C.pointer(C.c_int(size)),
            C.pointer(C.c_int(el_size)),
        )

    def encode(self, pcm: bytes) -> bytes:
        """Encode one frame of PCM; may return b"" while the encoder fills."""
        if len(pcm) != BYTES_PER_FRAME:
            raise ValueError(f"need {BYTES_PER_FRAME} bytes of PCM, got {len(pcm)}")
        inbuf = (C.c_ubyte * len(pcm)).from_buffer_copy(pcm)
        in_desc = self._desc(inbuf, len(pcm), _IN_AUDIO_DATA, 2)
        out_desc = self._desc(self._out, len(self._out), _OUT_BITSTREAM_DATA, 1)
        in_args = _InArgs(SAMPLES_PER_FRAME * CHANNELS, 0)
        out_args = _OutArgs()
        self._check(
            self._lib.aacEncEncode(
                self._handle,
                C.byref(in_desc),
                C.byref(out_desc),
                C.byref(in_args),
                C.byref(out_args),
            ),
            "encode",
        )
        return bytes(self._out[: out_args.numOutBytes])

    def close(self) -> None:
        if self._handle:
            self._lib.aacEncClose(C.byref(self._handle))
            self._handle = C.c_void_p()


def read_frames(stream: BinaryIO) -> Iterator[bytes]:
    """Yield exact one-frame chunks of PCM from *stream* until EOF."""
    read = getattr(stream, "read1", stream.read)
    buf = bytearray()
    while chunk := read(65536):
        buf += chunk
        while len(buf) >= BYTES_PER_FRAME:
            yield bytes(buf[:BYTES_PER_FRAME])
            del buf[:BYTES_PER_FRAME]


def frame_record(frame: bytes) -> bytes:
    """The length-prefixed record MirrorSession reads."""
    return len(frame).to_bytes(4, "big") + frame


def parec_argv(source: str) -> list[str]:
    return [
        "parec",
        f"--device={source}",
        "--format=s16le",
        f"--rate={SAMPLE_RATE}",
        f"--channels={CHANNELS}",
        "--raw",
        # Small capture fragments keep the added latency low.
        "--latency-msec=20",
    ]


def audio_command(source: str, bitrate: int = 128_000) -> str:
    """Shell command for MirrorSession's ``audio_command``."""
    args = [
        sys.executable, "-m", "omashare.audio",
        "--source", source, "--bitrate", str(bitrate),
    ]  # fmt: skip
    return "exec " + shlex.join(args)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omashare.audio")
    parser.add_argument("--source", required=True, help="PulseAudio source name")
    parser.add_argument("--bitrate", type=int, default=128_000)
    args = parser.parse_args(argv)

    _die_with_parent()
    encoder = EldEncoder(args.bitrate)
    parec = subprocess.Popen(
        parec_argv(args.source),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        preexec_fn=_die_with_parent,
    )
    signal.signal(signal.SIGTERM, lambda *_: parec.kill())
    assert parec.stdout is not None
    out = sys.stdout.buffer
    try:
        for pcm in read_frames(parec.stdout):
            frame = encoder.encode(pcm)
            if frame:
                out.write(frame_record(frame))
                out.flush()
    except BrokenPipeError:
        pass
    finally:
        parec.kill()
        encoder.close()
    return parec.wait()


if __name__ == "__main__":
    sys.exit(main())
