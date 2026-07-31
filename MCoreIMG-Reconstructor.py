#!/usr/bin/env python3
from __future__ import annotations
"""
MCoreIMG Reconstructor — compressed MeshCore image decoder
==========================================================

Decodes transport files exported by MCoreIMG-Constructor.py and renders the
reconstructed 720 x 480 image as PNG.

Compatible transport profile
----------------------------
* One to five MeshCore text frames.
* Maximum 150 ASCII characters per frame.
* 15-character MCI control header plus up to 135 Base91 payload characters.
* Per-frame CRC-16 and assembled-stream CRC-32 validation.
* Stateful opcode/color/parameter decoding.
* Absolute or predictive coordinate decoding.
* ZigZag + Golomb-Rice signed deltas.
* Unsigned Exp-Golomb variable integers.
* Translated-repeat macro expansion.

Usage
-----
    python MCoreIMG-Reconstructor.py image.mci
    python MCoreIMG-Reconstructor.py image.mci --output reconstructed.png
    python MCoreIMG-Reconstructor.py image.mci --dump-json

With no input path, a graphical file chooser is opened.

Arch Linux dependencies
-----------------------
    sudo pacman -Syu tk python-pillow
"""

import argparse
import binascii
import copy
import json
import math
import os
import sys
import subprocess
import tkinter as tk
import zlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tkinter import filedialog
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError as exc:  # pragma: no cover - environment dependent
    raise SystemExit(
        "Pillow is required. Install it with: sudo pacman -S python-pillow"
    ) from exc


# ---------------------------------------------------------------------------
# Protocol constants — must match MCoreIMG-Constructor.py
# ---------------------------------------------------------------------------

CANVAS_W = 720
CANVAS_H = 480
BACKGROUND = "#FFFFFF"

PROTOCOL_NAME = "MCoreIMG"
PROTOCOL_VERSION = 1
SOURCE_FORMAT = "MCoreIMG-source"
SOURCE_VERSION = 1
DEFAULT_GRID = "EN60"  # source metadata only; not transmitted

MAX_MESSAGES = 5
MESSAGE_LEN = 150
FRAME_HEADER_LEN = 15
FRAME_PAYLOAD_LEN = MESSAGE_LEN - FRAME_HEADER_LEN
FRAME_MAGIC = "MCI"

BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

# Printable ASCII ! through ~, excluding quote, apostrophe, and backslash.
BASE91 = "".join(
    chr(code)
    for code in range(33, 127)
    if chr(code) not in {'"', "'", "\\"}
)
assert len(BASE91) == 91
BASE91_INDEX = {ch: i for i, ch in enumerate(BASE91)}

TEXT_ALPHABET = " 0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ-+./?:,()[]#_=@"
assert len(TEXT_ALPHABET) <= 64
MAX_TEXT_LEN = 31
MAX_EDITOR_COMMANDS = 512
DEFAULT_TEXT_FONT_SIZE = 20
RECONSTRUCTOR_BUILD = "2026.07.31-fontfix3-hardcoded"

COLOR_TABLE: List[Tuple[str, str, str]] = [
    ("0", "Black", "#000000"),
    ("1", "White", "#FFFFFF"),
    ("2", "Gray", "#808080"),
    ("3", "Red", "#FF0000"),
    ("4", "Green", "#00A000"),
    ("5", "Blue", "#0000FF"),
    ("6", "Yellow", "#FFFF00"),
    ("7", "Cyan", "#00FFFF"),
    ("8", "Magenta", "#FF00FF"),
    ("9", "Brown", "#8B4513"),
    ("A", "Tan", "#D2B48C"),
    ("B", "Beige", "#F5F5DC"),
    ("C", "Wheat", "#F5DEB3"),
    ("D", "Sandybrown", "#F4A460"),
    ("E", "Sienna", "#A0522D"),
    ("F", "Chocolate", "#D2691E"),
    ("G", "Gold", "#FFD700"),
    ("H", "Crimson", "#DC143C"),
    ("I", "Indigo", "#4B0082"),
    ("J", "Hotpink", "#FF69B4"),
    ("K", "Orange", "#FFA500"),
    ("L", "Purple", "#800080"),
    ("M", "Lime", "#00FF00"),
    ("N", "Aliceblue", "#F0F8FF"),
    ("O", "Ivory", "#FFFFF0"),
    ("P", "Lavender", "#E6E6FA"),
    ("Q", "Mistyrose", "#FFE4E1"),
    ("R", "Papayawhip", "#FFEFD5"),
    ("S", "Seashell", "#FFF5EE"),
    ("T", "Silver", "#C0C0C0"),
    ("U", "Lightgray", "#D3D3D3"),
    ("V", "Darkslategray", "#2F4F4F"),
    ("W", "Dimgray", "#696969"),
]
COLOR_HEX = [item[2] for item in COLOR_TABLE]


@dataclass(frozen=True)
class ShapeDef:
    opcode: int
    code: str
    name: str


SHAPES: List[ShapeDef] = [
    ShapeDef(0x0, "0", "Text"),
    ShapeDef(0x1, "1", "Line"),
    ShapeDef(0x2, "2", "Rectangle"),
    ShapeDef(0x3, "3", "Ellipse"),
    ShapeDef(0x4, "4", "Triangle Outline"),
    ShapeDef(0x5, "5", "Triangle Fill"),
    ShapeDef(0x6, "6", "Arrow"),
    ShapeDef(0x7, "7", "Star"),
    ShapeDef(0x8, "8", "SemiCircle / Arc"),
    ShapeDef(0x9, "9", "Yagi Antenna"),
    ShapeDef(0xA, "A", "Dish Antenna"),
    ShapeDef(0xB, "B", "Radio Transceiver"),
    ShapeDef(0xC, "C", "Radio Waves"),
    ShapeDef(0xD, "D", "Moon"),
    ShapeDef(0xE, "E", "DoubleBox"),
]
SHAPE_BY_OPCODE = {shape.opcode: shape for shape in SHAPES}
REPEAT_TRANSLATED_OPCODE = 0xF


class CodecError(ValueError):
    pass


class FrameError(CodecError):
    pass


@dataclass
class DrawCommand:
    opcode: int
    color: int
    fields: Dict[str, Any] = field(default_factory=dict)

    def clone(self) -> "DrawCommand":
        return DrawCommand(self.opcode, self.color, copy.deepcopy(self.fields))

    @property
    def shape(self) -> ShapeDef:
        try:
            return SHAPE_BY_OPCODE[self.opcode]
        except KeyError as exc:
            raise CodecError(f"Unknown opcode {self.opcode}.") from exc

    def to_json(self) -> Dict[str, Any]:
        return {
            "opcode": self.opcode,
            "shape": self.shape.code,
            "color": self.color,
            "fields": copy.deepcopy(self.fields),
        }


@dataclass
class StreamState:
    has_point: bool = False
    x: int = 0
    y: int = 0
    has_opcode: bool = False
    opcode: int = 0
    has_color: bool = False
    color: int = 0
    params: Dict[str, int] = field(default_factory=dict)
    previous_command: Optional[DrawCommand] = None


# ---------------------------------------------------------------------------
# General helpers
# ---------------------------------------------------------------------------


def load_text_font(font_size: int) -> Tuple[ImageFont.ImageFont, str]:
    """Load a real scalable font; never silently fall back to the tiny bitmap font."""
    font_size = max(1, int(font_size))

    candidates = [
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",              # Arch Linux
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
        "DejaVuSans-Bold.ttf",
        "DejaVuSans.ttf",
    ]

    # Ask fontconfig as a final system-font lookup before using Pillow's default.
    try:
        matched = subprocess.run(
            ["fc-match", "-f", "%{file}", "DejaVu Sans:style=Bold"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout.strip()
        if matched:
            candidates.append(matched)
    except (OSError, subprocess.SubprocessError):
        pass

    tried = set()
    for candidate in candidates:
        if not candidate or candidate in tried:
            continue
        tried.add(candidate)
        try:
            return ImageFont.truetype(candidate, font_size), candidate
        except OSError:
            continue

    # Newer Pillow versions can scale the bundled default font. This remains
    # visibly larger than the historic unscaled 8-pixel fallback.
    try:
        return ImageFont.load_default(size=font_size), f"Pillow default at {font_size}px"
    except TypeError as exc:
        raise CodecError(
            "No scalable font was found. Install one with: sudo pacman -S ttf-dejavu"
        ) from exc


def clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(value)))


def decode_base62(text: str) -> int:
    value = 0
    for ch in text:
        try:
            digit = BASE62.index(ch)
        except ValueError as exc:
            raise FrameError(f"Invalid Base62 character {ch!r}.") from exc
        value = value * len(BASE62) + digit
    return value


def command_anchor(command: DrawCommand) -> Tuple[int, int]:
    fields = command.fields
    if "x" in fields and "y" in fields:
        return int(fields["x"]), int(fields["y"])
    return int(fields["x1"]), int(fields["y1"])


def coordinate_keys(command: DrawCommand) -> List[Tuple[str, str]]:
    if command.opcode in {0x1, 0x2, 0xE}:
        return [("x1", "y1"), ("x2", "y2")]
    return [("x", "y")]


def translated_clone(command: DrawCommand, dx: int, dy: int) -> DrawCommand:
    result = command.clone()
    for x_key, y_key in coordinate_keys(result):
        result.fields[x_key] = clamp(int(result.fields[x_key]) + dx, 0, CANVAS_W - 1)
        result.fields[y_key] = clamp(int(result.fields[y_key]) + dy, 0, CANVAS_H - 1)
    return result


# ---------------------------------------------------------------------------
# Bit-level decoder
# ---------------------------------------------------------------------------


class BitReader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.bit_pos = 0

    def remaining(self) -> int:
        return len(self.data) * 8 - self.bit_pos

    def read_bit(self) -> int:
        if self.remaining() < 1:
            raise CodecError("Unexpected end of compressed bitstream.")
        byte = self.data[self.bit_pos // 8]
        bit = (byte >> (7 - (self.bit_pos % 8))) & 1
        self.bit_pos += 1
        return bit

    def read_bits(self, width: int) -> int:
        if width < 0 or self.remaining() < width:
            raise CodecError("Unexpected end of compressed bitstream.")
        value = 0
        for _ in range(width):
            value = (value << 1) | self.read_bit()
        return value

    def read_ue(self, max_value: int = 1_000_000) -> int:
        zeros = 0
        while self.read_bit() == 0:
            zeros += 1
            if zeros > 31:
                raise CodecError("Exp-Golomb prefix is unreasonably long.")
        suffix = self.read_bits(zeros) if zeros else 0
        value = (1 << zeros) + suffix - 1
        if value > max_value:
            raise CodecError(f"Decoded Exp-Golomb value {value} exceeds limit.")
        return value

    def read_rice_unsigned(self, k: int, max_value: int = 1_000_000) -> int:
        quotient = 0
        while self.read_bit() == 1:
            quotient += 1
            if quotient > 4096:
                raise CodecError("Rice quotient is unreasonably long.")
        remainder = self.read_bits(k) if k else 0
        value = (quotient << k) | remainder
        if value > max_value:
            raise CodecError(f"Decoded Rice value {value} exceeds limit.")
        return value

    def read_rice_signed(self, k: int, max_abs: int = 10_000) -> int:
        zigzag = self.read_rice_unsigned(k, max_value=max_abs * 2 + 1)
        value = -(zigzag // 2) - 1 if zigzag & 1 else zigzag // 2
        if abs(value) > max_abs:
            raise CodecError(f"Decoded signed Rice value {value} exceeds limit.")
        return value


def read_stateful_fixed(
    reader: BitReader,
    state: StreamState,
    key: str,
    width: int,
) -> int:
    same = bool(reader.read_bit())
    if same:
        if key not in state.params:
            raise CodecError(f"Stateful field {key!r} referenced before initialization.")
        return state.params[key]
    value = reader.read_bits(width)
    state.params[key] = value
    return value


def read_stateful_ue(
    reader: BitReader,
    state: StreamState,
    key: str,
    max_value: int,
) -> int:
    same = bool(reader.read_bit())
    if same:
        if key not in state.params:
            raise CodecError(f"Stateful field {key!r} referenced before initialization.")
        return state.params[key]
    value = reader.read_ue(max_value=max_value)
    state.params[key] = value
    return value


def read_point(reader: BitReader, state: StreamState) -> Tuple[int, int]:
    use_delta = bool(reader.read_bit())
    if use_delta:
        if not state.has_point:
            raise CodecError("Delta point referenced before an absolute point.")
        x = state.x + reader.read_rice_signed(3, max_abs=CANVAS_W * 2)
        y = state.y + reader.read_rice_signed(3, max_abs=CANVAS_H * 2)
    else:
        x = reader.read_bits(10)
        y = reader.read_bits(9)

    if not (0 <= x < CANVAS_W and 0 <= y < CANVAS_H):
        raise CodecError(f"Decoded point ({x}, {y}) is outside the canvas.")

    state.x = x
    state.y = y
    state.has_point = True
    return x, y


def read_relative_point(reader: BitReader, x1: int, y1: int) -> Tuple[int, int]:
    use_delta = bool(reader.read_bit())
    if use_delta:
        x2 = x1 + reader.read_rice_signed(3, max_abs=CANVAS_W * 2)
        y2 = y1 + reader.read_rice_signed(3, max_abs=CANVAS_H * 2)
    else:
        x2 = reader.read_bits(10)
        y2 = reader.read_bits(9)

    if not (0 <= x2 < CANVAS_W and 0 <= y2 < CANVAS_H):
        raise CodecError(f"Decoded second point ({x2}, {y2}) is outside the canvas.")
    return x2, y2


def read_translation(reader: BitReader) -> Tuple[int, int]:
    use_rice = bool(reader.read_bit())
    if use_rice:
        return (
            reader.read_rice_signed(2, max_abs=719),
            reader.read_rice_signed(2, max_abs=479),
        )
    return reader.read_bits(11) - 719, reader.read_bits(10) - 479


def read_normal_opcode(reader: BitReader, state: StreamState) -> int:
    same = bool(reader.read_bit())
    if same:
        if not state.has_opcode:
            raise CodecError("Same-opcode flag used before opcode initialization.")
        return state.opcode

    opcode = reader.read_bits(4)
    if opcode == REPEAT_TRANSLATED_OPCODE or opcode not in SHAPE_BY_OPCODE:
        raise CodecError(f"Invalid normal command opcode {opcode}.")
    state.opcode = opcode
    state.has_opcode = True
    return opcode


def read_color(reader: BitReader, state: StreamState) -> int:
    same = bool(reader.read_bit())
    if same:
        if not state.has_color:
            raise CodecError("Same-color flag used before color initialization.")
        return state.color

    color = reader.read_bits(6)
    if not 0 <= color < len(COLOR_TABLE):
        raise CodecError(f"Decoded invalid color index {color}.")
    state.color = color
    state.has_color = True
    return color


def decode_command_fields(
    reader: BitReader,
    state: StreamState,
    opcode: int,
    color: int,
) -> DrawCommand:
    fields: Dict[str, Any] = {}

    if opcode in {0x1, 0x2, 0xE}:
        x1, y1 = read_point(reader, state)
        x2, y2 = read_relative_point(reader, x1, y1)
        fields.update(x1=x1, y1=y1, x2=x2, y2=y2)
    else:
        x, y = read_point(reader, state)
        fields.update(x=x, y=y)

    if opcode == 0x0:
        length = reader.read_ue(max_value=MAX_TEXT_LEN)
        chars: List[str] = []
        for _ in range(length):
            index = reader.read_bits(6)
            if index >= len(TEXT_ALPHABET):
                raise CodecError(f"Invalid text symbol index {index}.")
            chars.append(TEXT_ALPHABET[index])
        fields["text"] = "".join(chars)
    elif opcode == 0x1:
        pass
    elif opcode == 0x2:
        fields["fill"] = read_stateful_fixed(reader, state, "fill", 1)
    elif opcode == 0x3:
        fields["radius_h"] = read_stateful_ue(reader, state, "radius_h", 127) + 1
        fields["radius_w"] = read_stateful_ue(reader, state, "radius_w", 127) + 1
        fields["scale"] = read_stateful_ue(reader, state, "scale", 63) + 1
        fields["fill"] = read_stateful_fixed(reader, state, "fill", 1)
    elif opcode in {0x4, 0x5, 0x6, 0x9, 0xA, 0xB}:
        fields["orientation"] = read_stateful_fixed(reader, state, "orientation", 2)
        fields["scale"] = read_stateful_ue(reader, state, "scale", 63) + 1
    elif opcode == 0x7:
        fields["radius"] = read_stateful_ue(reader, state, "radius", 127) + 1
        fields["scale"] = read_stateful_ue(reader, state, "scale", 63) + 1
    elif opcode in {0x8, 0xC}:
        fields["radius"] = read_stateful_ue(reader, state, "radius", 127) + 1
        fields["scale"] = read_stateful_ue(reader, state, "scale", 63) + 1
        fields["start_angle"] = read_stateful_fixed(reader, state, "start_angle", 9)
        fields["arc_degrees"] = read_stateful_fixed(reader, state, "arc_degrees", 9)
    elif opcode == 0xD:
        fields["scale"] = read_stateful_ue(reader, state, "scale", 63) + 1
        crater_color = read_stateful_fixed(reader, state, "crater_color", 6)
        if crater_color >= len(COLOR_TABLE):
            raise CodecError(f"Invalid crater color index {crater_color}.")
        fields["crater_color"] = crater_color
    elif opcode == 0xE:
        fields["percent"] = read_stateful_ue(reader, state, "percent", 100)
    else:  # pragma: no cover - opcode checked earlier
        raise CodecError(f"Unsupported opcode {opcode}.")

    command = DrawCommand(opcode, color, fields)
    validate_command(command)
    return command


def decode_commands_from_bits(data: bytes) -> List[DrawCommand]:
    reader = BitReader(data)
    version = reader.read_bits(4)
    if version != PROTOCOL_VERSION:
        raise CodecError(f"Unsupported MCoreIMG bitstream version {version}.")

    count = reader.read_ue(max_value=MAX_EDITOR_COMMANDS)
    state = StreamState()
    commands: List[DrawCommand] = []

    for command_index in range(count):
        try:
            is_repeat = bool(reader.read_bit())
            if is_repeat:
                if state.previous_command is None:
                    raise CodecError("Translated-repeat command has no previous command.")
                dx, dy = read_translation(reader)
                command = translated_clone(state.previous_command, dx, dy)
                validate_command(command)
                state.x, state.y = command_anchor(command)
                state.has_point = True
                state.previous_command = command.clone()
                commands.append(command)
                continue

            opcode = read_normal_opcode(reader, state)
            color = read_color(reader, state)
            command = decode_command_fields(reader, state, opcode, color)
            state.previous_command = command.clone()
            commands.append(command)
        except CodecError as exc:
            raise CodecError(f"Command {command_index}: {exc}") from exc

    return commands


# ---------------------------------------------------------------------------
# Base91 and MeshCore frame layer
# ---------------------------------------------------------------------------


def base91_decode(text: str) -> bytes:
    accumulator = 0
    bit_count = 0
    value = -1
    output = bytearray()

    for ch in text:
        if ch not in BASE91_INDEX:
            raise CodecError(f"Invalid Base91 character {ch!r}.")
        digit = BASE91_INDEX[ch]
        if value < 0:
            value = digit
        else:
            value += digit * 91
            accumulator |= value << bit_count
            if value & 8191 > 88:
                bit_count += 13
            else:
                bit_count += 14
            while bit_count >= 8:
                output.append(accumulator & 255)
                accumulator >>= 8
                bit_count -= 8
            value = -1

    if value >= 0:
        accumulator |= value << bit_count
        bit_count += 7
        while bit_count >= 8:
            output.append(accumulator & 255)
            accumulator >>= 8
            bit_count -= 8

    return bytes(output)


def frame_crc(payload: str) -> int:
    return binascii.crc_hqx(payload.encode("ascii"), 0xFFFF)


def parse_frame(frame: str) -> Dict[str, Any]:
    frame = frame.rstrip("\r\n")
    if len(frame) < FRAME_HEADER_LEN:
        raise FrameError("Frame is shorter than the 15-character control header.")
    if len(frame) > MESSAGE_LEN:
        raise FrameError(f"Frame exceeds {MESSAGE_LEN} characters.")

    header = frame[:FRAME_HEADER_LEN]
    payload = frame[FRAME_HEADER_LEN:]

    if header[:3] != FRAME_MAGIC:
        raise FrameError("Frame does not begin with MCI.")

    version = decode_base62(header[3])
    if version != PROTOCOL_VERSION:
        raise FrameError(f"Unsupported frame version {version}.")

    image_id = header[4:7]
    if len(image_id) != 3 or any(ch not in BASE62 for ch in image_id):
        raise FrameError("Frame contains an invalid image ID.")

    part_index = decode_base62(header[7])
    total_parts = decode_base62(header[8])
    payload_length = decode_base62(header[9:11])
    expected_crc = decode_base62(header[11:14])
    flags = decode_base62(header[14])

    if payload_length != len(payload):
        raise FrameError(
            f"Frame payload length mismatch: header says {payload_length}, "
            f"received {len(payload)}."
        )
    if payload_length > FRAME_PAYLOAD_LEN:
        raise FrameError(f"Frame payload exceeds {FRAME_PAYLOAD_LEN} characters.")
    if any(ch not in BASE91_INDEX for ch in payload):
        raise FrameError("Frame payload contains a character outside the MCoreIMG Base91 alphabet.")

    actual_crc = frame_crc(payload)
    if expected_crc != actual_crc:
        raise FrameError(
            "Frame CRC-16 mismatch; request retransmission of this part."
        )

    if not 0 <= part_index < total_parts <= MAX_MESSAGES:
        raise FrameError("Invalid frame part/total values.")

    return {
        "image_id": image_id,
        "part_index": part_index,
        "total_parts": total_parts,
        "payload": payload,
        "flags": flags,
    }


def assemble_frames(frames: Iterable[str]) -> Tuple[str, bytes, int]:
    parsed: List[Dict[str, Any]] = []
    for line_number, frame in enumerate(frames, start=1):
        if not frame.strip():
            continue
        try:
            parsed.append(parse_frame(frame))
        except FrameError as exc:
            raise FrameError(f"Frame line {line_number}: {exc}") from exc

    if not parsed:
        raise FrameError("No MCoreIMG frames found.")

    image_ids = {item["image_id"] for item in parsed}
    totals = {item["total_parts"] for item in parsed}
    if len(image_ids) != 1 or len(totals) != 1:
        raise FrameError("Frames belong to different images or disagree on total parts.")

    image_id = next(iter(image_ids))
    total = next(iter(totals))
    parts: Dict[int, str] = {}
    duplicate_count = 0

    for item in parsed:
        index = item["part_index"]
        if index in parts:
            if parts[index] != item["payload"]:
                raise FrameError(f"Conflicting duplicates for frame part {index}.")
            duplicate_count += 1
            continue
        parts[index] = item["payload"]

    missing = [index for index in range(total) if index not in parts]
    if missing:
        missing_text = ", ".join(str(index) for index in missing)
        raise FrameError(f"Missing frame part(s): {missing_text}.")

    payload = "".join(parts[index] for index in range(total))
    raw_with_crc = base91_decode(payload)
    if len(raw_with_crc) < 4:
        raise CodecError("Assembled stream is too short for CRC-32.")

    raw = raw_with_crc[:-4]
    expected_crc = int.from_bytes(raw_with_crc[-4:], "big")
    actual_crc = zlib.crc32(raw) & 0xFFFFFFFF
    if expected_crc != actual_crc:
        raise CodecError("Assembled stream CRC-32 mismatch.")

    return image_id, raw, duplicate_count


def decode_image_frames(frames: Iterable[str]) -> Tuple[str, List[DrawCommand], int]:
    image_id, packed, duplicate_count = assemble_frames(frames)
    return image_id, decode_commands_from_bits(packed), duplicate_count


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _require_int(fields: Dict[str, Any], key: str, lo: int, hi: int) -> int:
    if key not in fields:
        raise CodecError(f"Command is missing field {key!r}.")
    try:
        value = int(fields[key])
    except (TypeError, ValueError) as exc:
        raise CodecError(f"Command field {key!r} must be an integer.") from exc
    if not lo <= value <= hi:
        raise CodecError(f"Command field {key!r}={value} must be {lo}..{hi}.")
    fields[key] = value
    return value


def validate_command(command: DrawCommand) -> None:
    if command.opcode not in SHAPE_BY_OPCODE:
        raise CodecError(f"Unknown command opcode {command.opcode}.")
    if not 0 <= int(command.color) < len(COLOR_TABLE):
        raise CodecError(f"Invalid color index {command.color}.")

    fields = command.fields
    opcode = command.opcode

    if opcode in {0x1, 0x2, 0xE}:
        _require_int(fields, "x1", 0, CANVAS_W - 1)
        _require_int(fields, "y1", 0, CANVAS_H - 1)
        _require_int(fields, "x2", 0, CANVAS_W - 1)
        _require_int(fields, "y2", 0, CANVAS_H - 1)
    else:
        _require_int(fields, "x", 0, CANVAS_W - 1)
        _require_int(fields, "y", 0, CANVAS_H - 1)

    if opcode == 0x0:
        text = str(fields.get("text", "")).upper()[:MAX_TEXT_LEN]
        fields["text"] = "".join(ch if ch in TEXT_ALPHABET else " " for ch in text).rstrip()
    elif opcode == 0x2:
        _require_int(fields, "fill", 0, 1)
    elif opcode == 0x3:
        _require_int(fields, "radius_h", 1, 128)
        _require_int(fields, "radius_w", 1, 128)
        _require_int(fields, "scale", 1, 64)
        _require_int(fields, "fill", 0, 1)
    elif opcode in {0x4, 0x5, 0x6, 0x9, 0xA, 0xB}:
        _require_int(fields, "orientation", 0, 3)
        _require_int(fields, "scale", 1, 64)
    elif opcode == 0x7:
        _require_int(fields, "radius", 1, 128)
        _require_int(fields, "scale", 1, 64)
    elif opcode in {0x8, 0xC}:
        _require_int(fields, "radius", 1, 128)
        _require_int(fields, "scale", 1, 64)
        _require_int(fields, "start_angle", 0, 360)
        _require_int(fields, "arc_degrees", 0, 360)
    elif opcode == 0xD:
        _require_int(fields, "scale", 1, 64)
        _require_int(fields, "crater_color", 0, len(COLOR_TABLE) - 1)
    elif opcode == 0xE:
        _require_int(fields, "percent", 0, 100)


# ---------------------------------------------------------------------------
# Rendering — mirrors the current constructor geometry
# ---------------------------------------------------------------------------


def rotate_point(
    px: float,
    py: float,
    origin_x: float,
    origin_y: float,
    orientation: int,
) -> Tuple[int, int]:
    dx = px - origin_x
    dy = py - origin_y
    orientation %= 4
    if orientation == 0:
        rx, ry = dx, dy
    elif orientation == 1:
        rx, ry = -dy, dx
    elif orientation == 2:
        rx, ry = -dx, -dy
    else:
        rx, ry = dy, -dx
    return round(origin_x + rx), round(origin_y + ry)


def polygon_regular(
    center_x: int,
    center_y: int,
    radius: int,
    sides: int,
    rotation_degrees: float,
) -> List[Tuple[int, int]]:
    points: List[Tuple[int, int]] = []
    for index in range(sides):
        angle = math.radians(rotation_degrees + index * 360.0 / sides)
        points.append(
            (
                round(center_x + radius * math.cos(angle)),
                round(center_y + radius * math.sin(angle)),
            )
        )
    return points


def draw_line(
    draw: ImageDraw.ImageDraw,
    point1: Tuple[float, float],
    point2: Tuple[float, float],
    color: str,
    width: int = 2,
) -> None:
    draw.line([point1, point2], fill=color, width=width)


def draw_arrow(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    orientation: int,
    scale: int,
    fill: str,
) -> None:
    scale = max(1, scale)
    points = [
        (x, y),
        (x - 8 * scale, y + 18 * scale),
        (x - 3 * scale, y + 18 * scale),
        (x - 3 * scale, y + 45 * scale),
        (x + 3 * scale, y + 45 * scale),
        (x + 3 * scale, y + 18 * scale),
        (x + 8 * scale, y + 18 * scale),
    ]
    points = [rotate_point(px, py, x, y, orientation) for px, py in points]
    draw.polygon(points, fill=fill, outline=fill)


def draw_star(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    radius: int,
    scale: int,
    color: str,
) -> None:
    if scale <= 0 or radius <= 0:
        return
    rendered_radius = radius * scale
    diagonal = round(rendered_radius / math.sqrt(2))
    lines = [
        ((x, y - rendered_radius), (x, y + rendered_radius)),
        ((x - rendered_radius, y), (x + rendered_radius, y)),
        ((x - diagonal, y - diagonal), (x + diagonal, y + diagonal)),
        ((x + diagonal, y - diagonal), (x - diagonal, y + diagonal)),
    ]
    for point1, point2 in lines:
        draw_line(draw, point1, point2, color, width=3)


def draw_yagi(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    orientation: int,
    scale: int,
    color: str,
) -> None:
    scale = max(1, scale)
    axis_start = rotate_point(x + 30 * scale, y, x, y, orientation)
    axis_end = rotate_point(x, y + 100 * scale, x, y, orientation)
    width = max(1, 2 * scale)
    draw_line(draw, axis_start, axis_end, color, width)

    for position in (0.15, 0.35, 0.55, 0.75):
        axis_x = (1 - position) * (x + 30 * scale) + position * x
        axis_y = (1 - position) * y + position * (y + 100 * scale)
        point1 = rotate_point(
            axis_x - 8 * scale,
            axis_y - 3 * scale,
            x,
            y,
            orientation,
        )
        point2 = rotate_point(
            axis_x + 8 * scale,
            axis_y + 3 * scale,
            x,
            y,
            orientation,
        )
        draw_line(draw, point1, point2, color, width)


def draw_dish(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    orientation: int,
    scale: int,
    color: str,
) -> None:
    if scale <= 0:
        return

    facing = orientation % 2
    scale = max(1, scale)
    radius = 40 * scale
    hub_radius = 5 * scale
    mast_length = 30 * scale
    width = 3
    flip = -1 if facing == 0 else 1

    arc_points: List[Tuple[int, int]] = []
    for angle in range(90, 181, 5):
        theta = math.radians(angle)
        dx = round(radius * math.cos(theta)) * flip
        dy = round(radius * math.sin(theta))
        arc_points.append((x + dx, y + dy))
    draw.line(arc_points, fill=color, width=width)

    end1_dx = -radius * flip
    end1_dy = 0
    end2_dx = 0
    end2_dy = radius
    draw_line(draw, (x, y), (x + end1_dx, y + end1_dy), color, width)
    draw_line(draw, (x, y), (x + end2_dx, y + end2_dy), color, width)

    midpoint_x = round((-radius / math.sqrt(2)) * flip)
    midpoint_y = round(radius / math.sqrt(2))
    draw_line(
        draw,
        (x + midpoint_x, y + midpoint_y),
        (x + midpoint_x, y + midpoint_y + mast_length),
        color,
        width,
    )

    draw.ellipse(
        (x - hub_radius, y - hub_radius, x + hub_radius, y + hub_radius),
        fill=color,
        outline=color,
    )


def draw_radio(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    orientation: int,
    scale: int,
    color: str,
) -> None:
    # The constructor currently stores orientation but intentionally renders the
    # radio in its original left-to-right orientation.
    del orientation
    if scale <= 0:
        return

    scale = max(1, scale)
    body_width = 50 * scale
    body_height = 20 * scale
    knob_radius = 5 * scale
    knob_x = x + 10 * scale
    knob_y = y + 10 * scale
    screen_x1 = x + 25 * scale
    screen_y1 = y + 5 * scale
    screen_x2 = x + 45 * scale
    screen_y2 = y + 15 * scale
    width = 3

    draw.rectangle((x, y, x + body_width, y + body_height), outline=color, width=width)
    draw.ellipse(
        (
            knob_x - knob_radius,
            knob_y - knob_radius,
            knob_x + knob_radius,
            knob_y + knob_radius,
        ),
        outline=color,
        width=width,
    )
    draw.rectangle(
        (screen_x1, screen_y1, screen_x2, screen_y2),
        outline=color,
        width=width,
    )


def draw_arc_line(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    radius: int,
    start_angle: int,
    arc_degrees: int,
    color: str,
) -> None:
    if radius <= 0:
        return

    start_angle %= 360
    arc_degrees = clamp(arc_degrees, 0, 360)
    if arc_degrees <= 0:
        return

    arc_points: List[Tuple[int, int]] = []
    # Match the constructor exactly: five-degree samples beginning at the
    # requested start angle. A non-multiple-of-five sweep ends at the final
    # sample before the requested endpoint.
    for angle_value in range(start_angle, start_angle + arc_degrees + 1, 5):
        theta = math.radians(angle_value % 360)
        arc_points.append(
            (
                round(x + radius * math.cos(theta)),
                round(y - radius * math.sin(theta)),
            )
        )

    if len(arc_points) >= 2:
        draw.line(arc_points, fill=color, width=3)


def draw_radio_waves(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    radius: int,
    scale: int,
    start_angle: int,
    arc_degrees: int,
    color: str,
) -> None:
    if scale <= 0 or radius <= 0 or arc_degrees <= 0:
        return
    base_radius = radius * scale
    spacing = 2 * radius * scale
    for offset in (0, spacing, 2 * spacing):
        draw_arc_line(
            draw,
            x,
            y,
            base_radius + offset,
            start_angle,
            arc_degrees,
            color,
        )


def draw_moon(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    scale: int,
    moon_color: str,
    crater_color: str,
) -> None:
    if scale <= 0:
        return
    scale = max(1, scale)
    moon_radius = 36 * scale
    left = x - moon_radius
    top = y - moon_radius
    right = x + moon_radius
    bottom = y + moon_radius
    draw.ellipse((left, top, right, bottom), fill=moon_color, outline=moon_color)

    diameter = moon_radius * 2
    crater_points = [
        (1 / 5, 1 / 4, 3),
        (3 / 7, 5 / 8, 5),
        (1 / 4, 7 / 9, 4),
        (4 / 5, 2 / 7, 2),
        (7 / 12, 1 / 5, 6),
        (2 / 3, 2 / 5, 3),
        (5 / 8, 3 / 4, 4),
        (7 / 20, 3 / 7, 2),
        (3 / 20, 5 / 9, 5),
        (3 / 4, 3 / 5, 3),
    ]
    for fraction_x, fraction_y, base_radius in crater_points:
        center_x = left + round(diameter * fraction_x)
        center_y = top + round(diameter * fraction_y)
        crater_radius = base_radius * scale
        draw.ellipse(
            (
                center_x - crater_radius,
                center_y - crater_radius,
                center_x + crater_radius,
                center_y + crater_radius,
            ),
            outline=crater_color,
            width=3,
        )


def draw_double_box(
    draw: ImageDraw.ImageDraw,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    percent: int,
    color: str,
) -> None:
    left, right = min(x1, x2), max(x1, x2)
    top, bottom = min(y1, y2), max(y1, y2)
    divider_y = top + round((bottom - top) * clamp(percent, 0, 100) / 100)
    draw.rectangle([left, top, right, bottom], outline=color, width=2)
    draw.line([(left, divider_y), (right, divider_y)], fill=color, width=2)


def render_command(
    command: DrawCommand,
    target: ImageDraw.ImageDraw,
    text_font: ImageFont.ImageFont,
) -> None:
    validate_command(command)
    fields = command.fields
    opcode = command.opcode
    color = COLOR_HEX[command.color]

    if opcode == 0x0:
        target.text(
            (fields["x"], fields["y"]),
            fields["text"],
            fill=color,
            font=text_font,
            anchor="lt",
        )
    elif opcode == 0x1:
        draw_line(
            target,
            (fields["x1"], fields["y1"]),
            (fields["x2"], fields["y2"]),
            color,
            2,
        )
    elif opcode == 0x2:
        left, right = min(fields["x1"], fields["x2"]), max(fields["x1"], fields["x2"])
        top, bottom = min(fields["y1"], fields["y2"]), max(fields["y1"], fields["y2"])
        target.rectangle(
            [left, top, right, bottom],
            outline=color,
            fill=color if fields["fill"] else None,
            width=2,
        )
    elif opcode == 0x3:
        radius_x = fields["radius_w"] * fields["scale"]
        radius_y = fields["radius_h"] * fields["scale"]
        box = [
            fields["x"] - radius_x,
            fields["y"] - radius_y,
            fields["x"] + radius_x,
            fields["y"] + radius_y,
        ]
        target.ellipse(
            box,
            outline=color,
            fill=color if fields["fill"] else None,
            width=2,
        )
    elif opcode in {0x4, 0x5}:
        points = polygon_regular(
            fields["x"],
            fields["y"],
            18 * fields["scale"],
            3,
            -90 + fields["orientation"] * 90,
        )
        if opcode == 0x5:
            target.polygon(points, outline=color, fill=color)
        else:
            target.line(points + [points[0]], fill=color, width=2)
    elif opcode == 0x6:
        draw_arrow(
            target,
            fields["x"],
            fields["y"],
            fields["orientation"],
            fields["scale"],
            color,
        )
    elif opcode == 0x7:
        draw_star(
            target,
            fields["x"],
            fields["y"],
            fields["radius"],
            fields["scale"],
            color,
        )
    elif opcode == 0x8:
        draw_arc_line(
            target,
            fields["x"],
            fields["y"],
            fields["radius"] * fields["scale"],
            fields["start_angle"],
            fields["arc_degrees"],
            color,
        )
    elif opcode == 0x9:
        draw_yagi(
            target,
            fields["x"],
            fields["y"],
            fields["orientation"],
            fields["scale"],
            color,
        )
    elif opcode == 0xA:
        draw_dish(
            target,
            fields["x"],
            fields["y"],
            fields["orientation"],
            fields["scale"],
            color,
        )
    elif opcode == 0xB:
        draw_radio(
            target,
            fields["x"],
            fields["y"],
            fields["orientation"],
            fields["scale"],
            color,
        )
    elif opcode == 0xC:
        draw_radio_waves(
            target,
            fields["x"],
            fields["y"],
            fields["radius"],
            fields["scale"],
            fields["start_angle"],
            fields["arc_degrees"],
            color,
        )
    elif opcode == 0xD:
        draw_moon(
            target,
            fields["x"],
            fields["y"],
            fields["scale"],
            color,
            COLOR_HEX[fields["crater_color"]],
        )
    elif opcode == 0xE:
        draw_double_box(
            target,
            fields["x1"],
            fields["y1"],
            fields["x2"],
            fields["y2"],
            fields["percent"],
            color,
        )
    else:  # pragma: no cover - validated earlier
        raise CodecError(f"Unsupported opcode {opcode}.")


def render_image(
    commands: Sequence[DrawCommand],
    output_path: Path,
    text_font_size: int = DEFAULT_TEXT_FONT_SIZE,
) -> str:
    image = Image.new("RGB", (CANVAS_W, CANVAS_H), BACKGROUND)
    draw = ImageDraw.Draw(image)
    text_font, font_source = load_text_font(text_font_size)
    for index, command in enumerate(commands):
        try:
            render_command(command, draw, text_font)
        except Exception as exc:
            image.close()
            raise CodecError(f"Render failed on command {index}: {exc}") from exc
    image.save(output_path)
    image.close()
    return font_source


# ---------------------------------------------------------------------------
# Input/output handling
# ---------------------------------------------------------------------------


def select_input_file() -> Optional[str]:
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"Could not open file chooser: {exc}", file=sys.stderr)
        return None

    root.withdraw()
    try:
        root.attributes("-topmost", True)
    except tk.TclError:
        pass

    filename = filedialog.askopenfilename(
        title="Select MCoreIMG transport file",
        filetypes=[
            ("MCoreIMG transport", "*.mci"),
            ("Text files", "*.txt"),
            ("All files", "*.*"),
        ],
    )
    root.destroy()
    return filename or None


def extract_frames_from_text(text: str) -> Tuple[List[str], int]:
    """Extract complete MCI frames from direct exports or prefixed log lines.

    Direct .mci exports are one frame per line. For captured chat/log files, a
    frame may follow a timestamp, sender, or other prefix. The 15-character
    header contains the exact payload length, allowing safe extraction without
    treating unrelated messages as drawing data.
    """
    frames: List[str] = []
    ignored_nonempty = 0

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        found_on_line = False
        search_from = 0
        while True:
            start = line.find(FRAME_MAGIC, search_from)
            if start < 0:
                break
            search_from = start + len(FRAME_MAGIC)

            if len(line) - start < FRAME_HEADER_LEN:
                continue
            header = line[start : start + FRAME_HEADER_LEN]
            try:
                if header[:3] != FRAME_MAGIC:
                    continue
                payload_length = decode_base62(header[9:11])
            except FrameError:
                continue

            frame_length = FRAME_HEADER_LEN + payload_length
            candidate = line[start : start + frame_length]
            if len(candidate) != frame_length:
                continue

            frames.append(candidate)
            found_on_line = True
            break

        if not found_on_line:
            ignored_nonempty += 1

    return frames, ignored_nonempty


def default_output_path(input_path: Path, image_id: str) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return input_path.with_name(f"{timestamp}-MCoreIMG-{image_id}.png")


def write_source_json(
    output_path: Path,
    commands: Sequence[DrawCommand],
    image_id: str,
) -> Path:
    json_path = output_path.with_suffix(".mci.json")
    document = {
        "format": SOURCE_FORMAT,
        "version": SOURCE_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "canvas": {"width": CANVAS_W, "height": CANVAS_H},
        "metadata": {
            "grid": DEFAULT_GRID,
            "grid_note": "Grid is source metadata and is not transmitted in MCoreIMG frames.",
            "reconstructed_image_id": image_id,
        },
        "commands": [command.to_json() for command in commands],
    }
    json_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return json_path


def command_summary(command: DrawCommand) -> str:
    fields = command.fields
    color_name = COLOR_TABLE[command.color][1]
    if command.opcode == 0x0:
        detail = f"({fields['x']},{fields['y']}) {fields.get('text', '')!r}"
    elif command.opcode in {0x1, 0x2, 0xE}:
        detail = f"({fields['x1']},{fields['y1']}) -> ({fields['x2']},{fields['y2']})"
    else:
        detail = f"({fields['x']},{fields['y']})"
    return f"{command.shape.code} {command.shape.name} | {color_name} | {detail}"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Decode and render MCoreIMG compressed MeshCore frames."
    )
    parser.add_argument(
        "input",
        nargs="?",
        help="MCoreIMG .mci transport file. Opens a file chooser when omitted.",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="PNG output path. Defaults beside the input file with timestamp and image ID.",
    )
    parser.add_argument(
        "--dump-json",
        action="store_true",
        help="Also write decoded commands as constructor-compatible .mci.json source.",
    )
    parser.add_argument(
        "--list-commands",
        action="store_true",
        help="Print the decoded drawing-command list.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    input_name = args.input or select_input_file()
    if not input_name:
        print("No input file selected.")
        return 1

    input_path = Path(input_name).expanduser().resolve()
    if not input_path.is_file():
        print(f"Input file does not exist: {input_path}", file=sys.stderr)
        return 2

    try:
        text = input_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        print(f"Input is not valid UTF-8/ASCII text: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"Could not read input file: {exc}", file=sys.stderr)
        return 2

    frames, ignored_lines = extract_frames_from_text(text)
    if not frames:
        print("No MCoreIMG frames were found in the selected file.", file=sys.stderr)
        return 3

    try:
        image_id, commands, duplicate_count = decode_image_frames(frames)
    except (FrameError, CodecError) as exc:
        print(f"Reconstruction failed: {exc}", file=sys.stderr)
        return 4

    output_path = (
        Path(args.output).expanduser().resolve()
        if args.output
        else default_output_path(input_path, image_id)
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        font_source = render_image(
            commands,
            output_path,
            text_font_size=DEFAULT_TEXT_FONT_SIZE,
        )
    except (CodecError, OSError) as exc:
        print(f"Could not render PNG: {exc}", file=sys.stderr)
        return 5

    print(f"MCoreIMG Reconstructor build: {RECONSTRUCTOR_BUILD}")
    print(f"MCoreIMG image ID: {image_id}")
    print(f"Validated frames: {len(frames) - duplicate_count}")
    if duplicate_count:
        print(f"Identical duplicate retransmissions ignored: {duplicate_count}")
    if ignored_lines:
        print(f"Non-MCoreIMG lines ignored: {ignored_lines}")
    print(f"Decoded commands: {len(commands)}")
    print(f"Rendered text font size: {DEFAULT_TEXT_FONT_SIZE} px (hard-coded)")
    print(f"Rendered text font: {font_source}")
    print(f"Image reconstruction complete: {output_path}")

    if args.list_commands:
        for index, command in enumerate(commands):
            print(f"{index:03d}: {command_summary(command)}")

    if args.dump_json:
        try:
            json_path = write_source_json(output_path, commands, image_id)
        except OSError as exc:
            print(f"PNG was created, but JSON export failed: {exc}", file=sys.stderr)
            return 6
        print(f"Decoded source JSON: {json_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())