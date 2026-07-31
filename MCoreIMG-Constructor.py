#!/usr/bin/env python3
from __future__ import annotations
"""
MCoreIMG Constructor — stateful compressed MeshCore image editor
===============================================================

This replaces EMEIMG's fixed 13-character drawing packets with a structured
vector editor and a single compressed image bitstream.

Transport profile
-----------------
* Canvas: 720 x 480 pixels.
* Up to five MeshCore text messages.
* Each message is at most 150 ASCII characters.
* Each message starts with a 15-character control header and carries up to
  135 Base91 payload characters.
* Each frame has a CRC-16. The assembled bitstream has a CRC-32.
* No forward-error-correction or packet repetition is added; ACK/retransmit is
  expected to provide reliability.

Compression profile
-------------------
* Draw order is implicit; no order field is transmitted.
* 4-bit opcodes, with a one-bit "same opcode" shortcut.
* Stateful color and shape parameters use change flags.
* Coordinates use absolute 10-bit X / 9-bit Y or predictive signed deltas.
* Signed deltas use ZigZag + Golomb-Rice coding.
* Variable non-negative integers use Exp-Golomb coding.
* Repeated translated commands are detected automatically and encoded as a
  compact translated-repeat macro.
* The complete byte stream is encoded with a text-safe custom Base91 alphabet.

Source files
------------
The editor saves lossless, human-readable JSON source files (*.mci.json).
Transport exports (*.mci) contain one MeshCore frame per line. Legacy EMEIMG
13-character packet files can be imported and converted.

Run:
    python MCoreIMG-Constructor.py

Self-test without opening Tk:
    python MCoreIMG-Constructor.py --self-test

Arch Linux Tk dependency:
    sudo pacman -Syu tk

Optional PNG export:
    sudo pacman -S python-pillow
    # or: pip install pillow
"""

import argparse
import binascii
import copy
import json
import math
import os
import random
import subprocess
import sys
import tkinter as tk
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = None
    ImageDraw = None
    ImageFont = None


# ---------------------------------------------------------------------------
# Protocol constants
# ---------------------------------------------------------------------------

CANVAS_W = 720
CANVAS_H = 480
PROTOCOL_NAME = "MCoreIMG"
PROTOCOL_VERSION = 1
SOURCE_FORMAT = "MCoreIMG-source"
SOURCE_VERSION = 1
DEFAULT_GRID = "EN60"  # source metadata only; not transmitted

# Shared text size for both the Tk preview and PNG export.
DEFAULT_TEXT_FONT_SIZE = 20
TEXT_FONT_WEIGHT = "bold"
CONSTRUCTOR_BUILD = "2026.07.31-fontfix3"


def load_text_font(font_size: int = DEFAULT_TEXT_FONT_SIZE):
    """Load one scalable font object for the entire PNG render.

    This is deliberately the same strategy used by the working MCoreIMG
    Reconstructor: resolve a real TrueType font once, return both the font and
    its source, and pass that exact object into every Pillow draw.text call.
    """
    if ImageFont is None:
        raise RuntimeError("Pillow ImageFont is unavailable.")

    font_size = max(1, int(font_size))
    candidates = [
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",  # Arch Linux
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/noto/NotoSans-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/segoeuib.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "DejaVuSans-Bold.ttf",
        "NotoSans-Bold.ttf",
        "LiberationSans-Bold.ttf",
    ]

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
        except (OSError, ValueError):
            continue

    try:
        return ImageFont.load_default(size=font_size), f"Pillow default at {font_size}px"
    except TypeError as exc:
        raise RuntimeError(
            "No scalable font was found. Install one with: sudo pacman -S ttf-dejavu"
        ) from exc

MAX_MESSAGES = 5
MESSAGE_LEN = 150
FRAME_HEADER_LEN = 15
FRAME_PAYLOAD_LEN = MESSAGE_LEN - FRAME_HEADER_LEN
FRAME_MAGIC = "MCI"

# Base62 is reserved for fixed frame-header fields.
BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

# 94 printable ASCII characters exist from ! through ~. Excluding quote,
# apostrophe, and backslash leaves exactly 91 characters and avoids common
# escaping problems in JSON, shells, and chat transport.
BASE91 = "".join(
    chr(code)
    for code in range(33, 127)
    if chr(code) not in {'"', "'", "\\"}
)
assert len(BASE91) == 91
BASE91_INDEX = {ch: i for i, ch in enumerate(BASE91)}

# Text inside a drawing uses six bits per symbol. Keep this intentionally
# conservative and human-oriented.
TEXT_ALPHABET = " 0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ-+./?:,()[]#_=@"
assert len(TEXT_ALPHABET) <= 64
TEXT_INDEX = {ch: i for i, ch in enumerate(TEXT_ALPHABET)}
MAX_TEXT_LEN = 31

DEFAULT_RADIUS = 35
DEFAULT_SCALE = 1
MAX_EDITOR_COMMANDS = 512  # safety limit; real limit is five-frame capacity


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
COLOR_CODE_TO_INDEX = {code: i for i, (code, _name, _hex) in enumerate(COLOR_TABLE)}
COLOR_HEX = [item[2] for item in COLOR_TABLE]
COLOR_NAME = [item[1] for item in COLOR_TABLE]


@dataclass(frozen=True)
class ShapeDef:
    opcode: int
    code: str
    name: str
    needs_second_click: bool
    default_fill: Optional[int] = None
    macro: bool = False


SHAPES: List[ShapeDef] = [
    ShapeDef(0x0, "0", "Text", False),
    ShapeDef(0x1, "1", "Line", True),
    ShapeDef(0x2, "2", "Rectangle", True, default_fill=0),
    ShapeDef(0x3, "3", "Ellipse", False, default_fill=0),
    ShapeDef(0x4, "4", "Triangle Outline", False),
    ShapeDef(0x5, "5", "Triangle Fill", False),
    ShapeDef(0x6, "6", "Arrow", False),
    ShapeDef(0x7, "7", "Star", False),
    ShapeDef(0x8, "8", "SemiCircle / Arc", False),
    ShapeDef(0x9, "9", "Yagi Antenna", False, macro=True),
    ShapeDef(0xA, "A", "Dish Antenna", False, macro=True),
    ShapeDef(0xB, "B", "Radio Transceiver", False, macro=True),
    ShapeDef(0xC, "C", "Radio Waves", False, macro=True),
    ShapeDef(0xD, "D", "Moon", False, macro=True),
    ShapeDef(0xE, "E", "DoubleBox", True, macro=True),
]
SHAPE_BY_OPCODE = {shape.opcode: shape for shape in SHAPES}
SHAPE_BY_CODE = {shape.code: shape for shape in SHAPES}
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
            raise CodecError(f"Unknown opcode {self.opcode}") from exc

    def to_json(self) -> Dict[str, Any]:
        return {
            "opcode": self.opcode,
            "shape": self.shape.code,
            "color": self.color,
            "fields": copy.deepcopy(self.fields),
        }

    @classmethod
    def from_json(cls, value: Dict[str, Any]) -> "DrawCommand":
        if not isinstance(value, dict):
            raise CodecError("Command must be a JSON object.")
        opcode = int(value.get("opcode", -1))
        color = int(value.get("color", -1))
        fields = value.get("fields", {})
        if opcode not in SHAPE_BY_OPCODE:
            raise CodecError(f"Invalid source opcode: {opcode}")
        if not 0 <= color < len(COLOR_TABLE):
            raise CodecError(f"Invalid source color index: {color}")
        if not isinstance(fields, dict):
            raise CodecError("Command fields must be an object.")
        cmd = cls(opcode, color, copy.deepcopy(fields))
        validate_command(cmd)
        return cmd


@dataclass
class CodecStats:
    command_count: int
    bit_count: int
    packed_bytes: int
    total_bytes_with_crc: int
    base91_chars: int
    frame_count: int
    total_transport_chars: int
    repeat_macros: int

    @property
    def fits(self) -> bool:
        return self.frame_count <= MAX_MESSAGES


@dataclass
class EncodedImage:
    raw: bytes
    payload: str
    frames: List[str]
    stats: CodecStats
    image_id: str


# ---------------------------------------------------------------------------
# General helpers
# ---------------------------------------------------------------------------


def clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(n)))


def validate_grid_locator(grid: str) -> str:
    grid = grid.strip().upper()
    if len(grid) != 4:
        raise CodecError("Grid locator must be exactly four characters, for example EN60.")
    if not ("A" <= grid[0] <= "R" and "A" <= grid[1] <= "R"):
        raise CodecError("Grid locator must begin with Maidenhead field letters A-R.")
    if not (grid[2].isdigit() and grid[3].isdigit()):
        raise CodecError("Grid locator must end with two digits.")
    return grid


def encode_base62(value: int, width: int) -> str:
    if value < 0 or value >= len(BASE62) ** width:
        raise CodecError(f"Value {value} does not fit in {width} Base62 characters.")
    chars = ["0"] * width
    for i in range(width - 1, -1, -1):
        value, rem = divmod(value, len(BASE62))
        chars[i] = BASE62[rem]
    return "".join(chars)


def decode_base62(text: str) -> int:
    value = 0
    for ch in text:
        try:
            digit = BASE62.index(ch)
        except ValueError as exc:
            raise FrameError(f"Invalid Base62 character {ch!r}") from exc
        value = value * len(BASE62) + digit
    return value


def clean_text(text: str) -> str:
    text = text.upper()[:MAX_TEXT_LEN]
    return "".join(ch if ch in TEXT_INDEX else " " for ch in text).rstrip()


def command_anchor(command: DrawCommand) -> Tuple[int, int]:
    f = command.fields
    if "x" in f and "y" in f:
        return int(f["x"]), int(f["y"])
    return int(f["x1"]), int(f["y1"])


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


def translated_repeat_delta(previous: Optional[DrawCommand], current: DrawCommand) -> Optional[Tuple[int, int]]:
    if previous is None or previous.opcode != current.opcode or previous.color != current.color:
        return None

    prev_fields = copy.deepcopy(previous.fields)
    cur_fields = copy.deepcopy(current.fields)
    px, py = command_anchor(previous)
    cx, cy = command_anchor(current)
    dx, dy = cx - px, cy - py

    for x_key, y_key in coordinate_keys(previous):
        if x_key not in prev_fields or y_key not in prev_fields:
            return None
        if x_key not in cur_fields or y_key not in cur_fields:
            return None
        if int(cur_fields[x_key]) - int(prev_fields[x_key]) != dx:
            return None
        if int(cur_fields[y_key]) - int(prev_fields[y_key]) != dy:
            return None
        del prev_fields[x_key], prev_fields[y_key]
        del cur_fields[x_key], cur_fields[y_key]

    if prev_fields != cur_fields:
        return None
    if dx == 0 and dy == 0:
        # Exact duplicate still benefits from the repeat macro.
        return 0, 0
    return dx, dy


def command_summary(command: DrawCommand) -> str:
    f = command.fields
    color = COLOR_TABLE[command.color][0]
    shape = command.shape
    if command.opcode == 0x0:
        detail = f"({f['x']},{f['y']}) {f.get('text', '')!r}"
    elif command.opcode in {0x1, 0x2, 0xE}:
        detail = f"({f['x1']},{f['y1']})→({f['x2']},{f['y2']})"
    else:
        detail = f"({f['x']},{f['y']})"
    return f"{shape.code} {shape.name} | C={color} | {detail}"


# ---------------------------------------------------------------------------
# Bit-level codec: Exp-Golomb + ZigZag/Golomb-Rice + fixed fields
# ---------------------------------------------------------------------------


class BitWriter:
    def __init__(self) -> None:
        self.bits: List[int] = []

    def write_bit(self, value: int | bool) -> None:
        self.bits.append(1 if value else 0)

    def write_bits(self, value: int, width: int) -> None:
        if width < 0:
            raise CodecError("Negative bit width.")
        if value < 0 or value >= (1 << width):
            raise CodecError(f"Value {value} does not fit in {width} bits.")
        for shift in range(width - 1, -1, -1):
            self.bits.append((value >> shift) & 1)

    def write_ue(self, value: int) -> None:
        """Unsigned Exp-Golomb code for value >= 0."""
        if value < 0:
            raise CodecError("Exp-Golomb value must be non-negative.")
        code_num = value + 1
        width = code_num.bit_length()
        self.bits.extend([0] * (width - 1))
        self.write_bits(code_num, width)

    def write_rice_unsigned(self, value: int, k: int) -> None:
        if value < 0 or k < 0:
            raise CodecError("Rice value and k must be non-negative.")
        quotient = value >> k
        remainder = value & ((1 << k) - 1) if k else 0
        self.bits.extend([1] * quotient)
        self.bits.append(0)
        if k:
            self.write_bits(remainder, k)

    def write_rice_signed(self, value: int, k: int) -> None:
        zigzag = (-value * 2 - 1) if value < 0 else value * 2
        self.write_rice_unsigned(zigzag, k)

    def to_bytes(self) -> bytes:
        output = bytearray((len(self.bits) + 7) // 8)
        for i, bit in enumerate(self.bits):
            if bit:
                output[i // 8] |= 1 << (7 - (i % 8))
        return bytes(output)

    @property
    def bit_count(self) -> int:
        return len(self.bits)


class BitReader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.bit_pos = 0

    def _remaining(self) -> int:
        return len(self.data) * 8 - self.bit_pos

    def read_bit(self) -> int:
        if self._remaining() < 1:
            raise CodecError("Unexpected end of compressed bitstream.")
        byte = self.data[self.bit_pos // 8]
        bit = (byte >> (7 - (self.bit_pos % 8))) & 1
        self.bit_pos += 1
        return bit

    def read_bits(self, width: int) -> int:
        if width < 0 or self._remaining() < width:
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


def rice_unsigned_bit_length(value: int, k: int) -> int:
    return (value >> k) + 1 + k


def rice_signed_bit_length(value: int, k: int) -> int:
    zigzag = (-value * 2 - 1) if value < 0 else value * 2
    return rice_unsigned_bit_length(zigzag, k)


def write_stateful_fixed(writer: BitWriter, state: StreamState, key: str, value: int, width: int) -> None:
    same = state.params.get(key) == value
    writer.write_bit(same)
    if not same:
        writer.write_bits(value, width)
        state.params[key] = value


def read_stateful_fixed(reader: BitReader, state: StreamState, key: str, width: int) -> int:
    same = bool(reader.read_bit())
    if same:
        if key not in state.params:
            raise CodecError(f"Stateful field {key!r} referenced before initialization.")
        return state.params[key]
    value = reader.read_bits(width)
    state.params[key] = value
    return value


def write_stateful_ue(writer: BitWriter, state: StreamState, key: str, value: int) -> None:
    same = state.params.get(key) == value
    writer.write_bit(same)
    if not same:
        writer.write_ue(value)
        state.params[key] = value


def read_stateful_ue(reader: BitReader, state: StreamState, key: str, max_value: int) -> int:
    same = bool(reader.read_bit())
    if same:
        if key not in state.params:
            raise CodecError(f"Stateful field {key!r} referenced before initialization.")
        return state.params[key]
    value = reader.read_ue(max_value=max_value)
    state.params[key] = value
    return value


def write_point(writer: BitWriter, state: StreamState, x: int, y: int) -> None:
    x = clamp(x, 0, CANVAS_W - 1)
    y = clamp(y, 0, CANVAS_H - 1)
    if state.has_point:
        dx, dy = x - state.x, y - state.y
        delta_bits = 1 + rice_signed_bit_length(dx, 3) + rice_signed_bit_length(dy, 3)
        absolute_bits = 1 + 10 + 9
        use_delta = delta_bits <= absolute_bits
    else:
        use_delta = False

    writer.write_bit(use_delta)
    if use_delta:
        writer.write_rice_signed(x - state.x, 3)
        writer.write_rice_signed(y - state.y, 3)
    else:
        writer.write_bits(x, 10)
        writer.write_bits(y, 9)

    state.x, state.y, state.has_point = x, y, True


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
    state.x, state.y, state.has_point = x, y, True
    return x, y


def write_relative_point(writer: BitWriter, x1: int, y1: int, x2: int, y2: int) -> None:
    dx, dy = x2 - x1, y2 - y1
    delta_bits = 1 + rice_signed_bit_length(dx, 3) + rice_signed_bit_length(dy, 3)
    absolute_bits = 1 + 10 + 9
    use_delta = delta_bits <= absolute_bits
    writer.write_bit(use_delta)
    if use_delta:
        writer.write_rice_signed(dx, 3)
        writer.write_rice_signed(dy, 3)
    else:
        writer.write_bits(clamp(x2, 0, CANVAS_W - 1), 10)
        writer.write_bits(clamp(y2, 0, CANVAS_H - 1), 9)


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


def write_translation(writer: BitWriter, dx: int, dy: int) -> None:
    delta_bits = 1 + rice_signed_bit_length(dx, 2) + rice_signed_bit_length(dy, 2)
    fixed_bits = 1 + 11 + 10
    use_rice = delta_bits <= fixed_bits
    writer.write_bit(use_rice)
    if use_rice:
        writer.write_rice_signed(dx, 2)
        writer.write_rice_signed(dy, 2)
    else:
        writer.write_bits(dx + 719, 11)
        writer.write_bits(dy + 479, 10)


def read_translation(reader: BitReader) -> Tuple[int, int]:
    use_rice = bool(reader.read_bit())
    if use_rice:
        return (
            reader.read_rice_signed(2, max_abs=719),
            reader.read_rice_signed(2, max_abs=479),
        )
    return reader.read_bits(11) - 719, reader.read_bits(10) - 479


def write_normal_opcode(writer: BitWriter, state: StreamState, opcode: int) -> None:
    same = state.has_opcode and state.opcode == opcode
    writer.write_bit(same)
    if not same:
        writer.write_bits(opcode, 4)
        state.opcode = opcode
        state.has_opcode = True


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


def write_color(writer: BitWriter, state: StreamState, color: int) -> None:
    same = state.has_color and state.color == color
    writer.write_bit(same)
    if not same:
        writer.write_bits(color, 6)
        state.color = color
        state.has_color = True


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


def encode_command_fields(writer: BitWriter, state: StreamState, command: DrawCommand) -> None:
    f = command.fields
    op = command.opcode

    if op in {0x1, 0x2, 0xE}:
        x1, y1 = int(f["x1"]), int(f["y1"])
        x2, y2 = int(f["x2"]), int(f["y2"])
        write_point(writer, state, x1, y1)
        write_relative_point(writer, x1, y1, x2, y2)
    else:
        write_point(writer, state, int(f["x"]), int(f["y"]))

    if op == 0x0:
        text = clean_text(str(f.get("text", "")))
        writer.write_ue(len(text))
        for ch in text:
            writer.write_bits(TEXT_INDEX[ch], 6)
    elif op == 0x1:
        pass
    elif op == 0x2:
        write_stateful_fixed(writer, state, "fill", int(f["fill"]), 1)
    elif op == 0x3:
        write_stateful_ue(writer, state, "radius_h", int(f["radius_h"]) - 1)
        write_stateful_ue(writer, state, "radius_w", int(f["radius_w"]) - 1)
        write_stateful_ue(writer, state, "scale", int(f["scale"]) - 1)
        write_stateful_fixed(writer, state, "fill", int(f["fill"]), 1)
    elif op in {0x4, 0x5, 0x6, 0x9, 0xA, 0xB}:
        write_stateful_fixed(writer, state, "orientation", int(f.get("orientation", 0)) % 4, 2)
        write_stateful_ue(writer, state, "scale", int(f["scale"]) - 1)
    elif op == 0x7:
        write_stateful_ue(writer, state, "radius", int(f["radius"]) - 1)
        write_stateful_ue(writer, state, "scale", int(f["scale"]) - 1)
    elif op in {0x8, 0xC}:
        write_stateful_ue(writer, state, "radius", int(f["radius"]) - 1)
        write_stateful_ue(writer, state, "scale", int(f["scale"]) - 1)
        write_stateful_fixed(writer, state, "start_angle", int(f["start_angle"]) % 361, 9)
        write_stateful_fixed(writer, state, "arc_degrees", int(f["arc_degrees"]) % 361, 9)
    elif op == 0xD:
        write_stateful_ue(writer, state, "scale", int(f["scale"]) - 1)
        write_stateful_fixed(writer, state, "crater_color", int(f["crater_color"]), 6)
    elif op == 0xE:
        write_stateful_ue(writer, state, "percent", int(f["percent"]))
    else:
        raise CodecError(f"Unsupported opcode {op}")


def decode_command_fields(reader: BitReader, state: StreamState, opcode: int, color: int) -> DrawCommand:
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
        chars = []
        for _ in range(length):
            idx = reader.read_bits(6)
            if idx >= len(TEXT_ALPHABET):
                raise CodecError(f"Invalid text symbol index {idx}.")
            chars.append(TEXT_ALPHABET[idx])
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
    else:
        raise CodecError(f"Unsupported opcode {opcode}")

    command = DrawCommand(opcode, color, fields)
    validate_command(command)
    return command


def encode_commands_to_bits(commands: Sequence[DrawCommand]) -> Tuple[bytes, int, int]:
    if len(commands) > MAX_EDITOR_COMMANDS:
        raise CodecError(f"Too many commands; editor safety limit is {MAX_EDITOR_COMMANDS}.")

    writer = BitWriter()
    writer.write_bits(PROTOCOL_VERSION, 4)
    writer.write_ue(len(commands))
    state = StreamState()
    repeat_count = 0

    for command in commands:
        validate_command(command)
        repeat_delta = translated_repeat_delta(state.previous_command, command)
        is_repeat = repeat_delta is not None
        writer.write_bit(is_repeat)

        if is_repeat:
            assert repeat_delta is not None and state.previous_command is not None
            write_translation(writer, repeat_delta[0], repeat_delta[1])
            state.x, state.y = command_anchor(command)
            state.has_point = True
            state.previous_command = command.clone()
            repeat_count += 1
            continue

        write_normal_opcode(writer, state, command.opcode)
        write_color(writer, state, command.color)
        encode_command_fields(writer, state, command)
        state.previous_command = command.clone()

    return writer.to_bytes(), writer.bit_count, repeat_count


def decode_commands_from_bits(data: bytes) -> List[DrawCommand]:
    reader = BitReader(data)
    version = reader.read_bits(4)
    if version != PROTOCOL_VERSION:
        raise CodecError(f"Unsupported MCoreIMG bitstream version {version}.")
    count = reader.read_ue(max_value=MAX_EDITOR_COMMANDS)
    state = StreamState()
    commands: List[DrawCommand] = []

    for _ in range(count):
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

    return commands


# ---------------------------------------------------------------------------
# Base91 and MeshCore frame layer
# ---------------------------------------------------------------------------


def base91_encode(data: bytes) -> str:
    output: List[str] = []
    accumulator = 0
    bit_count = 0
    for byte in data:
        accumulator |= byte << bit_count
        bit_count += 8
        if bit_count > 13:
            value = accumulator & 8191
            if value > 88:
                accumulator >>= 13
                bit_count -= 13
            else:
                value = accumulator & 16383
                accumulator >>= 14
                bit_count -= 14
            output.append(BASE91[value % 91])
            output.append(BASE91[value // 91])
    if bit_count:
        output.append(BASE91[accumulator % 91])
        if bit_count > 7 or accumulator > 90:
            output.append(BASE91[accumulator // 91])
    return "".join(output)


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


def deterministic_image_id(raw_with_crc: bytes) -> str:
    value = zlib.crc32(raw_with_crc) % (len(BASE62) ** 3)
    return encode_base62(value, 3)


def build_frame_header(
    image_id: str,
    part_index: int,
    total_parts: int,
    payload: str,
    flags: int = 0,
) -> str:
    if len(image_id) != 3 or any(ch not in BASE62 for ch in image_id):
        raise FrameError("Image ID must be three Base62 characters.")
    if not 0 <= part_index < MAX_MESSAGES:
        raise FrameError("Part index is outside the five-message profile.")
    if not 1 <= total_parts <= MAX_MESSAGES:
        raise FrameError("Total parts must be 1 through 5.")
    if len(payload) > FRAME_PAYLOAD_LEN:
        raise FrameError(f"Frame payload exceeds {FRAME_PAYLOAD_LEN} characters.")
    if not 0 <= flags < len(BASE62):
        raise FrameError("Frame flags do not fit one Base62 character.")

    header = (
        FRAME_MAGIC
        + encode_base62(PROTOCOL_VERSION, 1)
        + image_id
        + encode_base62(part_index, 1)
        + encode_base62(total_parts, 1)
        + encode_base62(len(payload), 2)
        + encode_base62(frame_crc(payload), 3)
        + encode_base62(flags, 1)
    )
    if len(header) != FRAME_HEADER_LEN:
        raise AssertionError(f"Frame header is {len(header)} characters, expected {FRAME_HEADER_LEN}.")
    return header


def parse_frame(frame: str) -> Dict[str, Any]:
    frame = frame.rstrip("\r\n")
    if len(frame) < FRAME_HEADER_LEN:
        raise FrameError("Frame is shorter than the 15-character control header.")
    if len(frame) > MESSAGE_LEN:
        raise FrameError(f"Frame exceeds {MESSAGE_LEN} characters.")
    header, payload = frame[:FRAME_HEADER_LEN], frame[FRAME_HEADER_LEN:]
    if header[:3] != FRAME_MAGIC:
        raise FrameError("Frame does not begin with MCI.")
    version = decode_base62(header[3])
    if version != PROTOCOL_VERSION:
        raise FrameError(f"Unsupported frame version {version}.")
    image_id = header[4:7]
    part_index = decode_base62(header[7])
    total_parts = decode_base62(header[8])
    payload_len = decode_base62(header[9:11])
    crc = decode_base62(header[11:14])
    flags = decode_base62(header[14])
    if payload_len != len(payload):
        raise FrameError(f"Frame declares {payload_len} payload characters but contains {len(payload)}.")
    if crc != frame_crc(payload):
        raise FrameError("Frame CRC-16 mismatch; request retransmission of this part.")
    if not 0 <= part_index < total_parts <= MAX_MESSAGES:
        raise FrameError("Invalid frame part/total values.")
    return {
        "image_id": image_id,
        "part_index": part_index,
        "total_parts": total_parts,
        "payload": payload,
        "flags": flags,
    }


def make_frames(payload: str, raw_with_crc: bytes) -> Tuple[str, List[str]]:
    chunks = [payload[i : i + FRAME_PAYLOAD_LEN] for i in range(0, len(payload), FRAME_PAYLOAD_LEN)]
    if not chunks:
        chunks = [""]
    if len(chunks) > MAX_MESSAGES:
        raise FrameError(
            f"Compressed image requires {len(chunks)} MeshCore messages; maximum is {MAX_MESSAGES}."
        )
    image_id = deterministic_image_id(raw_with_crc)
    frames = []
    for part_index, chunk in enumerate(chunks):
        header = build_frame_header(image_id, part_index, len(chunks), chunk, flags=0)
        frame = header + chunk
        if len(frame) > MESSAGE_LEN:
            raise AssertionError("Generated frame exceeds transport limit.")
        frames.append(frame)
    return image_id, frames


def assemble_frames(frames: Iterable[str]) -> Tuple[str, bytes]:
    parsed = [parse_frame(frame) for frame in frames if frame.strip()]
    if not parsed:
        raise FrameError("No MCoreIMG frames found.")
    image_ids = {item["image_id"] for item in parsed}
    totals = {item["total_parts"] for item in parsed}
    if len(image_ids) != 1 or len(totals) != 1:
        raise FrameError("Frames belong to different images or disagree on total parts.")
    image_id = next(iter(image_ids))
    total = next(iter(totals))
    parts: Dict[int, str] = {}
    for item in parsed:
        idx = item["part_index"]
        if idx in parts and parts[idx] != item["payload"]:
            raise FrameError(f"Conflicting duplicates for frame part {idx}.")
        parts[idx] = item["payload"]
    missing = [i for i in range(total) if i not in parts]
    if missing:
        missing_text = ", ".join(str(i) for i in missing)
        raise FrameError(f"Missing frame part(s): {missing_text}.")
    payload = "".join(parts[i] for i in range(total))
    raw_with_crc = base91_decode(payload)
    if len(raw_with_crc) < 4:
        raise CodecError("Assembled stream is too short for CRC-32.")
    raw, expected_crc_bytes = raw_with_crc[:-4], raw_with_crc[-4:]
    expected_crc = int.from_bytes(expected_crc_bytes, "big")
    actual_crc = zlib.crc32(raw) & 0xFFFFFFFF
    if expected_crc != actual_crc:
        raise CodecError("Assembled stream CRC-32 mismatch.")
    return image_id, raw


def encode_image(commands: Sequence[DrawCommand]) -> EncodedImage:
    packed, bit_count, repeat_count = encode_commands_to_bits(commands)
    stream_crc = zlib.crc32(packed) & 0xFFFFFFFF
    raw_with_crc = packed + stream_crc.to_bytes(4, "big")
    payload = base91_encode(raw_with_crc)
    predicted_frames = max(1, math.ceil(len(payload) / FRAME_PAYLOAD_LEN))
    image_id = deterministic_image_id(raw_with_crc)
    frames: List[str] = []
    if predicted_frames <= MAX_MESSAGES:
        image_id, frames = make_frames(payload, raw_with_crc)
    stats = CodecStats(
        command_count=len(commands),
        bit_count=bit_count,
        packed_bytes=len(packed),
        total_bytes_with_crc=len(raw_with_crc),
        base91_chars=len(payload),
        frame_count=predicted_frames,
        total_transport_chars=len(payload) + predicted_frames * FRAME_HEADER_LEN,
        repeat_macros=repeat_count,
    )
    return EncodedImage(raw_with_crc, payload, frames, stats, image_id)


def decode_image_frames(frames: Iterable[str]) -> List[DrawCommand]:
    _image_id, packed = assemble_frames(frames)
    return decode_commands_from_bits(packed)


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
    f = command.fields
    op = command.opcode

    if op in {0x1, 0x2, 0xE}:
        _require_int(f, "x1", 0, CANVAS_W - 1)
        _require_int(f, "y1", 0, CANVAS_H - 1)
        _require_int(f, "x2", 0, CANVAS_W - 1)
        _require_int(f, "y2", 0, CANVAS_H - 1)
    else:
        _require_int(f, "x", 0, CANVAS_W - 1)
        _require_int(f, "y", 0, CANVAS_H - 1)

    if op == 0x0:
        f["text"] = clean_text(str(f.get("text", "")))
    elif op == 0x2:
        _require_int(f, "fill", 0, 1)
    elif op == 0x3:
        _require_int(f, "radius_h", 1, 128)
        _require_int(f, "radius_w", 1, 128)
        _require_int(f, "scale", 1, 64)
        _require_int(f, "fill", 0, 1)
    elif op in {0x4, 0x5, 0x6, 0x9, 0xA, 0xB}:
        _require_int(f, "orientation", 0, 3)
        _require_int(f, "scale", 1, 64)
    elif op == 0x7:
        _require_int(f, "radius", 1, 128)
        _require_int(f, "scale", 1, 64)
    elif op in {0x8, 0xC}:
        _require_int(f, "radius", 1, 128)
        _require_int(f, "scale", 1, 64)
        _require_int(f, "start_angle", 0, 360)
        _require_int(f, "arc_degrees", 0, 360)
    elif op == 0xD:
        _require_int(f, "scale", 1, 64)
        _require_int(f, "crater_color", 0, len(COLOR_TABLE) - 1)
    elif op == 0xE:
        _require_int(f, "percent", 0, 100)


# ---------------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------------


def rotate_point(px: float, py: float, ox: float, oy: float, turns: int) -> Tuple[float, float]:
    turns %= 4
    dx, dy = px - ox, py - oy
    for _ in range(turns):
        dx, dy = dy, -dx
    return ox + dx, oy + dy


def polygon_regular(cx: int, cy: int, radius: int, sides: int, rotation_deg: float = -90.0) -> List[Tuple[int, int]]:
    points = []
    for i in range(sides):
        angle = math.radians(rotation_deg + 360 * i / sides)
        points.append((round(cx + math.cos(angle) * radius), round(cy + math.sin(angle) * radius)))
    return points


def draw_line(draw, p1, p2, color: str, width: int = 2, canvas_kind: str = "tk") -> None:
    if canvas_kind == "tk":
        draw.create_line(*p1, *p2, fill=color, width=width)
    else:
        draw.line([p1, p2], fill=color, width=width)


def draw_arrow(draw, x: int, y: int, orientation: int, scale: int, fill: str, canvas_kind: str = "tk") -> None:
    s = max(1, scale)
    points = [
        (x, y),
        (x - 8 * s, y + 18 * s),
        (x - 3 * s, y + 18 * s),
        (x - 3 * s, y + 45 * s),
        (x + 3 * s, y + 45 * s),
        (x + 3 * s, y + 18 * s),
        (x + 8 * s, y + 18 * s),
    ]
    points = [rotate_point(px, py, x, y, orientation) for px, py in points]
    if canvas_kind == "tk":
        draw.create_polygon(*points, fill=fill, outline=fill)
    else:
        draw.polygon(points, fill=fill, outline=fill)


def draw_star(draw, x: int, y: int, radius: int, scale: int, color: str, canvas_kind: str = "tk") -> None:
    if scale <= 0 or radius <= 0:
        return
    r = radius * scale
    diag = round(r / math.sqrt(2))
    lines = [
        ((x, y - r), (x, y + r)),
        ((x - r, y), (x + r, y)),
        ((x - diag, y - diag), (x + diag, y + diag)),
        ((x + diag, y - diag), (x - diag, y + diag)),
    ]
    for p1, p2 in lines:
        draw_line(draw, p1, p2, color, width=3, canvas_kind=canvas_kind)


def draw_yagi(draw, x: int, y: int, orientation: int, scale: int, color: str, canvas_kind: str = "tk") -> None:
    s = max(1, scale)
    axis_start = rotate_point(x + 30 * s, y, x, y, orientation)
    axis_end = rotate_point(x, y + 100 * s, x, y, orientation)
    width = max(1, 2 * s)
    draw_line(draw, axis_start, axis_end, color, width, canvas_kind)
    for t in [0.15, 0.35, 0.55, 0.75]:
        ax = (1 - t) * (x + 30 * s) + t * x
        ay = (1 - t) * y + t * (y + 100 * s)
        p1 = rotate_point(ax - 8 * s, ay - 3 * s, x, y, orientation)
        p2 = rotate_point(ax + 8 * s, ay + 3 * s, x, y, orientation)
        draw_line(draw, p1, p2, color, width, canvas_kind)


def draw_dish(draw, x: int, y: int, orientation: int, scale: int, color: str, canvas_kind: str = "tk") -> None:
    if scale <= 0:
        return
    facing = orientation % 2
    s = max(1, scale)
    radius = 40 * s
    hub_radius = 5 * s
    mast_length = 30 * s
    width = 3
    flip = -1 if facing == 0 else 1
    arc_points = []
    for angle in range(90, 181, 5):
        theta = math.radians(angle)
        dx = round(radius * math.cos(theta)) * flip
        dy = round(radius * math.sin(theta))
        arc_points.append((x + dx, y + dy))
    for p1, p2 in zip(arc_points, arc_points[1:]):
        draw_line(draw, p1, p2, color, width=width, canvas_kind=canvas_kind)
    draw_line(draw, (x, y), (x - radius * flip, y), color, width=width, canvas_kind=canvas_kind)
    draw_line(draw, (x, y), (x, y + radius), color, width=width, canvas_kind=canvas_kind)
    mid_x = round((-radius / math.sqrt(2)) * flip)
    mid_y = round(radius / math.sqrt(2))
    draw_line(draw, (x + mid_x, y + mid_y), (x + mid_x, y + mid_y + mast_length), color, width, canvas_kind)
    if canvas_kind == "tk":
        draw.create_oval(x - hub_radius, y - hub_radius, x + hub_radius, y + hub_radius, fill=color, outline=color)
    else:
        draw.ellipse((x - hub_radius, y - hub_radius, x + hub_radius, y + hub_radius), fill=color, outline=color)


def draw_radio(draw, x: int, y: int, orientation: int, scale: int, color: str, canvas_kind: str = "tk") -> None:
    if scale <= 0:
        return
    s = max(1, scale)
    body_w, body_h = 50 * s, 20 * s
    knob_radius = 5 * s
    knob_cx, knob_cy = x + 10 * s, y + 10 * s
    screen = (x + 25 * s, y + 5 * s, x + 45 * s, y + 15 * s)
    width = 3
    if canvas_kind == "tk":
        draw.create_rectangle(x, y, x + body_w, y + body_h, outline=color, width=width)
        draw.create_oval(knob_cx - knob_radius, knob_cy - knob_radius, knob_cx + knob_radius, knob_cy + knob_radius, outline=color, width=width)
        draw.create_rectangle(*screen, outline=color, width=width)
    else:
        draw.rectangle((x, y, x + body_w, y + body_h), outline=color, width=width)
        draw.ellipse((knob_cx - knob_radius, knob_cy - knob_radius, knob_cx + knob_radius, knob_cy + knob_radius), outline=color, width=width)
        draw.rectangle(screen, outline=color, width=width)


def draw_arc_line(draw, x: int, y: int, radius: int, start_angle: int, arc_degrees: int, color: str, canvas_kind: str = "tk") -> None:
    if radius <= 0 or arc_degrees <= 0:
        return
    start_angle %= 360
    arc_degrees = max(0, min(360, arc_degrees))
    points = []
    for angle in range(start_angle, start_angle + arc_degrees + 1, 5):
        theta = math.radians(angle % 360)
        points.append((round(x + radius * math.cos(theta)), round(y - radius * math.sin(theta))))
    if len(points) < 2:
        return
    if canvas_kind == "tk":
        for p1, p2 in zip(points, points[1:]):
            draw.create_line(*p1, *p2, fill=color, width=3)
    else:
        draw.line(points, fill=color, width=3)


def draw_radio_waves(draw, x: int, y: int, radius: int, scale: int, start_angle: int, arc_degrees: int, color: str, canvas_kind: str = "tk") -> None:
    if scale <= 0 or radius <= 0 or arc_degrees <= 0:
        return
    base_radius = radius * scale
    spacing = 2 * radius * scale
    for offset in (0, spacing, 2 * spacing):
        draw_arc_line(draw, x, y, base_radius + offset, start_angle, arc_degrees, color, canvas_kind)


def draw_moon(draw, x: int, y: int, scale: int, moon_color: str, crater_color: str, canvas_kind: str = "tk") -> None:
    if scale <= 0:
        return
    s = max(1, scale)
    moon_radius = 36 * s
    left, top = x - moon_radius, y - moon_radius
    right, bottom = x + moon_radius, y + moon_radius
    if canvas_kind == "tk":
        draw.create_oval(left, top, right, bottom, fill=moon_color, outline=moon_color)
    else:
        draw.ellipse((left, top, right, bottom), fill=moon_color, outline=moon_color)
    diameter = moon_radius * 2
    crater_points = [
        (1 / 5, 1 / 4, 3), (3 / 7, 5 / 8, 5), (1 / 4, 7 / 9, 4),
        (4 / 5, 2 / 7, 2), (7 / 12, 1 / 5, 6), (2 / 3, 2 / 5, 3),
        (5 / 8, 3 / 4, 4), (7 / 20, 3 / 7, 2), (3 / 20, 5 / 9, 5),
        (3 / 4, 3 / 5, 3),
    ]
    for fx, fy, base_radius in crater_points:
        cx = left + round(diameter * fx)
        cy = top + round(diameter * fy)
        r = base_radius * s
        if canvas_kind == "tk":
            draw.create_oval(cx - r, cy - r, cx + r, cy + r, outline=crater_color, width=3)
        else:
            draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=crater_color, width=3)


def draw_double_box(draw, x1: int, y1: int, x2: int, y2: int, percent: int, color: str, canvas_kind: str = "tk") -> None:
    left, right = min(x1, x2), max(x1, x2)
    top, bottom = min(y1, y2), max(y1, y2)
    divider_y = top + round((bottom - top) * clamp(percent, 0, 100) / 100)
    if canvas_kind == "tk":
        draw.create_rectangle(left, top, right, bottom, outline=color, width=2)
        draw.create_line(left, divider_y, right, divider_y, fill=color, width=2)
    else:
        draw.rectangle([left, top, right, bottom], outline=color, width=2)
        draw.line([(left, divider_y), (right, divider_y)], fill=color, width=2)


def render_command(
    command: DrawCommand,
    target,
    canvas_kind: str = "tk",
    text_font=None,
) -> None:
    validate_command(command)
    f = command.fields
    op = command.opcode
    color = COLOR_HEX[command.color]

    if op == 0x0:
        if canvas_kind == "tk":
            target.create_text(
                f["x"],
                f["y"],
                text=f["text"],
                fill=color,
                anchor="nw",
                font=("TkDefaultFont", DEFAULT_TEXT_FONT_SIZE, TEXT_FONT_WEIGHT),
            )
        else:
            if text_font is None:
                text_font, _font_source = load_text_font(DEFAULT_TEXT_FONT_SIZE)
            target.text(
                (f["x"], f["y"]),
                f["text"],
                fill=color,
                font=text_font,
                anchor="lt",
            )
    elif op == 0x1:
        draw_line(target, (f["x1"], f["y1"]), (f["x2"], f["y2"]), color, 2, canvas_kind)
    elif op == 0x2:
        left, right = min(f["x1"], f["x2"]), max(f["x1"], f["x2"])
        top, bottom = min(f["y1"], f["y2"]), max(f["y1"], f["y2"])
        if canvas_kind == "tk":
            target.create_rectangle(left, top, right, bottom, outline=color, fill=color if f["fill"] else "", width=2)
        else:
            target.rectangle([left, top, right, bottom], outline=color, fill=color if f["fill"] else None, width=2)
    elif op == 0x3:
        rx, ry = f["radius_w"] * f["scale"], f["radius_h"] * f["scale"]
        box = [f["x"] - rx, f["y"] - ry, f["x"] + rx, f["y"] + ry]
        if canvas_kind == "tk":
            target.create_oval(*box, outline=color, fill=color if f["fill"] else "", width=2)
        else:
            target.ellipse(box, outline=color, fill=color if f["fill"] else None, width=2)
    elif op in {0x4, 0x5}:
        points = polygon_regular(f["x"], f["y"], 18 * f["scale"], 3, -90 + f["orientation"] * 90)
        if canvas_kind == "tk":
            target.create_polygon(*points, outline=color, fill=color if op == 0x5 else "", width=2)
        else:
            if op == 0x5:
                target.polygon(points, outline=color, fill=color)
            else:
                target.line(points + [points[0]], fill=color, width=2)
    elif op == 0x6:
        draw_arrow(target, f["x"], f["y"], f["orientation"], f["scale"], color, canvas_kind)
    elif op == 0x7:
        draw_star(target, f["x"], f["y"], f["radius"], f["scale"], color, canvas_kind)
    elif op == 0x8:
        draw_arc_line(target, f["x"], f["y"], f["radius"] * f["scale"], f["start_angle"], f["arc_degrees"], color, canvas_kind)
    elif op == 0x9:
        draw_yagi(target, f["x"], f["y"], f["orientation"], f["scale"], color, canvas_kind)
    elif op == 0xA:
        draw_dish(target, f["x"], f["y"], f["orientation"], f["scale"], color, canvas_kind)
    elif op == 0xB:
        draw_radio(target, f["x"], f["y"], f["orientation"], f["scale"], color, canvas_kind)
    elif op == 0xC:
        draw_radio_waves(target, f["x"], f["y"], f["radius"], f["scale"], f["start_angle"], f["arc_degrees"], color, canvas_kind)
    elif op == 0xD:
        draw_moon(target, f["x"], f["y"], f["scale"], color, COLOR_HEX[f["crater_color"]], canvas_kind)
    elif op == 0xE:
        draw_double_box(target, f["x1"], f["y1"], f["x2"], f["y2"], f["percent"], color, canvas_kind)
    else:
        raise CodecError(f"Unsupported opcode {op}.")


# ---------------------------------------------------------------------------
# Source and legacy file support
# ---------------------------------------------------------------------------


LEGACY_BASE36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
LEGACY_PRIORITY_TAG = "[PRIORITY]"


def legacy_b36_1(ch: str) -> int:
    ch = ch.upper()
    if ch not in LEGACY_BASE36:
        raise CodecError(f"Invalid legacy Base36 character {ch!r}.")
    return LEGACY_BASE36.index(ch)


def legacy_b36_2(text: str) -> int:
    if len(text) != 2:
        raise CodecError("Legacy Base36 pair must be two characters.")
    return legacy_b36_1(text[0]) * 36 + legacy_b36_1(text[1])


def legacy_xy(text: str) -> Tuple[int, int]:
    if len(text) != 4:
        raise CodecError("Legacy coordinate field must be four characters.")
    return legacy_b36_2(text[:2]), legacy_b36_2(text[2:])


def legacy_packet_to_command(line: str) -> DrawCommand:
    line = line.rstrip("\r\n")
    if line.upper().endswith(LEGACY_PRIORITY_TAG):
        line = line[: -len(LEGACY_PRIORITY_TAG)].rstrip()
    packet = line.ljust(13)[:13]
    if len(packet) != 13:
        raise CodecError("Legacy packet must be 13 characters.")
    color_code = packet[1].upper()
    shape_code = packet[2].upper()
    if color_code not in COLOR_CODE_TO_INDEX:
        raise CodecError(f"Unknown legacy color code {color_code!r}.")
    if shape_code not in SHAPE_BY_CODE:
        raise CodecError(f"Unknown legacy shape code {shape_code!r}.")
    op = SHAPE_BY_CODE[shape_code].opcode
    color = COLOR_CODE_TO_INDEX[color_code]
    fields: Dict[str, Any] = {}

    if op in {0x1, 0x2, 0xE}:
        x1, y1 = legacy_xy(packet[3:7])
        x2, y2 = legacy_xy(packet[7:11])
        fields.update(x1=x1, y1=y1, x2=x2, y2=y2)
    else:
        x, y = legacy_xy(packet[3:7])
        fields.update(x=x, y=y)

    if op == 0x0:
        fields["text"] = packet[7:13].rstrip()
    elif op == 0x2:
        fields["fill"] = 1 if packet[11] == "1" else 0
    elif op == 0x3:
        fields.update(
            radius_h=max(1, legacy_b36_1(packet[7])),
            radius_w=max(1, legacy_b36_1(packet[8])),
            scale=max(1, legacy_b36_1(packet[9])),
            fill=1 if packet[10] == "1" else 0,
        )
    elif op in {0x4, 0x5, 0x6, 0x9, 0xA, 0xB}:
        fields.update(orientation=legacy_b36_1(packet[7]) % 4, scale=max(1, legacy_b36_1(packet[8])))
    elif op == 0x7:
        fields.update(radius=max(1, legacy_b36_1(packet[7])), scale=max(1, legacy_b36_1(packet[8])))
    elif op in {0x8, 0xC}:
        fields.update(
            radius=max(1, legacy_b36_1(packet[7])),
            scale=max(1, legacy_b36_1(packet[8])),
            start_angle=clamp(legacy_b36_2(packet[9:11]), 0, 360),
            arc_degrees=clamp(legacy_b36_2(packet[11:13]), 0, 360),
        )
    elif op == 0xD:
        crater_code = packet[8].strip().upper() or "1"
        fields.update(
            scale=max(1, legacy_b36_1(packet[7])),
            crater_color=COLOR_CODE_TO_INDEX.get(crater_code, COLOR_CODE_TO_INDEX["1"]),
        )
    elif op == 0xE:
        fields["percent"] = clamp(legacy_b36_2(packet[11:13]), 0, 100)

    command = DrawCommand(op, color, fields)
    validate_command(command)
    return command


def save_source(path: str | os.PathLike[str], commands: Sequence[DrawCommand], grid: str) -> None:
    document = {
        "format": SOURCE_FORMAT,
        "version": SOURCE_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "canvas": {"width": CANVAS_W, "height": CANVAS_H},
        "metadata": {"grid": validate_grid_locator(grid)},
        "commands": [command.to_json() for command in commands],
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2, ensure_ascii=True)
        handle.write("\n")


def load_source(path: str | os.PathLike[str]) -> Tuple[List[DrawCommand], str, str]:
    text = Path(path).read_text(encoding="utf-8")
    stripped = text.lstrip()

    if stripped.startswith("{"):
        document = json.loads(text)
        if document.get("format") != SOURCE_FORMAT:
            raise CodecError("JSON file is not an MCoreIMG source document.")
        version = int(document.get("version", -1))
        if version != SOURCE_VERSION:
            raise CodecError(f"Unsupported source version {version}.")
        commands = [DrawCommand.from_json(item) for item in document.get("commands", [])]
        grid = validate_grid_locator(document.get("metadata", {}).get("grid", DEFAULT_GRID))
        return commands, grid, "MCoreIMG JSON source"

    lines = [line.rstrip("\r\n") for line in text.splitlines() if line.strip()]
    if lines and all(line.startswith(FRAME_MAGIC) for line in lines):
        commands = decode_image_frames(lines)
        return commands, DEFAULT_GRID, "MCoreIMG transport frames"

    commands: List[DrawCommand] = []
    grid = DEFAULT_GRID
    for line in lines:
        upper = line.upper()
        if upper.startswith("EMEIMG"):
            # Old EMEIMGVVPGGGG header; pull the grid when present.
            clean = line.split("[", 1)[0].strip()
            if len(clean) >= 13:
                candidate = clean[-4:]
                try:
                    grid = validate_grid_locator(candidate)
                except CodecError:
                    pass
            continue
        if line.lstrip().startswith("#"):
            continue
        commands.append(legacy_packet_to_command(line))
    if not commands:
        raise CodecError("File contains no supported MCoreIMG or legacy EMEIMG commands.")
    return commands, grid, "legacy EMEIMG packet file"


# ---------------------------------------------------------------------------
# Tk editor
# ---------------------------------------------------------------------------


class MCoreIMGEditor(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"MCoreIMG Constructor — {CONSTRUCTOR_BUILD} — PNG text {DEFAULT_TEXT_FONT_SIZE}px")
        self.geometry("1510x920")
        self.minsize(1280, 780)

        self.commands: List[DrawCommand] = []
        self.selected_shape = SHAPES[0]
        self.selected_color = 0
        self.selected_layer_index: Optional[int] = None
        self.pending_first_click: Optional[Tuple[int, int]] = None
        self.pending_command: Optional[DrawCommand] = None

        self.var_grid = tk.StringVar(value=DEFAULT_GRID)
        self.var_text = tk.StringVar(value="KE9ETA")
        self.var_x1 = tk.IntVar(value=100)
        self.var_y1 = tk.IntVar(value=100)
        self.var_x2 = tk.IntVar(value=200)
        self.var_y2 = tk.IntVar(value=200)
        self.var_orientation = tk.IntVar(value=0)
        self.var_scale = tk.IntVar(value=DEFAULT_SCALE)
        self.var_radius_h = tk.IntVar(value=DEFAULT_RADIUS)
        self.var_radius_w = tk.IntVar(value=DEFAULT_RADIUS)
        self.var_start_angle = tk.IntVar(value=0)
        self.var_arc_degrees = tk.IntVar(value=180)
        self.var_fill = tk.IntVar(value=0)
        self.var_percent = tk.IntVar(value=50)
        self.var_crater_color = tk.IntVar(value=2)
        self.var_status = tk.StringVar(value="Select a shape and color, then click the canvas.")
        self.var_codec_stats = tk.StringVar(value="")
        self.var_selection = tk.StringVar(value="")

        self._build_ui()
        self._refresh_controls_from_shape()
        self._render_all()
        self._refresh_all_lists_and_stats()

    def _build_ui(self) -> None:
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        left = ttk.Frame(self, padding=6)
        left.grid(row=0, column=0, sticky="ns")

        ttk.Label(left, text="Shapes").grid(row=0, column=0, sticky="w")
        shape_frame = ttk.Frame(left)
        shape_frame.grid(row=1, column=0, sticky="ew")
        for i, shape in enumerate(SHAPES):
            ttk.Button(
                shape_frame,
                text=f"{shape.code}  {shape.name}",
                command=lambda selected=shape: self._select_shape(selected),
            ).grid(row=i // 2, column=i % 2, sticky="ew", padx=1, pady=1)

        ttk.Label(left, text="Colors").grid(row=2, column=0, sticky="w", pady=(10, 0))
        color_frame = ttk.Frame(left)
        color_frame.grid(row=3, column=0, sticky="ew")
        for i, (code, _name, hex_color) in enumerate(COLOR_TABLE):
            tk.Button(
                color_frame,
                text=code,
                width=3,
                relief="raised",
                bg=hex_color,
                fg=self._readable_fg(hex_color),
                command=lambda index=i: self._select_color(index),
            ).grid(row=i // 4, column=i % 4, padx=1, pady=1)

        ttk.Separator(left, orient="horizontal").grid(row=4, column=0, sticky="ew", pady=8)
        ttk.Label(left, text="Source Metadata").grid(row=5, column=0, sticky="w")
        metadata = ttk.Frame(left)
        metadata.grid(row=6, column=0, sticky="ew")
        ttk.Label(metadata, text="Grid").grid(row=0, column=0, sticky="w")
        ttk.Entry(metadata, textvariable=self.var_grid, width=8).grid(row=0, column=1, sticky="w")
        ttk.Label(left, text="Grid is saved locally; it is not transmitted.", foreground="#666").grid(row=7, column=0, sticky="w")

        center = ttk.Frame(self, padding=6)
        center.grid(row=0, column=1, sticky="nsew")
        center.rowconfigure(0, weight=1)
        center.columnconfigure(0, weight=1)

        self.canvas_frame = tk.Frame(center, bg="#303030")
        self.canvas_frame.grid(row=0, column=0, sticky="nsew")
        self.canvas_frame.rowconfigure(0, weight=1)
        self.canvas_frame.columnconfigure(0, weight=1)

        self.canvas = tk.Canvas(
            self.canvas_frame,
            width=CANVAS_W,
            height=CANVAS_H,
            bg="white",
            highlightthickness=1,
            highlightbackground="#999",
            bd=0,
        )
        self.canvas.grid(row=0, column=0)
        self.canvas.bind("<Button-1>", self._on_canvas_click)
        self.canvas.bind("<Motion>", self._on_canvas_motion)
        self.canvas_frame.bind("<Button-1>", self._on_canvas_click)
        self.canvas_frame.bind("<Motion>", self._on_canvas_motion)

        bottom = ttk.Frame(center)
        bottom.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        bottom.columnconfigure(0, weight=1)
        ttk.Label(bottom, textvariable=self.var_status).grid(row=0, column=0, sticky="w")
        ttk.Button(bottom, text="Clear Pending Click", command=self._clear_pending).grid(row=0, column=1, padx=3)
        ttk.Button(bottom, text="Add Pending", command=self._add_pending_command).grid(row=0, column=2, padx=3)
        ttk.Button(bottom, text="Save Source", command=self._save_source_dialog).grid(row=0, column=3, padx=3)
        ttk.Button(bottom, text="Load / Import", command=self._load_source_dialog).grid(row=0, column=4, padx=3)
        ttk.Button(bottom, text="Export MeshCore", command=self._export_transport_dialog).grid(row=0, column=5, padx=3)
        ttk.Button(bottom, text="Preview Frames", command=self._preview_frames).grid(row=0, column=6, padx=3)
        ttk.Button(bottom, text="Export PNG", command=self._export_png).grid(row=0, column=7, padx=3)

        right = ttk.Frame(self, padding=6)
        right.grid(row=0, column=2, sticky="ns")
        right.rowconfigure(3, weight=1)

        codec_box = ttk.LabelFrame(right, text="Compressed Transport", padding=8)
        codec_box.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(codec_box, textvariable=self.var_codec_stats, justify="left").grid(row=0, column=0, sticky="w")

        ttk.Label(right, textvariable=self.var_selection).grid(row=1, column=0, sticky="w")
        ttk.Label(right, text="Layers / Draw Order").grid(row=2, column=0, sticky="w")
        self.layer_list = tk.Listbox(right, width=46, height=10)
        self.layer_list.grid(row=3, column=0, sticky="nsew")
        self.layer_list.bind("<<ListboxSelect>>", self._on_layer_select)

        layer_buttons = ttk.Frame(right)
        layer_buttons.grid(row=4, column=0, sticky="ew", pady=4)
        ttk.Button(layer_buttons, text="Load Fields", command=self._load_selected_fields).grid(row=0, column=0, padx=2)
        ttk.Button(layer_buttons, text="Replace", command=self._replace_selected).grid(row=0, column=1, padx=2)
        ttk.Button(layer_buttons, text="Duplicate", command=self._duplicate_selected).grid(row=0, column=2, padx=2)
        ttk.Button(layer_buttons, text="Up", command=lambda: self._move_selected(-1)).grid(row=0, column=3, padx=2)
        ttk.Button(layer_buttons, text="Down", command=lambda: self._move_selected(1)).grid(row=0, column=4, padx=2)
        ttk.Button(layer_buttons, text="Delete", command=self._delete_selected).grid(row=0, column=5, padx=2)

        controls = ttk.LabelFrame(right, text="Command Builder", padding=8)
        controls.grid(row=5, column=0, sticky="ew", pady=(8, 0))
        controls.columnconfigure(1, weight=1)
        self.control_fields: Dict[str, Tuple[ttk.Label, tk.Widget]] = {}

        row = 0

        def add_entry(key: str, label: str, variable: tk.Variable, width: int = 12) -> None:
            nonlocal row
            lbl = ttk.Label(controls, text=label)
            lbl.grid(row=row, column=0, sticky="w")
            widget = ttk.Entry(controls, textvariable=variable, width=width)
            widget.grid(row=row, column=1, sticky="ew")
            self.control_fields[key] = (lbl, widget)
            row += 1

        def add_spin(key: str, label: str, variable: tk.Variable, lo: int, hi: int) -> None:
            nonlocal row
            lbl = ttk.Label(controls, text=label)
            lbl.grid(row=row, column=0, sticky="w")
            widget = ttk.Spinbox(controls, textvariable=variable, from_=lo, to=hi, width=7)
            widget.grid(row=row, column=1, sticky="w")
            self.control_fields[key] = (lbl, widget)
            row += 1

        add_spin("x1", "X / X1", self.var_x1, 0, CANVAS_W - 1)
        add_spin("y1", "Y / Y1", self.var_y1, 0, CANVAS_H - 1)
        add_spin("x2", "X2", self.var_x2, 0, CANVAS_W - 1)
        add_spin("y2", "Y2", self.var_y2, 0, CANVAS_H - 1)
        add_entry("text", "Text", self.var_text, 18)
        add_spin("orientation", "Orientation", self.var_orientation, 0, 3)
        add_spin("scale", "Scale", self.var_scale, 1, 64)
        add_spin("radius_h", "Radius H / R", self.var_radius_h, 1, 128)
        add_spin("radius_w", "Radius W", self.var_radius_w, 1, 128)
        add_spin("start_angle", "Start angle", self.var_start_angle, 0, 360)
        add_spin("arc_degrees", "Arc degrees", self.var_arc_degrees, 0, 360)
        add_spin("fill", "Fill 0/1", self.var_fill, 0, 1)
        add_spin("crater_color", "Crater color index", self.var_crater_color, 0, len(COLOR_TABLE) - 1)
        add_spin("percent", "Divider %", self.var_percent, 0, 100)

        ttk.Button(controls, text="Build From Fields", command=self._build_pending_from_fields).grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=(6, 0)
        )

    @staticmethod
    def _readable_fg(hex_color: str) -> str:
        r, g, b = int(hex_color[1:3], 16), int(hex_color[3:5], 16), int(hex_color[5:7], 16)
        return "black" if 0.299 * r + 0.587 * g + 0.114 * b > 150 else "white"

    def _set_status(self, message: str) -> None:
        self.var_status.set(message)

    def _select_shape(self, shape: ShapeDef) -> None:
        self.selected_shape = shape
        self.pending_first_click = None
        if shape.default_fill is not None:
            self.var_fill.set(shape.default_fill)
        self._refresh_controls_from_shape()
        self._refresh_selection_label()
        click_text = "two canvas points" if shape.needs_second_click else "the canvas"
        self._set_status(f"Selected {shape.code}: {shape.name}. Click {click_text}.")

    def _select_color(self, index: int) -> None:
        self.selected_color = index
        self._refresh_selection_label()

    def _refresh_selection_label(self) -> None:
        code, name, _hex = COLOR_TABLE[self.selected_color]
        self.var_selection.set(f"Shape {self.selected_shape.code}: {self.selected_shape.name} | Color {code}: {name}")

    def _relevant_fields(self, opcode: int) -> set[str]:
        common_point = {"x1", "y1"}
        mapping = {
            0x0: common_point | {"text"},
            0x1: common_point | {"x2", "y2"},
            0x2: common_point | {"x2", "y2", "fill"},
            0x3: common_point | {"radius_h", "radius_w", "scale", "fill"},
            0x4: common_point | {"orientation", "scale"},
            0x5: common_point | {"orientation", "scale"},
            0x6: common_point | {"orientation", "scale"},
            0x7: common_point | {"radius_h", "scale"},
            0x8: common_point | {"radius_h", "scale", "start_angle", "arc_degrees"},
            0x9: common_point | {"orientation", "scale"},
            0xA: common_point | {"orientation", "scale"},
            0xB: common_point | {"orientation", "scale"},
            0xC: common_point | {"radius_h", "scale", "start_angle", "arc_degrees"},
            0xD: common_point | {"scale", "crater_color"},
            0xE: common_point | {"x2", "y2", "percent"},
        }
        return mapping.get(opcode, common_point)

    def _refresh_controls_from_shape(self) -> None:
        relevant = self._relevant_fields(self.selected_shape.opcode)
        for key, (label, widget) in getattr(self, "control_fields", {}).items():
            enabled = key in relevant
            widget.configure(state="normal" if enabled else "disabled")
            try:
                label.configure(foreground="" if enabled else "#888888")
            except tk.TclError:
                pass
        self._refresh_selection_label()

    def _event_to_canvas_xy(self, event) -> Tuple[int, int]:
        if event.widget is self.canvas:
            raw_x, raw_y = event.x, event.y
        else:
            raw_x = event.x - self.canvas.winfo_x()
            raw_y = event.y - self.canvas.winfo_y()
        return clamp(raw_x, 0, CANVAS_W - 1), clamp(raw_y, 0, CANVAS_H - 1)

    def _on_canvas_motion(self, event) -> None:
        x, y = self._event_to_canvas_xy(event)
        self._set_status(f"Canvas ({x}, {y}) | {len(self.commands)} layers | click to build {self.selected_shape.name}")

    def _on_canvas_click(self, event) -> None:
        if len(self.commands) >= MAX_EDITOR_COMMANDS:
            messagebox.showerror("Editor limit", f"Editor safety limit is {MAX_EDITOR_COMMANDS} commands.")
            return
        x, y = self._event_to_canvas_xy(event)
        if self.selected_shape.needs_second_click:
            if self.pending_first_click is None:
                self.pending_first_click = (x, y)
                self.var_x1.set(x)
                self.var_y1.set(y)
                self._set_status("First point stored. Click the second point.")
                self._render_all()
                return
            x1, y1 = self.pending_first_click
            self.pending_first_click = None
            self.var_x1.set(x1)
            self.var_y1.set(y1)
            self.var_x2.set(x)
            self.var_y2.set(y)
        else:
            self.var_x1.set(x)
            self.var_y1.set(y)
        self._build_pending_from_fields()

    def _command_from_fields(self) -> DrawCommand:
        op = self.selected_shape.opcode
        x1 = clamp(self.var_x1.get(), 0, CANVAS_W - 1)
        y1 = clamp(self.var_y1.get(), 0, CANVAS_H - 1)
        x2 = clamp(self.var_x2.get(), 0, CANVAS_W - 1)
        y2 = clamp(self.var_y2.get(), 0, CANVAS_H - 1)
        fields: Dict[str, Any]

        if op in {0x1, 0x2, 0xE}:
            fields = {"x1": x1, "y1": y1, "x2": x2, "y2": y2}
        else:
            fields = {"x": x1, "y": y1}

        if op == 0x0:
            fields["text"] = clean_text(self.var_text.get())
        elif op == 0x2:
            fields["fill"] = clamp(self.var_fill.get(), 0, 1)
        elif op == 0x3:
            fields.update(
                radius_h=clamp(self.var_radius_h.get(), 1, 128),
                radius_w=clamp(self.var_radius_w.get(), 1, 128),
                scale=clamp(self.var_scale.get(), 1, 64),
                fill=clamp(self.var_fill.get(), 0, 1),
            )
        elif op in {0x4, 0x5, 0x6, 0x9, 0xA, 0xB}:
            fields.update(
                orientation=clamp(self.var_orientation.get(), 0, 3),
                scale=clamp(self.var_scale.get(), 1, 64),
            )
        elif op == 0x7:
            fields.update(radius=clamp(self.var_radius_h.get(), 1, 128), scale=clamp(self.var_scale.get(), 1, 64))
        elif op in {0x8, 0xC}:
            fields.update(
                radius=clamp(self.var_radius_h.get(), 1, 128),
                scale=clamp(self.var_scale.get(), 1, 64),
                start_angle=clamp(self.var_start_angle.get(), 0, 360),
                arc_degrees=clamp(self.var_arc_degrees.get(), 0, 360),
            )
        elif op == 0xD:
            fields.update(
                scale=clamp(self.var_scale.get(), 1, 64),
                crater_color=clamp(self.var_crater_color.get(), 0, len(COLOR_TABLE) - 1),
            )
        elif op == 0xE:
            fields["percent"] = clamp(self.var_percent.get(), 0, 100)

        command = DrawCommand(op, self.selected_color, fields)
        validate_command(command)
        return command

    def _build_pending_from_fields(self) -> None:
        try:
            command = self._command_from_fields()
        except (CodecError, tk.TclError, ValueError) as exc:
            messagebox.showerror("Invalid command", str(exc))
            return
        self.pending_command = command
        self._render_all()
        try:
            render_command(command, self.canvas, "tk")
        except CodecError as exc:
            messagebox.showerror("Preview error", str(exc))
            return
        self._set_status(f"Pending: {command_summary(command)}. Click Add Pending to add it as a layer.")

    def _add_pending_command(self) -> None:
        if self.pending_command is None:
            self._build_pending_from_fields()
        if self.pending_command is None:
            return
        self.commands.append(self.pending_command.clone())
        self.pending_command = None
        self._refresh_all_lists_and_stats()
        self._render_all()
        self._set_status(f"Added layer {len(self.commands) - 1}.")

    def _clear_pending(self) -> None:
        self.pending_first_click = None
        self.pending_command = None
        self._render_all()
        self._set_status("Pending click and command cleared.")

    def _refresh_layer_list(self) -> None:
        self.layer_list.delete(0, tk.END)
        previous: Optional[DrawCommand] = None
        for i, command in enumerate(self.commands):
            repeat = translated_repeat_delta(previous, command)
            suffix = " [translated repeat]" if repeat is not None else ""
            self.layer_list.insert(tk.END, f"{i:03d}: {command_summary(command)}{suffix}")
            previous = command

    def _refresh_codec_stats(self) -> None:
        try:
            encoded = encode_image(self.commands)
            stats = encoded.stats
            fit_text = "FITS" if stats.fits else f"OVER BY {stats.frame_count - MAX_MESSAGES} FRAME(S)"
            self.var_codec_stats.set(
                f"{stats.command_count} commands | {stats.bit_count} bits / {stats.packed_bytes} bytes\n"
                f"{stats.base91_chars} Base91 chars | {stats.frame_count}/{MAX_MESSAGES} frames — {fit_text}\n"
                f"{stats.total_transport_chars} transmitted chars | {stats.repeat_macros} repeat macros\n"
                f"Image ID: {encoded.image_id} | 15-char header + ≤135-char payload"
            )
        except Exception as exc:
            self.var_codec_stats.set(f"Codec error: {exc}")

    def _refresh_all_lists_and_stats(self) -> None:
        self._refresh_layer_list()
        self._refresh_codec_stats()
        self._refresh_selection_label()

    def _on_layer_select(self, _event=None) -> None:
        selection = self.layer_list.curselection()
        self.selected_layer_index = selection[0] if selection else None
        if self.selected_layer_index is not None:
            self._render_all(upto=self.selected_layer_index)
            self._set_status(f"Previewing through layer {self.selected_layer_index}.")

    def _load_selected_fields(self) -> None:
        idx = self.selected_layer_index
        if idx is None:
            messagebox.showinfo("No layer selected", "Select a layer first.")
            return
        command = self.commands[idx]
        self.selected_shape = command.shape
        self.selected_color = command.color
        f = command.fields
        if command.opcode in {0x1, 0x2, 0xE}:
            self.var_x1.set(f["x1"]); self.var_y1.set(f["y1"])
            self.var_x2.set(f["x2"]); self.var_y2.set(f["y2"])
        else:
            self.var_x1.set(f["x"]); self.var_y1.set(f["y"])
        if "text" in f: self.var_text.set(f["text"])
        if "orientation" in f: self.var_orientation.set(f["orientation"])
        if "scale" in f: self.var_scale.set(f["scale"])
        if "radius" in f: self.var_radius_h.set(f["radius"])
        if "radius_h" in f: self.var_radius_h.set(f["radius_h"])
        if "radius_w" in f: self.var_radius_w.set(f["radius_w"])
        if "start_angle" in f: self.var_start_angle.set(f["start_angle"])
        if "arc_degrees" in f: self.var_arc_degrees.set(f["arc_degrees"])
        if "fill" in f: self.var_fill.set(f["fill"])
        if "crater_color" in f: self.var_crater_color.set(f["crater_color"])
        if "percent" in f: self.var_percent.set(f["percent"])
        self._refresh_controls_from_shape()
        self._set_status(f"Loaded layer {idx} into the builder. Change fields, then click Replace.")

    def _replace_selected(self) -> None:
        idx = self.selected_layer_index
        if idx is None:
            messagebox.showinfo("No layer selected", "Select a layer first.")
            return
        try:
            command = self._command_from_fields()
        except Exception as exc:
            messagebox.showerror("Invalid command", str(exc))
            return
        self.commands[idx] = command
        self.pending_command = None
        self._refresh_all_lists_and_stats()
        self.layer_list.select_set(idx)
        self._render_all(upto=idx)
        self._set_status(f"Replaced layer {idx}.")

    def _duplicate_selected(self) -> None:
        idx = self.selected_layer_index
        if idx is None:
            messagebox.showinfo("No layer selected", "Select a layer first.")
            return
        self.commands.insert(idx + 1, self.commands[idx].clone())
        self.selected_layer_index = idx + 1
        self._refresh_all_lists_and_stats()
        self.layer_list.select_set(idx + 1)
        self._render_all()
        self._set_status("Duplicated selected layer. Moving it produces a translated-repeat macro automatically.")

    def _move_selected(self, delta: int) -> None:
        idx = self.selected_layer_index
        if idx is None:
            messagebox.showinfo("No layer selected", "Select a layer first.")
            return
        new_idx = idx + delta
        if not 0 <= new_idx < len(self.commands):
            return
        self.commands[idx], self.commands[new_idx] = self.commands[new_idx], self.commands[idx]
        self.selected_layer_index = new_idx
        self._refresh_all_lists_and_stats()
        self.layer_list.select_set(new_idx)
        self._render_all()

    def _delete_selected(self) -> None:
        idx = self.selected_layer_index
        if idx is None:
            messagebox.showinfo("No layer selected", "Select a layer first.")
            return
        del self.commands[idx]
        self.selected_layer_index = None
        self._refresh_all_lists_and_stats()
        self._render_all()
        self._set_status("Deleted selected layer.")

    def _render_all(self, upto: Optional[int] = None) -> None:
        self.canvas.delete("all")
        self.canvas.create_rectangle(0, 0, CANVAS_W, CANVAS_H, fill="white", outline="")
        commands = self.commands if upto is None else self.commands[: upto + 1]
        for i, command in enumerate(commands):
            try:
                render_command(command, self.canvas, "tk")
            except Exception as exc:
                self._set_status(f"Render error on layer {i}: {exc}")
                break
        if self.pending_first_click is not None:
            x, y = self.pending_first_click
            self.canvas.create_oval(x - 3, y - 3, x + 3, y + 3, fill="red", outline="red")

    def _valid_grid_or_none(self) -> Optional[str]:
        try:
            grid = validate_grid_locator(self.var_grid.get())
            self.var_grid.set(grid)
            return grid
        except CodecError as exc:
            messagebox.showerror("Invalid grid", str(exc))
            return None

    def _save_source_dialog(self) -> None:
        grid = self._valid_grid_or_none()
        if grid is None:
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".mci.json",
            filetypes=[("MCoreIMG source", "*.mci.json"), ("JSON", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            save_source(path, self.commands, grid)
        except Exception as exc:
            messagebox.showerror("Save failed", str(exc))
            return
        self._set_status(f"Saved {len(self.commands)} structured commands to {path}")

    def _load_source_dialog(self) -> None:
        path = filedialog.askopenfilename(
            filetypes=[
                ("MCoreIMG / EMEIMG", "*.mci.json *.mci *.emeimg *.txt *.json"),
                ("All files", "*.*"),
            ]
        )
        if not path:
            return
        try:
            commands, grid, source_type = load_source(path)
        except Exception as exc:
            messagebox.showerror("Load failed", str(exc))
            return
        self.commands = commands
        self.var_grid.set(grid)
        self.selected_layer_index = None
        self.pending_first_click = None
        self.pending_command = None
        self._refresh_all_lists_and_stats()
        self._render_all()
        self._set_status(f"Loaded {len(commands)} commands from {source_type}: {path}")

    def _export_transport_dialog(self) -> None:
        try:
            encoded = encode_image(self.commands)
        except Exception as exc:
            messagebox.showerror("Encoding failed", str(exc))
            return
        if not encoded.stats.fits:
            messagebox.showerror(
                "Image exceeds MeshCore profile",
                f"This image needs {encoded.stats.frame_count} messages. The profile allows {MAX_MESSAGES}.\n\n"
                f"Current payload: {encoded.stats.base91_chars} Base91 characters.\n"
                f"Maximum payload: {MAX_MESSAGES * FRAME_PAYLOAD_LEN} characters.",
            )
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".mci",
            filetypes=[("MCoreIMG MeshCore frames", "*.mci"), ("Text", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            Path(path).write_text("\n".join(encoded.frames) + "\n", encoding="ascii")
        except Exception as exc:
            messagebox.showerror("Export failed", str(exc))
            return
        self._set_status(f"Exported {len(encoded.frames)} MeshCore frames for image {encoded.image_id} to {path}")

    def _preview_frames(self) -> None:
        try:
            encoded = encode_image(self.commands)
        except Exception as exc:
            messagebox.showerror("Encoding failed", str(exc))
            return
        if not encoded.stats.fits:
            messagebox.showerror("Image exceeds profile", f"Encoding requires {encoded.stats.frame_count} messages.")
            return
        window = tk.Toplevel(self)
        window.title(f"MCoreIMG Frames — {encoded.image_id}")
        window.geometry("980x430")
        text = tk.Text(window, wrap="none", font=("TkFixedFont", 11))
        text.pack(fill="both", expand=True, padx=8, pady=8)
        for i, frame in enumerate(encoded.frames):
            text.insert("end", f"Part {i}/{len(encoded.frames) - 1} — {len(frame)} chars\n{frame}\n\n")
        text.configure(state="disabled")

        def copy_all() -> None:
            self.clipboard_clear()
            self.clipboard_append("\n".join(encoded.frames))
            self._set_status(f"Copied {len(encoded.frames)} MeshCore frames to clipboard.")

        ttk.Button(window, text="Copy All Frames", command=copy_all).pack(pady=(0, 8))

    def _export_png(self) -> None:
        if Image is None or ImageDraw is None:
            messagebox.showerror("Pillow missing", "Install Pillow first: sudo pacman -S python-pillow")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".png",
            filetypes=[("PNG image", "*.png"), ("All files", "*.*")],
        )
        if not path:
            return
        image = Image.new("RGB", (CANVAS_W, CANVAS_H), "white")
        draw = ImageDraw.Draw(image)
        try:
            text_font, font_source = load_text_font(DEFAULT_TEXT_FONT_SIZE)
            for command in self.commands:
                render_command(command, draw, "pil", text_font=text_font)
            image.save(path)
        except Exception as exc:
            image.close()
            messagebox.showerror("PNG export failed", str(exc))
            return
        image.close()
        self._set_status(
            f"Exported PNG: {path} | text={DEFAULT_TEXT_FONT_SIZE}px | font={font_source}"
        )


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------


def sample_commands() -> List[DrawCommand]:
    return [
        DrawCommand(0x0, 0, {"x": 20, "y": 20, "text": "KE9ETA"}),
        DrawCommand(0x1, 3, {"x1": 30, "y1": 70, "x2": 220, "y2": 90}),
        DrawCommand(0x2, 5, {"x1": 50, "y1": 110, "x2": 180, "y2": 180, "fill": 0}),
        DrawCommand(0x2, 5, {"x1": 210, "y1": 110, "x2": 340, "y2": 180, "fill": 0}),
        DrawCommand(0x3, 4, {"x": 430, "y": 145, "radius_h": 20, "radius_w": 35, "scale": 1, "fill": 1}),
        DrawCommand(0x4, 7, {"x": 100, "y": 260, "orientation": 1, "scale": 2}),
        DrawCommand(0x5, 8, {"x": 170, "y": 260, "orientation": 1, "scale": 2}),
        DrawCommand(0x6, 9, {"x": 250, "y": 235, "orientation": 2, "scale": 1}),
        DrawCommand(0x7, 6, {"x": 350, "y": 260, "radius": 25, "scale": 1}),
        DrawCommand(0x8, 2, {"x": 450, "y": 260, "radius": 30, "scale": 1, "start_angle": 0, "arc_degrees": 180}),
        DrawCommand(0x9, 0, {"x": 600, "y": 50, "orientation": 0, "scale": 1}),
        DrawCommand(0xA, 3, {"x": 600, "y": 200, "orientation": 1, "scale": 1}),
        DrawCommand(0xB, 0, {"x": 520, "y": 350, "orientation": 0, "scale": 2}),
        DrawCommand(0xC, 5, {"x": 300, "y": 400, "radius": 8, "scale": 1, "start_angle": 315, "arc_degrees": 90}),
        DrawCommand(0xD, 6, {"x": 650, "y": 390, "scale": 1, "crater_color": 2}),
        DrawCommand(0xE, 0, {"x1": 20, "y1": 330, "x2": 180, "y2": 450, "percent": 40}),
    ]


def run_self_test() -> None:
    # Base91 round trips across varied lengths.
    rng = random.Random(0x4D4349)
    for length in list(range(0, 80)) + [127, 128, 255, 512]:
        data = bytes(rng.randrange(256) for _ in range(length))
        encoded = base91_encode(data)
        decoded = base91_decode(encoded)
        assert decoded == data, f"Base91 round-trip failed at length {length}"

    commands = sample_commands()
    for command in commands:
        validate_command(command)
    encoded = encode_image(commands)
    assert encoded.stats.fits, f"Sample unexpectedly requires {encoded.stats.frame_count} frames"
    decoded = decode_image_frames(encoded.frames)
    assert [cmd.to_json() for cmd in decoded] == [cmd.to_json() for cmd in commands]

    # Frame corruption must be detected.
    bad = encoded.frames.copy()
    pos = FRAME_HEADER_LEN
    replacement = BASE91[(BASE91_INDEX[bad[0][pos]] + 1) % len(BASE91)]
    bad[0] = bad[0][:pos] + replacement + bad[0][pos + 1 :]
    try:
        decode_image_frames(bad)
    except FrameError:
        pass
    else:
        raise AssertionError("Corrupted frame was not rejected")

    # Source JSON round-trip.
    temp = Path("/tmp/mcoreimg-self-test.mci.json")
    save_source(temp, commands, "EN60")
    loaded, grid, source_type = load_source(temp)
    assert grid == "EN60" and source_type == "MCoreIMG JSON source"
    assert [cmd.to_json() for cmd in loaded] == [cmd.to_json() for cmd in commands]
    temp.unlink(missing_ok=True)

    # PNG font regression test: verify the configured 20 px font is truly
    # scalable and not Pillow's historic tiny bitmap fallback.
    if Image is not None and ImageDraw is not None and ImageFont is not None:
        test_font, font_source = load_text_font(DEFAULT_TEXT_FONT_SIZE)
        test_image = Image.new("RGB", (240, 80), "black")
        test_draw = ImageDraw.Draw(test_image)
        bbox = test_draw.textbbox((0, 0), "KE9ETA", font=test_font, anchor="lt")
        rendered_height = bbox[3] - bbox[1]
        rendered_width = bbox[2] - bbox[0]
        test_image.close()
        assert rendered_height >= 12, (
            f"PNG font regression: expected a scalable {DEFAULT_TEXT_FONT_SIZE}px font, "
            f"got bbox {bbox} from {font_source}"
        )
        assert rendered_width >= 45, (
            f"PNG font regression: text is still too narrow: bbox {bbox} from {font_source}"
        )

    print("MCoreIMG constructor self-test: PASS")
    print(
        f"sample: {encoded.stats.command_count} commands, {encoded.stats.bit_count} bits, "
        f"{encoded.stats.base91_chars} Base91 chars, {encoded.stats.frame_count} frame(s), "
        f"{encoded.stats.repeat_macros} translated-repeat macro(s)"
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="MCoreIMG compressed vector-image constructor")
    parser.add_argument("--self-test", action="store_true", help="run codec tests without opening the GUI")
    args = parser.parse_args(argv)
    if args.self_test:
        run_self_test()
        return 0
    app = MCoreIMGEditor()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())