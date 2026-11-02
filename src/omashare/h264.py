"""Just enough H.264 parsing to read the picture size from an SPS."""

from __future__ import annotations

from typing import Iterable, Optional

# Profiles whose SPS carries chroma format, bit depth and scaling lists.
_HIGH_PROFILES = {100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135}


class _Bits:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def bit(self) -> int:
        byte = self._data[self._pos >> 3]
        value = (byte >> (7 - (self._pos & 7))) & 1
        self._pos += 1
        return value

    def bits(self, n: int) -> int:
        value = 0
        for _ in range(n):
            value = (value << 1) | self.bit()
        return value

    def ue(self) -> int:
        zeros = 0
        while self.bit() == 0:
            zeros += 1
        return (1 << zeros) - 1 + self.bits(zeros)

    def se(self) -> int:
        value = self.ue()
        return (value + 1) // 2 if value & 1 else -(value // 2)


def _unescape(nal: bytes) -> bytes:
    """Drop emulation-prevention bytes (00 00 03 -> 00 00)."""
    out = bytearray()
    zeros = 0
    for byte in nal:
        if zeros >= 2 and byte == 3:
            zeros = 0
            continue
        out.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return bytes(out)


def _skip_scaling_list(bits: _Bits, size: int) -> None:
    last = next_scale = 8
    for _ in range(size):
        if next_scale:
            next_scale = (last + bits.se() + 256) % 256
        last = next_scale or last


def sps_dimensions(nal: bytes) -> tuple[int, int]:
    """Displayed (width, height) of an SPS NAL unit (header byte included)."""
    if not nal or nal[0] & 0x1F != 7:
        raise ValueError("not an SPS NAL unit")
    bits = _Bits(_unescape(nal[1:]))
    profile = bits.bits(8)
    bits.bits(16)  # constraint flags, level
    bits.ue()  # seq_parameter_set_id
    chroma_format = 1
    if profile in _HIGH_PROFILES:
        chroma_format = bits.ue()
        if chroma_format == 3:
            bits.bit()  # separate_colour_plane_flag
        bits.ue()  # bit_depth_luma_minus8
        bits.ue()  # bit_depth_chroma_minus8
        bits.bit()  # qpprime_y_zero_transform_bypass_flag
        if bits.bit():  # seq_scaling_matrix_present_flag
            for i in range(8 if chroma_format != 3 else 12):
                if bits.bit():
                    _skip_scaling_list(bits, 16 if i < 6 else 64)
    bits.ue()  # log2_max_frame_num_minus4
    poc_type = bits.ue()
    if poc_type == 0:
        bits.ue()
    elif poc_type == 1:
        bits.bit()
        bits.se()
        bits.se()
        for _ in range(bits.ue()):
            bits.se()
    bits.ue()  # max_num_ref_frames
    bits.bit()  # gaps_in_frame_num_value_allowed_flag
    width_mbs = bits.ue() + 1
    height_map_units = bits.ue() + 1
    frame_mbs_only = bits.bit()
    if not frame_mbs_only:
        bits.bit()  # mb_adaptive_frame_field_flag
    bits.bit()  # direct_8x8_inference_flag
    width = width_mbs * 16
    height = height_map_units * 16 * (2 - frame_mbs_only)
    if bits.bit():  # frame_cropping_flag
        left, right, top, bottom = bits.ue(), bits.ue(), bits.ue(), bits.ue()
        crop_x = 2 if chroma_format in (1, 2) else 1
        crop_y = (2 if chroma_format == 1 else 1) * (2 - frame_mbs_only)
        width -= (left + right) * crop_x
        height -= (top + bottom) * crop_y
    return width, height


def find_sps(nalus: Iterable[bytes]) -> Optional[bytes]:
    """The first SPS among *nalus*, or None."""
    return next((n for n in nalus if n and n[0] & 0x1F == 7), None)
