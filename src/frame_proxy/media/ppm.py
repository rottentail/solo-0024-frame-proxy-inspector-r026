from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PpmHeader:
    width: int
    height: int
    maximum: int
    pixel_offset: int

    @property
    def pixel_byte_size(self) -> int:
        return self.width * self.height * 3


def _skip_header_layout(data: bytes, cursor: int) -> int:
    while cursor < len(data):
        if data[cursor] in b" \t\r\n":
            cursor += 1
            continue
        if data[cursor : cursor + 1] == b"#":
            line_end = data.find(b"\n", cursor + 1)
            if line_end < 0:
                raise ValueError("PPM header comment is truncated")
            cursor = line_end + 1
            continue
        break
    return cursor


def _read_header_token(data: bytes, cursor: int) -> tuple[bytes, int]:
    cursor = _skip_header_layout(data, cursor)
    if cursor >= len(data):
        raise ValueError("PPM header is truncated")
    start = cursor
    while cursor < len(data) and data[cursor] not in b" \t\r\n":
        cursor += 1
    if cursor == start:
        raise ValueError("PPM header contains an empty token")
    return data[start:cursor], cursor


def _raster_offset(data: bytes, cursor: int) -> int:
    if cursor >= len(data) or data[cursor] not in b" \t\r\n":
        raise ValueError("PPM header is missing its final separator")
    if data[cursor : cursor + 2] == b"\r\n":
        cursor += 2
    else:
        cursor += 1

    # Newer encoders may emit a comment between the maximum and raster.
    # Reusing header layout parsing here also normalizes that representation.
    if data[cursor : cursor + 1] == b"#":
        cursor = _skip_header_layout(data, cursor)
    return cursor


def encode_ppm(width: int, height: int, pixels: bytes) -> bytes:
    if width <= 0 or height <= 0:
        raise ValueError("PPM dimensions must be positive")
    if len(pixels) != width * height * 3:
        raise ValueError("PPM pixel length does not match dimensions")
    return f"P6\n{width} {height}\n255\n".encode("ascii") + pixels


def parse_ppm_header(data: bytes) -> PpmHeader:
    if len(data) < 11:
        raise ValueError("payload is not a binary PPM")
    magic, cursor = _read_header_token(data, 0)
    if magic != b"P6":
        raise ValueError("payload is not a binary PPM")
    width_token, cursor = _read_header_token(data, cursor)
    height_token, cursor = _read_header_token(data, cursor)
    maximum_token, cursor = _read_header_token(data, cursor)
    try:
        width = int(width_token)
        height = int(height_token)
        maximum = int(maximum_token)
    except ValueError as exc:
        raise ValueError("PPM header dimensions must be integers") from exc
    cursor = _raster_offset(data, cursor)
    expected = width * height * 3
    if width <= 0 or height <= 0 or maximum != 255:
        raise ValueError("PPM header values are out of range")
    if len(data) - cursor != expected:
        raise ValueError("PPM payload length does not match its header")
    return PpmHeader(
        width=width,
        height=height,
        maximum=maximum,
        pixel_offset=cursor,
    )


def validate_ppm(
    data: bytes,
    *,
    expected_width: int | None = None,
    expected_height: int | None = None,
) -> PpmHeader:
    header = parse_ppm_header(data)
    if expected_width is not None and header.width != expected_width:
        raise ValueError("PPM width does not match the contact sheet record")
    if expected_height is not None and header.height != expected_height:
        raise ValueError("PPM height does not match the contact sheet record")
    if len(data) - header.pixel_offset != header.pixel_byte_size:
        raise ValueError("PPM raster length does not match its dimensions")
    return header
