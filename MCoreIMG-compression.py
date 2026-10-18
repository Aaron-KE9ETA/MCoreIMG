#!/usr/bin/env python3
"""
MCoreIMG compression — protocol-v6 transport codec
==================================================

This module owns **everything that turns drawing commands into radio bytes and
back again**.  Nothing else in the project may write a bit, choose a record
type, build a palette, or frame a MeshCore message.

It is free of GUI, SVG-parsing, rendering, and file-dialog code so that a
change to the compression scheme is a change to exactly one file.

    MCoreIMG-model.py           <- opcodes, model, geometry (imported here)
    MCoreIMG-compression.py     <- you are here: codec and framing
    MCoreIMG-Constructor.py     <- SVG import, rendering, editor GUI
    MCoreIMG-Reconstructor.py   <- frame decoding and export

WHAT LIVES HERE
---------------

1.  Protocol identity, the MeshCore message envelope, and record tags.
2.  Palette construction and RGB565 + 4-bit alpha quantization.
3.  Bit IO and the stateful point and style codecs.
4.  Record writers and readers, including translated single-command and
    contiguous-group repeats.
5.  Local-space SVG group planning (scale-independent groups).
6.  Base91 payload coding, Base62 header fields, CRC, and MeshCore framing.

WHAT LIVES IN THE MODEL MODULE
------------------------------

``PaintStyle``, ``VectorCommand``, the opcode and primitive vocabularies, the
matrix helpers, and the geometry operations (``transform_command``,
``quantize_command``, ``validate_command``, ``command_points``).  Those are
re-exported here for convenience, so importing this module gives a caller the
whole transport vocabulary, but they are *defined* in ``MCoreIMG-model.py``.

The dividing line is transport quantization.  Rounding a coordinate to an
integer is a model concern; rounding a colour to RGB565 with 4-bit alpha is a
wire-format concern and therefore lives here.

PUBLIC API
----------

``encode_image(commands) -> EncodedImage``
    Full pipeline: plan, compress, frame.  This is what the Constructor calls.

``decode_frames(frames) -> list[VectorCommand]``
    Full inverse pipeline with CRC and envelope validation.

``encode_commands(commands, local_extent=...) -> (bytes, bit_count, metrics, palette)``
``decode_commands(data, mode) -> (list[VectorCommand], palette)``
    Bitstream layer only, for tests and tooling that bypass framing.

RECORD LAYOUT (protocol 6)
--------------------------

A stream is::

    [4 bits protocol version]
    [ue palette_count-1] [ 16-bit RGB565 + 4-bit alpha ] * palette_count
    [ue command_count]
    record*

Each record opens with a two-bit tag:

======================  ====  =======================================
``REC_NORMAL``          0     A fully specified command.
``REC_SINGLE_REPEAT``   1     Re-emit one earlier command at a delta.
``REC_GROUP_REPEAT``    2     Re-emit a contiguous run at a delta.
``REC_TRANSFORM_GROUP`` 3     A local-space SVG group: fixed-width display
                              box plus either an inline local definition or
                              a back-reference to an earlier definition.
======================  ====  =======================================

WHY LOCAL SPACE MATTERS
-----------------------

Display scale is carried in a fixed-width transform box, never baked into
coordinates.  Geometry is normalized into a stable local box before encoding,
so the same artwork costs the same number of bits at 100%, 50%, or 10% display
scale.  ``encode_image`` picks the highest local precision that still fits the
ten-message budget.

EDITING THIS FILE
-----------------

Any change to a writer must be mirrored in its reader, and the two must be
exercised by the round-trip fixtures in the Constructor's ``--self-test``.  A
change that alters emitted bits for unchanged input is a protocol change:
bump :data:`PROTOCOL_VERSION` and update the Reconstructor in the same commit.
"""

from __future__ import annotations

import copy
import itertools
import importlib.util
import json
import math
import sys
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

COMPRESSION_BUILD = "2026.10.17-compression-v7.1-CHANNEL120"


# ===========================================================================
# Model import
# ===========================================================================
#
# The model ships as "MCoreIMG-model.py". The hyphen is not a legal Python
# identifier, so it is loaded by path. An already-imported instance is reused:
# two copies would define two distinct VectorCommand classes and objects from
# one would not be recognised by the other.

MODEL_FILENAME = "MCoreIMG-model.py"


def load_model(explicit=None):
    """Import the model module from an explicit path or from beside us."""
    candidates = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.append(Path(__file__).resolve().parent / MODEL_FILENAME)
    candidates.append(Path.cwd() / MODEL_FILENAME)

    tried = []
    for path in candidates:
        tried.append(str(path))
        if not path.is_file():
            continue
        existing = sys.modules.get("mcoreimg_model")
        if existing is not None:
            existing_file = getattr(existing, "__file__", None)
            if existing_file and Path(existing_file).resolve() == path.resolve():
                return existing
        spec = importlib.util.spec_from_file_location("mcoreimg_model", path)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules["mcoreimg_model"] = module
        spec.loader.exec_module(module)
        return module

    searched = "\n".join(f"  - {item}" for item in tried)
    raise ImportError(
        f"Cannot find {MODEL_FILENAME}.\n\nSearched:\n{searched}"
    )


model = load_model()

# Re-exported so that importing the codec yields the full transport vocabulary.
MCIError = model.MCIError
PaintStyle = model.PaintStyle
VectorCommand = model.VectorCommand
Matrix = model.Matrix
IDENTITY = model.IDENTITY

CANVAS_W = model.CANVAS_W
CANVAS_H = model.CANVAS_H

OP_RECT = model.OP_RECT
OP_ELLIPSE = model.OP_ELLIPSE
OP_LINE = model.OP_LINE
OP_POLYLINE = model.OP_POLYLINE
OP_POLYGON = model.OP_POLYGON
OP_PATH = model.OP_PATH
OP_PRIMITIVE = model.OP_PRIMITIVE
OP_NAMES = model.OP_NAMES

SEG_M = model.SEG_M
SEG_L = model.SEG_L
SEG_Q = model.SEG_Q
SEG_C = model.SEG_C
SEG_Z = model.SEG_Z
SEG_NAMES = model.SEG_NAMES
SEG_POINT_COUNTS = model.SEG_POINT_COUNTS

PRIM_TEXT = model.PRIM_TEXT
PRIM_TRIANGLE_OUTLINE = model.PRIM_TRIANGLE_OUTLINE
PRIM_TRIANGLE_FILL = model.PRIM_TRIANGLE_FILL
PRIM_ARROW = model.PRIM_ARROW
PRIM_STAR = model.PRIM_STAR
PRIM_ARC = model.PRIM_ARC
PRIM_YAGI = model.PRIM_YAGI
PRIM_DISH = model.PRIM_DISH
PRIM_RADIO = model.PRIM_RADIO
PRIM_RADIO_WAVES = model.PRIM_RADIO_WAVES
PRIM_MOON = model.PRIM_MOON
PRIM_DOUBLE_BOX = model.PRIM_DOUBLE_BOX
PRIMITIVE_NAMES = model.PRIMITIVE_NAMES
PRIMITIVE_BY_NAME = model.PRIMITIVE_BY_NAME
TEXT_ALPHABET = model.TEXT_ALPHABET
TEXT_INDEX = model.TEXT_INDEX
MAX_TEXT_LEN = model.MAX_TEXT_LEN
MOON_CRATER_POINTS = model.MOON_CRATER_POINTS
primitive_to_vectors = model.primitive_to_vectors
primitive_kind = model.primitive_kind
clean_primitive_text = model.clean_primitive_text
primitive_anchor = model.primitive_anchor

mat_mul = model.mat_mul
mat_translate = model.mat_translate
mat_scale = model.mat_scale
mat_rotate = model.mat_rotate
apply_mat = model.apply_mat
is_axis_aligned = model.is_axis_aligned
clamp_int = model.clamp_int
normalize_hex = model.normalize_hex
color_to_rgba = model.color_to_rgba
rgba_to_hex = model.rgba_to_hex
rgb565 = model.rgb565
alpha4 = model.alpha4
from_rgb565 = model.from_rgb565
from_rgb565_a4 = model.from_rgb565_a4
quantize_palette_color = model.quantize_palette_color
cubic_point = model.cubic_point
quad_point = model.quad_point
flatten_path = model.flatten_path
command_points = model.command_points
commands_bbox = model.commands_bbox
transform_command = model.transform_command
translate_command = model.translate_command
quantize_command = model.quantize_command
validate_command = model.validate_command


# ==========================================================================
# Protocol constants
# ==========================================================================


# PROTOCOL_VERSION is written into the bitstream. The frame format version is
# carried by the magic strings themselves (see the header layout below).
PROTOCOL_VERSION = 7

# MeshCore transport profile. Keep the arithmetic in one place so GUI counters,
# encoder limits, and decoder validation cannot drift apart.
MAX_MESSAGES = 10
# The target is a MeshCore channel, where a message is limited to 120
# characters (direct messages allow 150). Every frame, data or parity, must fit.
MESSAGE_LEN = 120
# Header layout (protocol 7), all Base62:
#   MCI   magic            3   also the frame-format version: a future format
#                              changes the magic rather than spending a
#                              character on a version field
#   iiii  image id         4   ties every message of one image together
#   p     part descriptor  1   index + 10*final + 20*mode
#
# Messages from many images can arrive interleaved on a shared channel, so the
# image id is what sorts them into sets. It is a hash of the encoded stream,
# which has two consequences: identical images sent by different stations
# produce identical frames that simply merge, and a receiver can check, once a
# set is complete, that the id really names that content. Four characters give
# 14.8 million ids, where protocol 6's three gave 238 thousand; on a busy
# channel three characters would collide every few thousand images, and a
# collision costs both images. The extra character comes from dropping the
# header version field, so payload capacity is unchanged.
#
# There is no per-frame CRC: MeshCore already guarantees message integrity,
# and the stream CRC-32 still catches mis-assembly (colliding image ids, a
# user pasting the wrong set). There is no length field either: chunking fills
# every frame except the last, so a non-final frame is always exactly
# MESSAGE_LEN characters and the final frame's length is what remains.
IMAGE_ID_LEN = 4
IMAGE_ID_SPACE = 62 ** IMAGE_ID_LEN
FRAME_HEADER_LEN = 3 + IMAGE_ID_LEN + 1
assert FRAME_HEADER_LEN == 8
FRAME_PAYLOAD_LEN = MESSAGE_LEN - FRAME_HEADER_LEN
MAX_PAYLOAD_CHARS = MAX_MESSAGES * FRAME_PAYLOAD_LEN
FRAME_MAGIC = "MCI"

# Defensive decoder limits. These cap allocation and malformed-count loops.
MAX_COMMANDS = 2048
MAX_PALETTE = 32

# Base62 carries fixed-width header fields; Base91 carries the payload and
# excludes quote and backslash so frames stay safe in text transports.
BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
BASE91 = "".join(chr(c) for c in range(33, 127) if chr(c) not in {'"', "'", "\\"})
BASE91_INDEX = {ch: i for i, ch in enumerate(BASE91)}
assert len(BASE91) == 91

# Two-bit record tags.
REC_NORMAL = 0
REC_SINGLE_REPEAT = 1
REC_GROUP_REPEAT = 2
REC_TRANSFORM_GROUP = 3

# Default local-coordinate extent for normalized SVG groups. encode_image walks
# a ladder downward from here when the ten-message budget is tight; it is
# passed explicitly through the encoder rather than held as mutable state.
LOCAL_GROUP_EXTENT = 255
PRECISION_LADDER = (255, 224, 192, 160, 144, 128, 112, 96, 80, 72, 64, 56, 48, 40, 32)

# ==========================================================================
# Errors
# ==========================================================================


class FrameError(MCIError):
    """Raised for damaged, incomplete, mixed, or inconsistent text frames.

    Subclasses the model's MCIError so a caller can catch one exception type
    for both malformed geometry and malformed transport.
    """

    pass

# ==========================================================================
# Transport-precision command comparison
# ==========================================================================

# These two are transport concerns rather than model concerns: they compare
# commands at the precision the wire actually carries, which means colours
# quantized to RGB565 with 4-bit alpha. Two commands the editor would call
# different can be identical once transmitted, and the repeat planner needs
# to know that.


def _style_transport_signature(style: PaintStyle) -> Tuple[Any, ...]:
    normalized = style.normalized()
    return (
        quantize_palette_color(normalized.fill) if normalized.fill else None,
        quantize_palette_color(normalized.stroke) if normalized.stroke else None,
        max(1, min(64, int(round(normalized.stroke_width)))) if normalized.stroke else 0,
        normalized.fill_rule,
    )


def geom_translation(prev: VectorCommand, cur: VectorCommand) -> Optional[Tuple[int, int]]:
    if prev.opcode != cur.opcode or _style_transport_signature(prev.style) != _style_transport_signature(cur.style):
        return None
    if prev.opcode == OP_PRIMITIVE and primitive_kind(prev) != primitive_kind(cur):
        return None
    pa = command_points(prev)
    pb = command_points(cur)
    if len(pa) != len(pb) or not pa:
        return None
    dx = int(round(pb[0][0] - pa[0][0]))
    dy = int(round(pb[0][1] - pa[0][1]))
    translated = quantize_command(translate_command(prev, dx, dy))
    target = quantize_command(cur)
    if translated.opcode != target.opcode:
        return None
    if translated.style.to_json() != target.style.to_json() or translated.geom != target.geom:
        return None
    return dx, dy

# ==========================================================================
# Palette construction
# ==========================================================================


# Colour quantization itself lives in the model (see quantize_palette_color).
# What belongs here is assembling the ordered palette and enforcing the codec's
# 32-entry ceiling, both of which are wire-format decisions.
def build_palette(commands: Sequence[VectorCommand]) -> List[str]:
    palette: List[str] = []
    for command in commands:
        for color in (command.style.fill, command.style.stroke):
            if color:
                quant = quantize_palette_color(color)
                if quant not in palette:
                    palette.append(quant)
        if command.opcode == OP_PRIMITIVE and primitive_kind(command) == PRIM_MOON:
            crater = quantize_palette_color(str(command.geom.get("crater_color", "#808080")))
            if crater not in palette:
                palette.append(crater)
    if not palette:
        palette = ["#000000"]
    if len(palette) > MAX_PALETTE:
        raise MCIError(f"Image needs {len(palette)} colors; protocol v{PROTOCOL_VERSION} supports {MAX_PALETTE}.")
    return palette

# ==========================================================================
# Bit IO
# ==========================================================================

# Adaptive binary range coder (LZMA-style).
#
# Replacing raw bit packing with an adaptive arithmetic coder is where the
# remaining redundancy in flags, record tags and Exp-Golomb/Rice prefixes gets
# collected. A flag that is 90% one value costs ~0.47 bits instead of 1.
#
# Bits fall into two classes:
#   contexted  - skewed, worth modelling; each context adapts independently
#   bypass     - near-uniform (coordinate low bits, palette indices), coded at
#                a flat 1 bit to avoid polluting the models
#
# Contexts are plain strings so a new syntax element can be added without
# renumbering a table. They are created on demand and start at p=0.5.

_PROB_BITS = 11
_PROB_ONE = 1 << _PROB_BITS
_PROB_INIT = _PROB_ONE >> 1
# Adaptation rate. LZMA's default of 5 is tuned for megabyte streams; an
# MCoreIMG payload is at most 1,420 characters, so contexts never warm up
# before the stream ends. Measured across the real corpus: 3 gives -0.38%,
# 4 gives -0.93%, 6 gives +0.84%. A two-speed schedule (4 then 5) matched 4
# without beating it, so the constant stays a constant.
_MOVE_BITS = 4
_RC_TOP = 1 << 24
_MASK32 = 0xFFFFFFFF


class RangeEncoder:
    """Binary range encoder with adaptive contexts and a bypass mode."""

    def __init__(self) -> None:
        self.low = 0
        self.range = _MASK32
        self.cache = 0
        self.cache_size = 1
        self.out = bytearray()
        self.probs: Dict[str, int] = {}

    def _shift_low(self) -> None:
        if self.low < 0xFF000000 or self.low > _MASK32:
            carry = self.low >> 32
            temp = self.cache
            while True:
                self.out.append((temp + carry) & 0xFF)
                temp = 0xFF
                self.cache_size -= 1
                if self.cache_size == 0:
                    break
            self.cache = (self.low >> 24) & 0xFF
        self.cache_size += 1
        self.low = (self.low << 8) & _MASK32

    def encode_bit(self, ctx: str, bit: int) -> None:
        prob = self.probs.get(ctx, _PROB_INIT)
        bound = (self.range >> _PROB_BITS) * prob
        if not bit:
            self.range = bound
            self.probs[ctx] = prob + ((_PROB_ONE - prob) >> _MOVE_BITS)
        else:
            self.low += bound
            self.range -= bound
            self.probs[ctx] = prob - (prob >> _MOVE_BITS)
        while self.range < _RC_TOP:
            self.range = (self.range << 8) & _MASK32
            self._shift_low()

    def encode_direct(self, bit: int) -> None:
        self.range >>= 1
        if bit:
            self.low += self.range
        while self.range < _RC_TOP:
            self.range = (self.range << 8) & _MASK32
            self._shift_low()

    def finish(self) -> bytes:
        for _ in range(5):
            self._shift_low()
        # The decoder reads zeros past the end of the buffer, so trailing zero
        # bytes carry no information. On a payload this small the five flush
        # bytes are a real cost, and most of them are usually zero.
        out = bytes(self.out)
        while out and out[-1] == 0:
            out = out[:-1]
        return out


class RangeDecoder:
    """Inverse of RangeEncoder. Context updates must mirror it exactly."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 1  # the encoder's first output byte is padding
        self.range = _MASK32
        self.code = 0
        self.probs: Dict[str, int] = {}
        for _ in range(4):
            self.code = ((self.code << 8) | self._byte()) & _MASK32

    def _byte(self) -> int:
        if self.pos < len(self.data):
            b = self.data[self.pos]
            self.pos += 1
            return b
        self.pos += 1
        return 0

    def decode_bit(self, ctx: str) -> int:
        prob = self.probs.get(ctx, _PROB_INIT)
        bound = (self.range >> _PROB_BITS) * prob
        if self.code < bound:
            self.range = bound
            self.probs[ctx] = prob + ((_PROB_ONE - prob) >> _MOVE_BITS)
            bit = 0
        else:
            self.code -= bound
            self.range -= bound
            self.probs[ctx] = prob - (prob >> _MOVE_BITS)
            bit = 1
        while self.range < _RC_TOP:
            self.range = (self.range << 8) & _MASK32
            self.code = ((self.code << 8) | self._byte()) & _MASK32
        return bit

    def decode_direct(self) -> int:
        self.range >>= 1
        if self.code >= self.range:
            self.code -= self.range
            bit = 1
        else:
            bit = 0
        while self.range < _RC_TOP:
            self.range = (self.range << 8) & _MASK32
            self.code = ((self.code << 8) | self._byte()) & _MASK32
        return bit


# Context depth cap. Beyond this, prefix positions share a model rather than
# creating contexts that would never see enough data to adapt.
_CTX_CAP = 15


class BitWriter:
    """Records syntax elements, then range-codes them on to_bytes().

    Elements rather than bits, because the planner builds competing candidate
    records and splices the winner. An adaptive coder cannot be spliced after
    the fact, so the decision stage works on recorded elements and an estimated
    cost, and entropy coding happens once at the end over the final sequence.

    ``cost`` is the pre-entropy bit count. It is what the planner compares, and
    it deliberately matches the older raw-bit measure so representation choices
    stay stable.
    """

    def __init__(self) -> None:
        self.ops: List[Tuple[str, Optional[str], int]] = []
        self.cost = 0

    def extend(self, other: "BitWriter") -> None:
        self.ops.extend(other.ops)
        self.cost += other.cost

    def bit(self, v, ctx: str = "flag") -> None:
        self.ops.append(("b", ctx, 1 if v else 0))
        self.cost += 1

    def bypass_n(self, v: int, n: int) -> None:
        for shift in range(n - 1, -1, -1):
            self.ops.append(("d", None, (v >> shift) & 1))
        self.cost += n

    def bits_n(self, v: int, n: int, ctx: Optional[str] = None) -> None:
        if v < 0 or v >= (1 << n):
            raise MCIError(f"Value {v} does not fit in {n} bits.")
        if ctx is None:
            self.bypass_n(v, n)
            return
        for shift in range(n - 1, -1, -1):
            self.ops.append(("b", f"{ctx}.{shift}", (v >> shift) & 1))
        self.cost += n

    def ue(self, v: int, ctx: str = "ue") -> None:
        if v < 0:
            raise MCIError("Unsigned Exp-Golomb cannot encode negative values.")
        n = v + 1
        width = n.bit_length()
        for i in range(width - 1):
            self.ops.append(("b", f"{ctx}.p{min(i, _CTX_CAP)}", 0))
        self.ops.append(("b", f"{ctx}.p{min(width - 1, _CTX_CAP)}", 1))
        for shift in range(width - 2, -1, -1):
            self.ops.append(("d", None, (n >> shift) & 1))
        self.cost += 2 * width - 1

    def se(self, v: int, ctx: str = "se") -> None:
        """Signed Exp-Golomb (zigzag + ue).

        Repeat deltas span the whole canvas: an adjacent copy is a few units
        away, a mirrored one on the far side is several hundred. A fixed Rice
        parameter cannot serve both -- k=2 spends over a hundred unary bits on
        a distance of 200, which made distant repeats lose to a full record
        every time. Exp-Golomb grows logarithmically instead.
        """
        self.ue((v << 1) if v >= 0 else ((-v << 1) - 1), ctx)

    def rice_signed(self, v: int, k: int = 3, ctx: str = "rice") -> None:
        z = (-v * 2 - 1) if v < 0 else v * 2
        q = z >> k
        for i in range(q):
            self.ops.append(("b", f"{ctx}.q{min(i, _CTX_CAP)}", 1))
        self.ops.append(("b", f"{ctx}.q{min(q, _CTX_CAP)}", 0))
        if k:
            for shift in range(k - 1, -1, -1):
                self.ops.append(("d", None, (z >> shift) & 1))
        self.cost += q + 1 + k

    def to_bytes_raw(self) -> bytes:
        """Pack elements as literal bits, exactly as protocol 5 did."""
        out = bytearray((len(self.ops) + 7) // 8)
        for i, (_kind, _ctx, bit) in enumerate(self.ops):
            if bit:
                out[i // 8] |= 1 << (7 - i % 8)
        return bytes(out)

    def to_bytes_rc(self) -> bytes:
        """Entropy-code elements with the adaptive range coder."""
        encoder = RangeEncoder()
        for kind, ctx, bit in self.ops:
            if kind == "d":
                encoder.encode_direct(bit)
            else:
                encoder.encode_bit(ctx, bit)
        return encoder.finish()

    def to_bytes(self, mode: int = 1) -> bytes:
        return self.to_bytes_rc() if mode else self.to_bytes_raw()

    def best_bytes(self) -> Tuple[bytes, int]:
        """Return the smaller of the two encodings and its mode flag.

        The range coder pays a fixed flush cost and needs data before its
        models are worth anything, so it loses on very small images. Coding
        both ways and keeping the winner makes protocol 6 never worse than
        protocol 5, at the cost of one extra encode pass over a payload that is
        at most 1,420 characters.
        """
        raw = self.to_bytes_raw()
        coded = self.to_bytes_rc()
        return (coded, 1) if len(coded) < len(raw) else (raw, 0)


class BitReader:
    """Inverse of BitWriter. Every read must mirror its write exactly."""

    def __init__(self, data: bytes, mode: int = 1) -> None:
        self.mode = int(mode)
        self.data = data
        self.pos = 0
        self.rc = RangeDecoder(data) if self.mode else None

    def _raw_bit(self) -> int:
        if self.pos >= len(self.data) * 8:
            raise MCIError("Unexpected end of stream.")
        b = (self.data[self.pos // 8] >> (7 - self.pos % 8)) & 1
        self.pos += 1
        return b

    def bit(self, ctx: str = "flag") -> int:
        return self.rc.decode_bit(ctx) if self.mode else self._raw_bit()

    def bypass_n(self, n: int) -> int:
        v = 0
        if self.mode:
            for _ in range(n):
                v = (v << 1) | self.rc.decode_direct()
        else:
            for _ in range(n):
                v = (v << 1) | self._raw_bit()
        return v

    def bits_n(self, n: int, ctx: Optional[str] = None) -> int:
        if ctx is None:
            return self.bypass_n(n)
        if not self.mode:
            return self.bypass_n(n)
        v = 0
        for shift in range(n - 1, -1, -1):
            v = (v << 1) | self.rc.decode_bit(f"{ctx}.{shift}")
        return v

    def ue(self, max_value: int = 10_000_000, ctx: str = "ue") -> int:
        z = 0
        while self.bit(f"{ctx}.p{min(z, _CTX_CAP)}") == 0:
            z += 1
            if z > 31:
                raise MCIError("Exp-Golomb prefix too long.")
        v = (1 << z) + (self.bypass_n(z) if z else 0) - 1
        if v > max_value:
            raise MCIError("Exp-Golomb value exceeds limit.")
        return v

    def se(self, max_abs: int = 4096, ctx: str = "se") -> int:
        """Inverse of BitWriter.se."""
        z = self.ue(2 * max_abs + 1, ctx)
        v = (z >> 1) if not (z & 1) else -((z + 1) >> 1)
        if abs(v) > max_abs:
            raise MCIError("Signed Exp-Golomb value exceeds limit.")
        return v

    def rice_signed(self, k: int = 3, max_abs: int = 4096, ctx: str = "rice") -> int:
        q = 0
        while self.bit(f"{ctx}.q{min(q, _CTX_CAP)}") == 1:
            q += 1
            if q > 8192:
                raise MCIError("Rice quotient too long.")
        z = (q << k) | (self.bypass_n(k) if k else 0)
        v = -(z // 2) - 1 if z & 1 else z // 2
        if abs(v) > max_abs:
            raise MCIError("Rice value exceeds limit.")
        return v


@dataclass
class PointState:
    """Per-opcode coordinate history for the predictive point codec.

    ``x``/``y`` are the last point; ``prev_x``/``prev_y`` the one before it.
    ``err1``/``err2`` are decayed absolute-residual scores for the first- and
    second-order predictors. Both ends update them identically from decoded
    history, so the predictor choice costs no bits.
    """

    initialized: bool = False
    x: int = 0
    y: int = 0
    prev_x: int = 0
    prev_y: int = 0
    count: int = 0
    err1: int = 0
    err2: int = 0


def predict_point(state: PointState) -> Tuple[int, int]:
    """Predict the next point from shared history.

    First order is "same as last point". Second order extrapolates linearly,
    which tracks flattened Beziers and traced outlines far better because
    those advance in a roughly constant direction.

    The selector is the decayed error score, so the codec follows the artwork
    instead of committing to one predictor. With no history at all the
    prediction is the last point, which degrades to the original behaviour.
    """
    if state.count >= 2 and state.err2 <= state.err1:
        return 2 * state.x - state.prev_x, 2 * state.y - state.prev_y
    return state.x, state.y


def update_point_state(state: PointState, x: int, y: int) -> None:
    """Fold a decoded point into history. Must run identically on both ends."""
    if state.count >= 2:
        second_x = 2 * state.x - state.prev_x
        second_y = 2 * state.y - state.prev_y
        # Decay by 3/4 so the selector tracks recent behaviour rather than the
        # whole command. Integer arithmetic keeps both ends exactly in step.
        state.err1 = (state.err1 * 3) // 4 + abs(x - state.x) + abs(y - state.y)
        state.err2 = (state.err2 * 3) // 4 + abs(x - second_x) + abs(y - second_y)
    state.prev_x, state.prev_y = state.x, state.y
    state.x, state.y = x, y
    state.count += 1
    state.initialized = True


def rice_signed_length(v:int,k:int=3)->int:
    z=(-v*2-1) if v<0 else v*2
    return (z>>k)+1+k


# Conditioning the point coder on previous residual magnitude and on history
# depth was implemented and swept. Every added split made the payload larger,
# monotonically with context count: flat -1.42%, two buckets -1.28%, six
# buckets -0.90%. Merging existing models or capping prefix positions lower did
# not help either, so the current context set is already a local optimum.
#
# A 1,420-character payload is context-starved rather than context-poor:
# splitting a model halves the data each half sees, and at this size the models
# never converge. Worth revisiting only if the envelope grows.


def write_point(w: BitWriter, state: PointState, p: Tuple[int, int]) -> None:
    """Write one canvas point as a prediction residual or an absolute pair."""
    x, y = int(p[0]), int(p[1])
    px, py = predict_point(state)
    use_delta = state.initialized and (
        1 + rice_signed_length(x - px) + rice_signed_length(y - py) <= 20)
    w.bit(use_delta, "pt.mode")
    if use_delta:
        w.rice_signed(x - px, 3, "pt.x")
        w.rice_signed(y - py, 3, "pt.y")
    else:
        w.bits_n(x, 10)
        w.bits_n(y, 9)
    update_point_state(state, x, y)


def read_point(r: BitReader, state: PointState) -> Tuple[int, int]:
    """Inverse of write_point, rebuilding the same prediction from history."""
    if r.bit("pt.mode"):
        if not state.initialized:
            raise MCIError("Delta point before absolute point.")
        px, py = predict_point(state)
        # Second-order prediction can reach roughly twice the canvas extent,
        # so the residual bound is wider than the canvas itself.
        x = px + r.rice_signed(3, 2048, "pt.x")
        y = py + r.rice_signed(3, 2048, "pt.y")
    else:
        x = r.bits_n(10)
        y = r.bits_n(9)
    if not (0 <= x < CANVAS_W and 0 <= y < CANVAS_H):
        raise MCIError(f"Point outside canvas: {x},{y}")
    update_point_state(state, x, y)
    return x, y

# ==========================================================================
# Style records
# ==========================================================================

def style_key(style:PaintStyle,palette:Sequence[str])->Tuple[Any,...]:
    def idx(c): return palette.index(quantize_palette_color(c)) if c else -1
    return idx(style.fill),idx(style.stroke),max(1,min(64,int(round(style.stroke_width)))) if style.stroke else 0,style.fill_rule


def write_style(w:BitWriter,style:PaintStyle,palette:Sequence[str]):
    fill=style.fill is not None; stroke=style.stroke is not None
    w.bit(fill, "st.fill"); w.bit(stroke, "st.stroke"); w.bit(style.fill_rule=="evenodd", "st.eo")
    width=max(1,(len(palette)-1).bit_length())
    if fill: w.bits_n(palette.index(quantize_palette_color(style.fill)),width)
    if stroke:
        w.bits_n(palette.index(quantize_palette_color(style.stroke)),width)
        w.ue(max(1,min(64,int(round(style.stroke_width))))-1)


def read_style(r:BitReader,palette:Sequence[str])->PaintStyle:
    has_fill=bool(r.bit("st.fill")); has_stroke=bool(r.bit("st.stroke")); even=bool(r.bit("st.eo"))
    width=max(1,(len(palette)-1).bit_length())
    fill=palette[r.bits_n(width)] if has_fill else None
    stroke=palette[r.bits_n(width)] if has_stroke else None
    sw=r.ue(63)+1 if has_stroke else 0
    return PaintStyle(fill,stroke,sw,"evenodd" if even else "nonzero")

# ==========================================================================
# Stateful field helpers
# ==========================================================================

def _ue_length(value: int) -> int:
    number = int(value) + 1
    return number.bit_length() * 2 - 1


def _state_params(state: PointState) -> Dict[str, int]:
    params = getattr(state, "params", None)
    if params is None:
        params = {}
        setattr(state, "params", params)
    return params


def _write_stateful_fixed(w: BitWriter, state: PointState, key: str, value: int, width: int) -> None:
    params = _state_params(state)
    same = params.get(key) == value
    w.bit(same, f"sf.{key}")
    if not same:
        w.bits_n(value, width)
        params[key] = value


def _read_stateful_fixed(r: BitReader, state: PointState, key: str, width: int) -> int:
    params = _state_params(state)
    if r.bit(f"sf.{key}"):
        if key not in params:
            raise MCIError(f"Primitive state {key!r} reused before initialization.")
        return params[key]
    value = r.bits_n(width)
    params[key] = value
    return value


def _write_stateful_ue(w: BitWriter, state: PointState, key: str, value: int) -> None:
    params = _state_params(state)
    same = params.get(key) == value
    w.bit(same, f"sf.{key}")
    if not same:
        w.ue(value)
        params[key] = value


def _read_stateful_ue(r: BitReader, state: PointState, key: str, max_value: int) -> int:
    params = _state_params(state)
    if r.bit(f"sf.{key}"):
        if key not in params:
            raise MCIError(f"Primitive state {key!r} reused before initialization.")
        return params[key]
    value = r.ue(max_value)
    params[key] = value
    return value

# ==========================================================================
# Canvas-space geometry records
# ==========================================================================

def write_geometry(w: BitWriter, cmd: VectorCommand, state: PointState, palette: Optional[Sequence[str]] = None) -> None:
    if cmd.opcode != OP_PRIMITIVE:
        g = cmd.geom
        if cmd.opcode == OP_RECT:
            write_point(w, state, (int(g["x"]), int(g["y"]))); w.ue(int(g["w"]) - 1); w.ue(int(g["h"]) - 1)
        elif cmd.opcode == OP_ELLIPSE:
            write_point(w, state, (int(g["cx"]), int(g["cy"]))); w.ue(int(g["rx"]) - 1); w.ue(int(g["ry"]) - 1)
        elif cmd.opcode == OP_LINE:
            write_point(w, state, tuple(g["p1"])); write_point(w, state, tuple(g["p2"]))
        elif cmd.opcode in {OP_POLYLINE, OP_POLYGON}:
            pts = [tuple(p) for p in g["points"]]; minimum = 2 if cmd.opcode == OP_POLYLINE else 3
            w.ue(len(pts) - minimum)
            for point in pts: write_point(w, state, point)
        elif cmd.opcode == OP_PATH:
            segments = g["segments"]; w.ue(len(segments) - 1)
            for segment in segments:
                w.bits_n(int(segment["op"]), 3, "seg")
                for point in segment.get("points", []): write_point(w, state, tuple(point))
        else:
            raise MCIError("Unknown geometry opcode.")
        return

    if palette is None:
        raise MCIError("Primitive encoding requires the palette.")
    g = cmd.geom
    kind = primitive_kind(cmd)
    _write_stateful_fixed(w, state, "primitive_kind", kind, 4)
    if kind == PRIM_DOUBLE_BOX:
        write_point(w, state, (int(g["x1"]), int(g["y1"])))
        write_point(w, state, (int(g["x2"]), int(g["y2"])))
        _write_stateful_ue(w, state, "percent", int(g.get("percent", 50)))
        return
    write_point(w, state, (int(g["x"]), int(g["y"])))
    if kind == PRIM_TEXT:
        text = clean_primitive_text(g.get("text", ""))
        w.ue(len(text))
        for char in text: w.bits_n(TEXT_INDEX[char], 6)
    elif kind in {PRIM_TRIANGLE_OUTLINE, PRIM_TRIANGLE_FILL, PRIM_ARROW, PRIM_YAGI, PRIM_DISH, PRIM_RADIO}:
        _write_stateful_fixed(w, state, f"orientation_{kind}", int(g.get("orientation", 0)) % 4, 2)
        _write_stateful_ue(w, state, f"scale_{kind}", int(g.get("scale", 1)) - 1)
    elif kind == PRIM_STAR:
        _write_stateful_ue(w, state, "star_radius", int(g.get("radius", 35)) - 1)
        _write_stateful_ue(w, state, "star_scale", int(g.get("scale", 1)) - 1)
    elif kind in {PRIM_ARC, PRIM_RADIO_WAVES}:
        prefix = "arc" if kind == PRIM_ARC else "waves"
        _write_stateful_ue(w, state, f"{prefix}_radius", int(g.get("radius", 35)) - 1)
        _write_stateful_ue(w, state, f"{prefix}_scale", int(g.get("scale", 1)) - 1)
        _write_stateful_fixed(w, state, f"{prefix}_start", int(g.get("start_angle", 0)), 9)
        _write_stateful_fixed(w, state, f"{prefix}_degrees", int(g.get("arc_degrees", 180)), 9)
    elif kind == PRIM_MOON:
        _write_stateful_ue(w, state, "moon_scale", int(g.get("scale", 1)) - 1)
        width = max(1, (len(palette) - 1).bit_length())
        crater = quantize_palette_color(str(g.get("crater_color", "#808080")))
        w.bits_n(palette.index(crater), width)
    else:
        raise MCIError(f"Unsupported primitive kind {kind}.")


def read_geometry(r: BitReader, opcode: int, state: PointState, palette: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    if opcode != OP_PRIMITIVE:
        if opcode == OP_RECT:
            x, y = read_point(r, state); return {"x": x, "y": y, "w": r.ue(719) + 1, "h": r.ue(479) + 1}
        if opcode == OP_ELLIPSE:
            cx, cy = read_point(r, state); return {"cx": cx, "cy": cy, "rx": r.ue(719) + 1, "ry": r.ue(479) + 1}
        if opcode == OP_LINE:
            return {"p1": read_point(r, state), "p2": read_point(r, state)}
        if opcode in {OP_POLYLINE, OP_POLYGON}:
            minimum = 2 if opcode == OP_POLYLINE else 3; count = r.ue(MAX_COMMANDS) + minimum
            return {"points": [read_point(r, state) for _ in range(count)]}
        if opcode == OP_PATH:
            count = r.ue(MAX_COMMANDS * 8) + 1; segments = []
            point_counts = {SEG_M: 1, SEG_L: 1, SEG_Q: 2, SEG_C: 3, SEG_Z: 0}
            for _ in range(count):
                operation = r.bits_n(3, "seg")
                if operation not in point_counts: raise MCIError("Invalid path segment opcode.")
                segments.append({"op": operation, "points": [read_point(r, state) for _ in range(point_counts[operation])]})
            return {"segments": segments}
        raise MCIError("Unknown opcode.")

    if palette is None:
        raise MCIError("Primitive decoding requires the palette.")
    kind = _read_stateful_fixed(r, state, "primitive_kind", 4)
    if kind not in PRIMITIVE_NAMES:
        raise MCIError(f"Invalid primitive kind {kind}.")
    g: Dict[str, Any] = {"kind": kind}
    if kind == PRIM_DOUBLE_BOX:
        g["x1"], g["y1"] = read_point(r, state)
        g["x2"], g["y2"] = read_point(r, state)
        g["percent"] = _read_stateful_ue(r, state, "percent", 100)
        return g
    g["x"], g["y"] = read_point(r, state)
    if kind == PRIM_TEXT:
        length = r.ue(MAX_TEXT_LEN)
        chars = []
        for _ in range(length):
            index = r.bits_n(6)
            if index >= len(TEXT_ALPHABET): raise MCIError("Invalid text symbol.")
            chars.append(TEXT_ALPHABET[index])
        g["text"] = "".join(chars)
    elif kind in {PRIM_TRIANGLE_OUTLINE, PRIM_TRIANGLE_FILL, PRIM_ARROW, PRIM_YAGI, PRIM_DISH, PRIM_RADIO}:
        g["orientation"] = _read_stateful_fixed(r, state, f"orientation_{kind}", 2)
        g["scale"] = _read_stateful_ue(r, state, f"scale_{kind}", 63) + 1
    elif kind == PRIM_STAR:
        g["radius"] = _read_stateful_ue(r, state, "star_radius", 127) + 1
        g["scale"] = _read_stateful_ue(r, state, "star_scale", 63) + 1
    elif kind in {PRIM_ARC, PRIM_RADIO_WAVES}:
        prefix = "arc" if kind == PRIM_ARC else "waves"
        g["radius"] = _read_stateful_ue(r, state, f"{prefix}_radius", 127) + 1
        g["scale"] = _read_stateful_ue(r, state, f"{prefix}_scale", 63) + 1
        g["start_angle"] = _read_stateful_fixed(r, state, f"{prefix}_start", 9)
        g["arc_degrees"] = _read_stateful_fixed(r, state, f"{prefix}_degrees", 9)
    elif kind == PRIM_MOON:
        g["scale"] = _read_stateful_ue(r, state, "moon_scale", 63) + 1
        width = max(1, (len(palette) - 1).bit_length())
        index = r.bits_n(width)
        if index >= len(palette): raise MCIError("Invalid moon crater palette index.")
        g["crater_color"] = palette[index]
    return g



# ==========================================================================
# Shape refitting
# ==========================================================================
#
# Traced artwork arrives as long runs of straight segments approximating a
# curve. Simplification removes redundant vertices but cannot change the
# representation, so a smooth outline still costs one point per retained
# vertex. Refitting replaces such a run with the shape it was approximating:
# a handful of cubic Beziers, or an ellipse or rectangle when the geometry
# really is one.
#
# Two acceptance rules, both required:
#   1. the candidate must encode smaller, measured with the real record writer
#   2. every original vertex must lie within REFIT_TOLERANCE of the candidate
#
# Rule 2 is checked against the *flattened* candidate, which is what a receiver
# will actually draw, so the budget is on visible geometry rather than on
# control points.

REFIT_TOLERANCE = 0.5
REFIT_MIN_POINTS = 8          # below this a refit cannot pay for itself








def _as_ellipse(points, tolerance) -> Optional[Dict[str, Any]]:
    """Recognise a traced circle or axis-aligned ellipse."""
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    cx, cy = (min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0
    rx, ry = (max(xs) - min(xs)) / 2.0, (max(ys) - min(ys)) / 2.0
    if rx < 2 or ry < 2:
        return None
    for x, y in points:
        # distance from the ellipse, scaled back into pixels
        r = math.hypot((x - cx) / rx, (y - cy) / ry)
        if abs(r - 1.0) * min(rx, ry) > tolerance:
            return None
    return {"cx": cx, "cy": cy, "rx": rx, "ry": ry}


def _as_rect(points, tolerance) -> Optional[Dict[str, Any]]:
    """Recognise a traced axis-aligned rectangle."""
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    if x1 - x0 < 2 or y1 - y0 < 2:
        return None
    for x, y in points:
        on_vertical = min(abs(x - x0), abs(x - x1)) <= tolerance
        on_horizontal = min(abs(y - y0), abs(y - y1)) <= tolerance
        if not (on_vertical or on_horizontal):
            return None
    # Reject a plain L or U: every edge of the box must actually be occupied.
    edges = [False, False, False, False]
    for x, y in points:
        if abs(x - x0) <= tolerance: edges[0] = True
        if abs(x - x1) <= tolerance: edges[1] = True
        if abs(y - y0) <= tolerance: edges[2] = True
        if abs(y - y1) <= tolerance: edges[3] = True
    if not all(edges):
        return None
    return {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}


def _densify_closed(candidate: VectorCommand, drawn) -> List[Tuple[float, float]]:
    """Return a closed outline dense enough to measure distance against.

    ``command_points`` gives a rect four corners and an ellipse its extremes,
    which is fine for bounding boxes but far too coarse for an error metric.
    """
    if candidate.opcode == OP_ELLIPSE:
        g = candidate.geom
        cx, cy = float(g["cx"]), float(g["cy"])
        rx, ry = float(g["rx"]), float(g["ry"])
        steps = 180
        return [(cx + rx * math.cos(2 * math.pi * i / steps),
                 cy + ry * math.sin(2 * math.pi * i / steps)) for i in range(steps + 1)]
    if candidate.opcode == OP_RECT:
        g = candidate.geom
        x, y, w, h = float(g["x"]), float(g["y"]), float(g["w"]), float(g["h"])
        return [(x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)]
    points = list(drawn)
    if points and points[0] != points[-1]:
        points.append(points[0])
    return points


def _outline_deviation(source_points, candidate: VectorCommand, limit: float = float("inf")) -> float:
    """Worst distance from an original vertex to the candidate's drawn outline.

    Closed candidates must have their final edge included. Leaving it out made
    every point near the closing edge look far from the shape, which silently
    rejected correct rectangle and ellipse matches.
    """
    try:
        drawn = command_points(candidate)
    except Exception:
        return float("inf")
    if candidate.opcode in (OP_RECT, OP_ELLIPSE, OP_POLYGON):
        drawn = _densify_closed(candidate, drawn)
    if len(drawn) < 2:
        return float("inf")
    worst = 0.0
    segments = range(len(drawn) - 1)
    for point in source_points:
        best = min(_perpendicular_distance(point, drawn[i], drawn[i + 1]) for i in segments)
        if best > worst:
            worst = best
            # Callers only ever ask "is this within budget", so stop as soon as
            # the answer is no. This is the hot loop of the tolerance search.
            if worst > limit:
                return worst
    return worst


def refit_candidates(command: VectorCommand,
                     tolerance: float = REFIT_TOLERANCE) -> List[VectorCommand]:
    """Alternative representations of a traced run, all within tolerance.

    Returns candidates only; the caller decides using encoded size. Ordering is
    cheapest-shape-first so a caller that stops early still does well.
    """
    if command.opcode not in (OP_POLYLINE, OP_POLYGON):
        return []
    points = [tuple(float(v) for v in p) for p in command.geom.get("points", [])]
    if len(points) < REFIT_MIN_POINTS:
        return []
    closed = command.opcode == OP_POLYGON
    out: List[VectorCommand] = []

    rect = _as_rect(points, tolerance) if closed else None
    if rect is not None:
        candidate = command.clone()
        candidate.opcode = OP_RECT
        candidate.geom = rect
        out.append(candidate)

    ellipse = _as_ellipse(points, tolerance) if closed else None
    if ellipse is not None:
        candidate = command.clone()
        candidate.opcode = OP_ELLIPSE
        candidate.geom = ellipse
        out.append(candidate)

    # Cubic Bezier refitting was implemented and measured here, and removed.
    # It lost on every content class: a cubic segment costs three points, while
    # Douglas-Peucker at the same tolerance retains fewer total vertices, and
    # the predictive point codec handles consecutive polyline vertices better
    # than it handles control points. Measured -0.1% payload for +26% encode
    # time across the corpus. Recognising exact shapes is kept because it
    # replaces the whole run with a handful of numbers rather than trading one
    # point representation for another.

    # Enforce the visible-geometry budget on every candidate before returning.
    return [c for c in out if _outline_deviation(points, c, tolerance) <= tolerance]



TOLERANCE_LADDER = (0.5, 0.75, 1.0, 1.5, 2.0, 3.0)


def _simplified_at(command: VectorCommand, tolerance: float) -> VectorCommand:
    return simplify_commands([command], tolerance)[0]


def simplify_rate_distortion(commands: Sequence[VectorCommand],
                             palette: Sequence[str]) -> Tuple[List[VectorCommand], int]:
    """Choose a simplification tolerance per object instead of globally.

    A single global tolerance has to be set for the most delicate object in the
    image, which wastes bits on every large smooth contour. This walks a
    ladder per object and keeps the coarsest setting that stays inside that
    object's own deviation budget, measured against its original outline.

    Primitives are never touched: their geometry is parametric, and text in
    particular has no vertices to thin.
    """
    out: List[VectorCommand] = []
    coarsened = 0
    for command in commands:
        if command.opcode not in (OP_POLYLINE, OP_POLYGON, OP_PATH):
            out.append(command)
            continue
        try:
            source_points = command_points(command)
        except Exception:
            out.append(command)
            continue
        if len(source_points) < REFIT_MIN_POINTS:
            out.append(_simplified_at(command, SIMPLIFY_TOLERANCE))
            continue

        budget = _deviation_budget(command, source_points)
        best = _simplified_at(command, SIMPLIFY_TOLERANCE)
        best_cost = _record_cost(best, palette)
        best_tolerance = SIMPLIFY_TOLERANCE
        for tolerance in TOLERANCE_LADDER:
            if tolerance <= SIMPLIFY_TOLERANCE:
                continue
            # Douglas-Peucker bounds deviation by its tolerance by definition,
            # and corner protection only ever keeps extra vertices, so the
            # budget test is a comparison rather than a measurement. Measuring
            # it cost more encode time than the whole search saved.
            if tolerance > budget:
                break
            candidate = _simplified_at(command, tolerance)
            cost = _record_cost(candidate, palette)
            if cost < best_cost:
                best, best_cost, best_tolerance = candidate, cost, tolerance
        if best_tolerance > SIMPLIFY_TOLERANCE:
            coarsened += 1
        out.append(best)
    return out, coarsened



# ==========================================================================
# Perceptual palette merging
# ==========================================================================
#
# Palette indices are fixed width: max(1, (count - 1).bit_length()). The width
# therefore drops only at powers of two, so a palette of 17 costs every command
# a fifth index bit that a palette of 16 does not, and the two extra entries
# cost 20 bits each on top.
#
# Merging is attempted only when the palette sits just above such a boundary,
# and only between colours close enough that the substitution is not visible.
# Distance is weighted RGB, which tracks perceived difference far better than
# plain Euclidean and is much cheaper than a full Lab conversion. Alpha is
# compared separately because a translucent colour must never collapse into an
# opaque one.

PALETTE_MERGE_DISTANCE = 40.0
PALETTE_MERGE_MAX_STEPS = 3


def _palette_distance(a: str, b: str) -> float:
    """Weighted RGB distance, with alpha treated as non-negotiable."""
    ar, ag, ab, aa = color_to_rgba(a)
    br, bg, bb, ba = color_to_rgba(b)
    if abs(aa - ba) > 8:
        return float("inf")
    rmean = (ar + br) / 2.0
    dr, dg, db = ar - br, ag - bg, ab - bb
    return math.sqrt((2 + rmean / 256.0) * dr * dr + 4 * dg * dg
                     + (2 + (255 - rmean) / 256.0) * db * db)


def _index_width(count: int) -> int:
    return max(1, (max(1, count) - 1).bit_length())


def merge_palette(commands: Sequence[VectorCommand],
                  palette: Sequence[str]) -> Tuple[List[VectorCommand], List[str], int]:
    """Collapse near-identical colours to cross an index-width boundary.

    Returns the recoloured commands, the new palette, and how many merges were
    applied. A merge is only made when it actually narrows the index field, so
    an image already at a power of two is left completely alone.
    """
    current = list(palette)
    if len(current) < 3:
        return list(commands), current, 0

    target = 1 << (_index_width(len(current)) - 1)
    if len(current) <= target or len(current) - target > PALETTE_MERGE_MAX_STEPS:
        return list(commands), current, 0

    # Rank candidate pairs by perceived difference and take the closest first.
    pairs = []
    for i in range(len(current)):
        for j in range(i + 1, len(current)):
            d = _palette_distance(current[i], current[j])
            if d <= PALETTE_MERGE_DISTANCE:
                pairs.append((d, i, j))
    if len(pairs) < len(current) - target:
        return list(commands), current, 0
    pairs.sort()

    mapping = {colour: colour for colour in current}
    removed: set = set()
    merges = 0
    for _d, i, j in pairs:
        if len(current) - len(removed) <= target:
            break
        a, b = current[i], current[j]
        if a in removed or b in removed:
            continue
        # Keep the earlier entry; it is the one more commands already reference.
        mapping[b] = mapping[a]
        removed.add(b)
        merges += 1

    if len(current) - len(removed) > target or merges == 0:
        return list(commands), current, 0

    out: List[VectorCommand] = []
    for command in commands:
        clone = command.clone()
        style = clone.style
        # Palette entries are already RGB565+A4 quantized, so look up the
        # quantized form of each colour rather than the source hex.
        if style.fill is not None:
            style.fill = mapping.get(quantize_palette_color(style.fill), style.fill)
        if style.stroke is not None:
            style.stroke = mapping.get(quantize_palette_color(style.stroke), style.stroke)
        if clone.opcode == OP_PRIMITIVE and "crater_color" in clone.geom:
            crater = quantize_palette_color(str(clone.geom["crater_color"]))
            clone.geom["crater_color"] = mapping.get(crater, crater)
        out.append(clone)
    return out, build_palette(out), merges




# ==========================================================================
# Style-run reordering: implemented, measured, removed
# ==========================================================================
#
# Real tracer output interleaves colours, so on the operator's test files the
# same-style shortcut never fires: 97 commands produced 97 style runs, and each
# re-sent its paint flags and palette index.
#
# A safe reorder was built for this. Draw order only matters between shapes
# that overlap, so the overlap graph was topologically sorted preferring the
# current style, keeping overlapping pairs in their original relative order.
# It worked: style runs fell 97->64 and 64->40, and the rendered raster was
# byte-identical.
#
# It still lost. Payload rose +0.1% to +0.4%, and adding a spatial-locality
# tie-break made it worse rather than better. The point codec predicts from the
# previous coordinate, so grouping by style scatters geometry and the extra
# coordinate residuals cost more than the style bits saved.
#
# Capturing this overhead needs a record that carries several sub-outlines
# under one style header without per-segment opcodes -- roughly 26 bits of
# header times the records eliminated, about 9% on these files. That is a
# protocol change, not a planning change.


# ==========================================================================
# Polygon start-point rotation
# ==========================================================================
#
# A closed polygon's starting vertex is arbitrary: the decoder walks the same
# loop whichever vertex it begins at. But the first point of a command is the
# one the point codec can predict least well, because prediction carries over
# from whatever was coded before it.
#
# Rotating each polygon to begin at the vertex nearest the previous coded
# position turns that expensive first delta into a cheap one. It is free and
# lossless, and needs no format change.
#
# Only OP_POLYGON is eligible. A polyline is an open path, so its endpoints
# are meaningful and rotating it would change the drawing.




# ==========================================================================
# Start-point rotation and trellis quantization: measured, removed
# ==========================================================================
#
# Both were implemented, verified correct, and removed on cost grounds.
#
# Start-point rotation re-phased closed subpaths to begin at the vertex
# nearest the previous coded point, turning an expensive first delta into a
# cheap one. Trellis quantization chose between floor and ceil per coordinate
# to minimise the coded residual, in the manner of JPEG and HEVC encoders,
# skipping anything involved in a repeat because two copies of one shape must
# round identically or they stop matching.
#
# Measured on the real corpus: rotation saved 11 characters at no measurable
# time cost, trellis saved 9 for +31ms, and together 17 for +80ms. Against a
# 3,400-character corpus that is not worth two extra geometry passes and the
# repeat-protection machinery trellis needs to be safe.
#
# The one change from that round that was kept is the entropy coder's
# adaptation rate, because a constant costs nothing to run: see _MOVE_BITS.

# ==========================================================================
# Global coordinate precision: implemented, measured, removed
# ==========================================================================
#
# Everything outside a local-space group is coded at full 720x480 precision,
# so coding on a coarser global grid looked like it should shrink every
# residual -- perhaps a bit per coordinate, and coordinates are most of the
# payload.
#
# It was built: a two-bit grid index in the stream header, geometry scaled
# before planning and scaled back after decoding, and a search across grids
# alongside the local-precision ladder.
#
# It gained nothing: 0.0% on both portraits and -0.3% on the cart, for grid
# errors up to 3px. The reason is that local-space SVG groups already
# re-normalise their contents into a private coordinate box at whatever extent
# the precision ladder picked, so a coarser global grid is normalised straight
# back out again. The two mechanisms do the same job, and the local one is
# already adaptive and lossless with respect to placement.
#
# It would only pay for content with no local groups at all, which the editor
# does not produce: importing an SVG assigns one.

# ==========================================================================
# Symmetry-aware repeats
# ==========================================================================
#
# A translated copy is only the simplest kind of repetition. Radio artwork is
# full of the others: arrows pointing four ways, mirrored antenna elements,
# symmetric logos, tiled patterns. Those cost a full geometry record each even
# though the shape was already sent.
#
# The eight symmetries of the square (four rotations by 90 degrees, and each of
# those mirrored) are exactly the transforms that map integer coordinates to
# integer coordinates with no rounding, so a match is exact rather than
# approximate. Anything else -- arbitrary rotation, scaling -- would need
# tolerance handling and is deliberately out of scope.
#
# Cost: one flag per repeat record saying "plain translation". With entropy
# coding that flag is strongly skewed and costs a fraction of a bit, so plain
# translations are barely affected while a mirrored copy saves a whole record.

SYM_IDENTITY = 0
SYM_ROT90 = 1
SYM_ROT180 = 2
SYM_ROT270 = 3
SYM_FLIP_X = 4
SYM_FLIP_Y = 5
SYM_TRANSPOSE = 6
SYM_ANTITRANSPOSE = 7

# (a, b, c, d) of the affine matrix, applied about the origin:
#   x' = a*x + c*y      y' = b*x + d*y
SYMMETRY_MATRICES: Dict[int, Tuple[float, float, float, float]] = {
    SYM_IDENTITY: (1, 0, 0, 1),
    SYM_ROT90: (0, 1, -1, 0),
    SYM_ROT180: (-1, 0, 0, -1),
    SYM_ROT270: (0, -1, 1, 0),
    SYM_FLIP_X: (-1, 0, 0, 1),
    SYM_FLIP_Y: (1, 0, 0, -1),
    SYM_TRANSPOSE: (0, 1, 1, 0),
    SYM_ANTITRANSPOSE: (0, -1, -1, 0),
}

SYMMETRY_NAMES = {
    SYM_IDENTITY: "identity", SYM_ROT90: "rotate 90", SYM_ROT180: "rotate 180",
    SYM_ROT270: "rotate 270", SYM_FLIP_X: "flip x", SYM_FLIP_Y: "flip y",
    SYM_TRANSPOSE: "transpose", SYM_ANTITRANSPOSE: "anti-transpose",
}


def apply_symmetry(command: VectorCommand, code: int) -> VectorCommand:
    """Apply one square symmetry about the origin.

    Rect and ellipse are handled directly rather than through the generic
    matrix path: every symmetry here maps an axis-aligned box to an axis-
    aligned box, so those opcodes survive intact instead of degrading into
    polygons. Keeping the opcode is what makes the repeat match at all.
    """
    if code == SYM_IDENTITY:
        return command.clone()
    a, b, c, d = SYMMETRY_MATRICES[code]
    matrix = (float(a), float(b), float(c), float(d), 0.0, 0.0)

    if command.opcode == OP_RECT:
        out = command.clone()
        corners = [apply_mat(matrix, p) for p in command_points(command)]
        xs = [p[0] for p in corners]
        ys = [p[1] for p in corners]
        out.geom = {"x": min(xs), "y": min(ys),
                    "w": max(xs) - min(xs), "h": max(ys) - min(ys)}
        return out
    if command.opcode == OP_ELLIPSE:
        out = command.clone()
        g = command.geom
        cx, cy = apply_mat(matrix, (float(g["cx"]), float(g["cy"])))
        swaps = a == 0  # the matrix exchanges the x and y axes
        rx, ry = float(g["rx"]), float(g["ry"])
        out.geom = {"cx": cx, "cy": cy,
                    "rx": ry if swaps else rx, "ry": rx if swaps else ry}
        return out
    return transform_command(command, matrix)


def geom_symmetry(prev: VectorCommand, cur: VectorCommand) -> Optional[Tuple[int, int, int]]:
    """Find a symmetry plus translation mapping ``prev`` exactly onto ``cur``.

    Returns ``(code, dx, dy)`` or ``None``. Identity is tried first so plain
    translations keep their existing, cheapest encoding.

    Compact primitives are excluded: their orientation lives in a geometry
    field rather than in coordinates, so rotating one is not representable and
    a false match would decode to the wrong shape.
    """
    if prev.opcode != cur.opcode or prev.opcode == OP_PRIMITIVE:
        plain = geom_translation(prev, cur) if prev.opcode == cur.opcode else None
        return (SYM_IDENTITY, plain[0], plain[1]) if plain else None
    for code in range(8):
        candidate = prev if code == SYM_IDENTITY else apply_symmetry(prev, code)
        found = geom_translation(candidate, cur)
        if found is not None:
            return code, found[0], found[1]
    return None


# ==========================================================================
# Encoder state, normal records, and translated repeats
# ==========================================================================

@dataclass
class EncoderState:
    point_states: Dict[int, PointState] = field(default_factory=lambda: {op: PointState() for op in OP_NAMES})
    style_states: Dict[int, Tuple[Any, ...]] = field(default_factory=dict)
    recent: Dict[int, VectorCommand] = field(default_factory=dict)
    previous_opcode: Optional[int] = None

    def clone(self) -> "EncoderState":
        return copy.deepcopy(self)


def _update_history(state: EncoderState, commands: Sequence[VectorCommand]) -> None:
    for command in commands:
        state.recent[command.opcode] = command.clone()
        state.previous_opcode = command.opcode


def _write_normal_record(w: BitWriter, state: EncoderState, cmd: VectorCommand, palette: Sequence[str]) -> None:
    w.bits_n(REC_NORMAL, 2, "rec")
    same_opcode = state.previous_opcode == cmd.opcode
    w.bit(same_opcode, "sameop")
    if not same_opcode:
        w.bits_n(cmd.opcode, 3, "op")
    key = style_key(cmd.style, palette)
    same_style = state.style_states.get(cmd.opcode) == key
    w.bit(same_style, "samestyle")
    if not same_style:
        write_style(w, cmd.style, palette)
        state.style_states[cmd.opcode] = key
    write_geometry(w, cmd, state.point_states[cmd.opcode], palette)
    _update_history(state, [cmd])


def _write_single_repeat_record(w: BitWriter, state: EncoderState, cmd: VectorCommand,
                                delta: Tuple[int, int], symmetry: int = SYM_IDENTITY) -> None:
    w.bits_n(REC_SINGLE_REPEAT, 2, "rec")
    w.bits_n(cmd.opcode, 3, "op")
    w.bit(symmetry != SYM_IDENTITY, "rep.sym")
    if symmetry != SYM_IDENTITY:
        w.bits_n(symmetry, 3, "rep.symcode")
    w.se(delta[0], "rep.dx")
    w.se(delta[1], "rep.dy")
    _update_history(state, [cmd])


def _best_single_or_normal(cmd: VectorCommand, state: EncoderState, palette: Sequence[str]) -> Tuple[BitWriter, EncoderState, bool]:
    normal_writer = BitWriter(); normal_state = state.clone()
    _write_normal_record(normal_writer, normal_state, cmd, palette)
    repeat_writer: Optional[BitWriter] = None
    repeat_state: Optional[EncoderState] = None
    reference = state.recent.get(cmd.opcode)
    match = geom_symmetry(reference, cmd) if reference is not None else None
    if match is not None:
        symmetry, dx, dy = match
        repeat_writer = BitWriter(); repeat_state = state.clone()
        _write_single_repeat_record(repeat_writer, repeat_state, cmd, (dx, dy), symmetry)
    if repeat_writer is not None and repeat_writer.cost < normal_writer.cost:
        assert repeat_state is not None
        return repeat_writer, repeat_state, True
    return normal_writer, normal_state, False


def _simulate_sequence(sequence: Sequence[VectorCommand], state: EncoderState, palette: Sequence[str]) -> Tuple[int, EncoderState, int]:
    current = state.clone(); bits = 0; repeats = 0
    for command in sequence:
        writer, current, used_repeat = _best_single_or_normal(command, current, palette)
        bits += writer.cost
        repeats += int(used_repeat)
    return bits, current, repeats


def _command_invariant_signature(cmd: VectorCommand) -> str:
    anchor = primitive_anchor(cmd) if cmd.opcode == OP_PRIMITIVE else (command_points(cmd)[0] if command_points(cmd) else (0, 0))
    shifted = quantize_command(translate_command(cmd, -int(round(anchor[0])), -int(round(anchor[1]))))
    return json.dumps({"op": shifted.opcode, "style": shifted.style.to_json(), "geom": shifted.geom}, sort_keys=True, separators=(",", ":"))


def _write_group_record(w: BitWriter, state: EncoderState, source_distance: int, length: int,
                        delta: Tuple[int, int], generated: Sequence[VectorCommand]) -> None:
    w.bits_n(REC_GROUP_REPEAT, 2, "rec")
    w.ue(length - 2)
    adjacent = source_distance == length
    w.bit(adjacent, "grp.adj")
    if not adjacent:
        # A nonoverlapping source is always at least `length` commands back.
        w.ue(source_distance - length)
    w.se(delta[0], "rep.dx")
    w.se(delta[1], "rep.dy")
    _update_history(state, generated)


def _best_group_repeat(commands: Sequence[VectorCommand], index: int, state: EncoderState,
                       palette: Sequence[str], signature_positions: Dict[str, List[int]]) -> Optional[Tuple[int, int, int, int, int]]:
    """Return source_start, length, dx, dy, saved_bits for the best prior group."""
    if index + 1 >= len(commands):
        return None
    signature = _command_invariant_signature(commands[index])
    candidates = signature_positions.get(signature, [])
    best: Optional[Tuple[int, int, int, int, int]] = None
    # Search recent and nonadjacent matching starts. Capping at 256 keeps a
    # pathological tiled drawing responsive while still being comprehensive
    # for realistic ten-message images.
    for source_start in reversed(candidates[-256:]):
        if source_start + 2 > index:
            continue
        first_delta = geom_translation(commands[source_start], commands[index])
        if first_delta is None:
            continue
        maximum = min(index - source_start, len(commands) - index)
        length = 0
        while length < maximum:
            delta = geom_translation(commands[source_start + length], commands[index + length])
            if delta != first_delta:
                break
            length += 1
        if length < 2:
            continue
        baseline_bits, _baseline_state, _ = _simulate_sequence(commands[index:index + length], state, palette)
        group_writer = BitWriter(); group_state = state.clone()
        _write_group_record(group_writer, group_state, index - source_start, length, first_delta, commands[index:index + length])
        saved = baseline_bits - group_writer.cost
        if saved > 0 and (best is None or saved > best[4] or (saved == best[4] and length > best[1])):
            best = (source_start, length, first_delta[0], first_delta[1], saved)
    return best

# ==========================================================================
# Local-space SVG groups
# ==========================================================================

@dataclass
class TransformGroupPlan:
    """Encoder plan for one contiguous imported-SVG command run.

    ``start:end`` indexes the transport-planned display command list.
    ``local_commands`` are normalized and scale-independent.
    ``display_commands`` are the quantized reconstruction used for history and
    preview parity. ``signature`` identifies reusable local definitions and
    deliberately excludes x/y/display width/display height.
    """

    start: int
    end: int
    local_commands: List[VectorCommand]
    display_commands: List[VectorCommand]
    x: int
    y: int
    width: int
    height: int
    local_width: int
    local_height: int
    signature: str


@dataclass
class DecoderState:
    """Mutable decode/encode history shared by normal and repeat records.

    Point and style state are opcode-local. ``recent`` stores the latest fully
    reconstructed command for each opcode. ``previous_opcode`` supports the
    one-bit same-opcode shortcut used by normal records.
    """

    point_states: Dict[int, PointState] = field(default_factory=lambda: {op: PointState() for op in OP_NAMES})
    style_states: Dict[int, PaintStyle] = field(default_factory=dict)
    recent: Dict[int, VectorCommand] = field(default_factory=dict)
    previous_opcode: Optional[int] = None


def _all_transport_points(command: VectorCommand) -> List[Tuple[float, float]]:
    """Return every coordinate that the bitstream must carry, including Bézier controls."""
    g = command.geom
    if command.opcode == OP_PATH:
        return [tuple(point) for segment in g.get("segments", []) for point in segment.get("points", [])]
    if command.opcode == OP_RECT:
        x, y, width, height = float(g["x"]), float(g["y"]), float(g["w"]), float(g["h"])
        return [(x, y), (x + width, y + height)]
    if command.opcode == OP_ELLIPSE:
        cx, cy, rx, ry = float(g["cx"]), float(g["cy"]), float(g["rx"]), float(g["ry"])
        return [(cx - rx, cy - ry), (cx + rx, cy + ry)]
    return [tuple(point) for point in command_points(command)]


def _geometry_bbox(commands: Sequence[VectorCommand]) -> Tuple[float, float, float, float]:
    """Bounds for normalization, including path control points.

    This differs from visual bounds. Control points must be included because
    they are encoded and must remain inside the advertised local coordinate
    box even when a Bézier curve never reaches the control point itself.
    """
    points = [point for command in commands for point in _all_transport_points(command)]
    if not points:
        return 0.0, 0.0, 0.0, 0.0
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def _group_transport_signature(commands: Sequence[VectorCommand], local_width: int, local_height: int) -> str:
    """Create a deterministic identity for reusable local geometry.

    Placement is intentionally absent. Two copies at different positions or
    display sizes should share one definition when their normalized commands,
    styles, and local dimensions match.
    """
    payload = {
        "w": int(local_width),
        "h": int(local_height),
        "commands": [
            {
                "opcode": command.opcode,
                "style": command.style.normalized().to_json(),
                "geom": command.geom,
            }
            for command in commands
        ],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _make_transform_group(
    commands: Sequence[VectorCommand], start: int, end: int, local_extent: int,
) -> Optional[TransformGroupPlan]:
    """Normalize one eligible contiguous command run into local coordinates.

    ``local_extent`` is the normalized coordinate range to quantize into; it
    is supplied by the caller rather than read from module state, so the
    precision ladder in :func:`encode_image` cannot leak between calls.

    Returns ``None`` for runs that are too short, contain compact primitives,
    or have degenerate width/height. The caller may then encode those commands
    using normal/repeat records. This function performs lossy integer
    quantization at the selected local extent; preview parity depends on using
    ``display_commands`` reconstructed from the same quantized definition.
    """
    group = [command.clone() for command in commands[start:end]]
    if len(group) < 2 or any(command.opcode == OP_PRIMITIVE for command in group):
        return None
    x1, y1, x2, y2 = _geometry_bbox(group)
    raw_width = x2 - x1
    raw_height = y2 - y1
    if raw_width <= 1e-6 or raw_height <= 1e-6:
        return None
    maximum = max(raw_width, raw_height)
    local_width = max(1, min(local_extent, int(round(raw_width / maximum * local_extent))))
    local_height = max(1, min(local_extent, int(round(raw_height / maximum * local_extent))))
    normalize = mat_mul(
        mat_scale(local_width / raw_width, local_height / raw_height),
        mat_translate(-x1, -y1),
    )
    local_commands: List[VectorCommand] = []
    for command in group:
        local = quantize_command(transform_command(command, normalize))
        local.editor_group = None
        local_commands.append(local)
    # Integer quantization can push an endpoint one unit beyond the nominal
    # normalized extent (for example, rounded x plus rounded width). Advertise
    # the actual local coordinate envelope so the nested point codec and the
    # inverse transform agree exactly.
    local_points = [point for command in local_commands for point in _all_transport_points(command)]
    if local_points:
        local_width = max(local_width, min(256, int(math.ceil(max(point[0] for point in local_points)))))
        local_height = max(local_height, min(256, int(math.ceil(max(point[1] for point in local_points)))))

    # The placement fields are fixed-width, so changing them does not change
    # the transmission length. The local definition above remains stable as
    # the SVG is resized.
    x = max(0, min(CANVAS_W - 1, int(round(x1))))
    y = max(0, min(CANVAS_H - 1, int(round(y1))))
    width = max(1, min(CANVAS_W, int(round(raw_width))))
    height = max(1, min(CANVAS_H, int(round(raw_height))))
    if x + width > CANVAS_W:
        width = CANVAS_W - x
    if y + height > CANVAS_H:
        height = CANVAS_H - y

    display_commands = _apply_transform_group(
        local_commands, x, y, width, height, local_width, local_height,
    )
    signature = _group_transport_signature(local_commands, local_width, local_height)
    return TransformGroupPlan(
        start, end, local_commands, display_commands,
        x, y, width, height, local_width, local_height, signature,
    )


def _apply_transform_group(
    local_commands: Sequence[VectorCommand],
    x: int,
    y: int,
    width: int,
    height: int,
    local_width: int,
    local_height: int,
) -> List[VectorCommand]:
    matrix = mat_mul(
        mat_translate(x, y),
        mat_scale(width / max(1, local_width), height / max(1, local_height)),
    )
    output: List[VectorCommand] = []
    for command in local_commands:
        restored = quantize_command(transform_command(command, matrix))
        restored.editor_group = None
        output.append(restored)
    return output


def _write_transform_box(w: BitWriter, plan: TransformGroupPlan) -> None:
    """Write fixed-width display and local-box dimensions.

    Fixed width is intentional: moving or resizing a group changes values but
    not field length. Canvas dimensions explain the 10/9-bit x/y and width/
    height fields; local dimensions fit in eight bits.
    """
    w.bits_n(plan.x, 10)
    w.bits_n(plan.y, 9)
    w.bits_n(plan.width - 1, 10)
    w.bits_n(plan.height - 1, 9)
    w.bits_n(plan.local_width - 1, 8)
    w.bits_n(plan.local_height - 1, 8)


def _read_transform_box(r: BitReader) -> Tuple[int, int, int, int, int, int]:
    """Read and validate a transform box before allocating/expanding geometry."""
    x = r.bits_n(10)
    y = r.bits_n(9)
    width = r.bits_n(10) + 1
    height = r.bits_n(9) + 1
    local_width = r.bits_n(8) + 1
    local_height = r.bits_n(8) + 1
    if x >= CANVAS_W or y >= CANVAS_H or x + width > CANVAS_W or y + height > CANVAS_H:
        raise MCIError("Transformed SVG group lies outside the canvas.")
    return x, y, width, height, local_width, local_height


def _local_bits(maximum: int) -> int:
    return max(1, int(maximum).bit_length())


def _write_local_point(
    w: BitWriter, state: PointState, point: Tuple[int, int], local_width: int, local_height: int,
) -> None:
    """Write one local point using the cheaper absolute or Rice-delta form.

    Unlike canvas points, the absolute bit width derives from the group's local
    dimensions. Delta state remains opcode-local through the nested decoder
    state used for the definition.
    """
    x, y = int(point[0]), int(point[1])
    x_bits = _local_bits(local_width)
    y_bits = _local_bits(local_height)
    absolute_bits = 1 + x_bits + y_bits
    px, py = predict_point(state)
    delta_bits = (1 + rice_signed_length(x - px, 3) + rice_signed_length(y - py, 3)
                  if state.initialized else 10 ** 9)
    use_delta = state.initialized and delta_bits <= absolute_bits
    w.bit(use_delta, "lpt.mode")
    if use_delta:
        w.rice_signed(x - px, 3, "lpt.x")
        w.rice_signed(y - py, 3, "lpt.y")
    else:
        w.bits_n(x, x_bits)
        w.bits_n(y, y_bits)
    update_point_state(state, x, y)


def _read_local_point(
    r: BitReader, state: PointState, local_width: int, local_height: int,
) -> Tuple[int, int]:
    """Inverse of _write_local_point with strict local-box validation."""
    if r.bit("lpt.mode"):
        if not state.initialized:
            raise MCIError("Local delta point before initialization.")
        px, py = predict_point(state)
        x = px + r.rice_signed(3, 1024, "lpt.x")
        y = py + r.rice_signed(3, 1024, "lpt.y")
    else:
        x = r.bits_n(_local_bits(local_width))
        y = r.bits_n(_local_bits(local_height))
    if not (0 <= x <= local_width and 0 <= y <= local_height):
        raise MCIError(f"Point outside local SVG group: {x},{y}.")
    update_point_state(state, x, y)
    return x, y


def _write_local_geometry(
    w: BitWriter, command: VectorCommand, state: PointState, local_width: int, local_height: int,
) -> None:
    """Write generic vector geometry inside a local SVG definition.

    Compact primitives are excluded before this function. Keeping local
    definitions generic makes their signatures deterministic and lets the same
    geometry be instantiated at several display sizes.
    """
    g = command.geom
    point = lambda value: _write_local_point(w, state, tuple(value), local_width, local_height)
    if command.opcode == OP_RECT:
        point((int(g["x"]), int(g["y"])))
        w.ue(int(g["w"]) - 1); w.ue(int(g["h"]) - 1)
    elif command.opcode == OP_ELLIPSE:
        point((int(g["cx"]), int(g["cy"])))
        w.ue(int(g["rx"]) - 1); w.ue(int(g["ry"]) - 1)
    elif command.opcode == OP_LINE:
        point(g["p1"]); point(g["p2"])
    elif command.opcode in {OP_POLYLINE, OP_POLYGON}:
        points = [tuple(value) for value in g["points"]]
        minimum = 2 if command.opcode == OP_POLYLINE else 3
        w.ue(len(points) - minimum)
        for value in points:
            point(value)
    elif command.opcode == OP_PATH:
        segments = g["segments"]
        w.ue(len(segments) - 1)
        for segment in segments:
            w.bits_n(int(segment["op"]), 3, "seg")
            for value in segment.get("points", []):
                point(value)
    else:
        raise MCIError("Local SVG groups support generic vector commands only.")


def _read_local_geometry(
    r: BitReader, opcode: int, state: PointState, local_width: int, local_height: int,
) -> Dict[str, Any]:
    point = lambda: _read_local_point(r, state, local_width, local_height)
    if opcode == OP_RECT:
        x, y = point(); return {"x": x, "y": y, "w": r.ue(1023) + 1, "h": r.ue(1023) + 1}
    if opcode == OP_ELLIPSE:
        cx, cy = point(); return {"cx": cx, "cy": cy, "rx": r.ue(1023) + 1, "ry": r.ue(1023) + 1}
    if opcode == OP_LINE:
        return {"p1": point(), "p2": point()}
    if opcode in {OP_POLYLINE, OP_POLYGON}:
        minimum = 2 if opcode == OP_POLYLINE else 3
        count = r.ue(MAX_COMMANDS) + minimum
        return {"points": [point() for _ in range(count)]}
    if opcode == OP_PATH:
        count = r.ue(MAX_COMMANDS * 8) + 1
        point_counts = {SEG_M: 1, SEG_L: 1, SEG_Q: 2, SEG_C: 3, SEG_Z: 0}
        segments = []
        for _ in range(count):
            operation = r.bits_n(3, "seg")
            if operation not in point_counts:
                raise MCIError("Invalid local path segment opcode.")
            segments.append({"op": operation, "points": [point() for _ in range(point_counts[operation])]})
        return {"segments": segments}
    raise MCIError("Invalid local SVG opcode.")


def _write_local_normal_record(
    w: BitWriter,
    state: EncoderState,
    command: VectorCommand,
    palette: Sequence[str],
    local_width: int,
    local_height: int,
) -> None:
    w.bits_n(REC_NORMAL, 2, "rec")
    same_opcode = state.previous_opcode == command.opcode
    w.bit(same_opcode, "sameop")
    if not same_opcode:
        w.bits_n(command.opcode, 3, "op")
    key = style_key(command.style, palette)
    same_style = state.style_states.get(command.opcode) == key
    w.bit(same_style, "samestyle")
    if not same_style:
        write_style(w, command.style, palette)
        state.style_states[command.opcode] = key
    _write_local_geometry(
        w, command, state.point_states[command.opcode], local_width, local_height,
    )
    _update_history(state, [command])


def _best_local_record(
    command: VectorCommand,
    state: EncoderState,
    palette: Sequence[str],
    local_width: int,
    local_height: int,
) -> Tuple[BitWriter, EncoderState]:
    normal = BitWriter(); normal_state = state.clone()
    _write_local_normal_record(
        normal, normal_state, command, palette, local_width, local_height,
    )
    reference = state.recent.get(command.opcode)
    delta = geom_translation(reference, command) if reference is not None else None
    if delta is None:
        return normal, normal_state
    repeated = BitWriter(); repeated_state = state.clone()
    _write_single_repeat_record(repeated, repeated_state, command, delta)
    if repeated.cost < normal.cost:
        return repeated, repeated_state
    return normal, normal_state


def _write_local_definition(
    w: BitWriter,
    commands: Sequence[VectorCommand],
    palette: Sequence[str],
    local_width: int,
    local_height: int,
) -> None:
    state = EncoderState()
    for command in commands:
        record, state = _best_local_record(
            command, state, palette, local_width, local_height,
        )
        w.extend(record)


def _decode_regular_record(r: BitReader, palette: Sequence[str], state: DecoderState, local_width: int, local_height: int) -> VectorCommand:
    record_type = r.bits_n(2, "rec")
    if record_type == REC_NORMAL:
        same = bool(r.bit("sameop"))
        if same:
            if state.previous_opcode is None:
                raise MCIError("Same opcode before initialization.")
            opcode = state.previous_opcode
        else:
            opcode = r.bits_n(3, "op")
        if opcode not in OP_NAMES:
            raise MCIError("Invalid opcode.")
        same_style = bool(r.bit("samestyle"))
        if same_style:
            if opcode not in state.style_states:
                raise MCIError("Style reuse before initialization.")
            style = copy.deepcopy(state.style_states[opcode])
        else:
            style = read_style(r, palette)
            state.style_states[opcode] = copy.deepcopy(style)
        command = VectorCommand(opcode, style, _read_local_geometry(r, opcode, state.point_states[opcode], local_width, local_height))
        validate_command(command)
    elif record_type == REC_SINGLE_REPEAT:
        opcode = r.bits_n(3, "op")
        if opcode not in state.recent:
            raise MCIError("Repeat references missing opcode history.")
        symmetry = r.bits_n(3, "rep.symcode") if r.bit("rep.sym") else SYM_IDENTITY
        source = apply_symmetry(state.recent[opcode], symmetry)
        command = quantize_command(
            translate_command(source, r.se(2048, "rep.dx"), r.se(2048, "rep.dy"))
        )
    else:
        raise MCIError("A local SVG definition may contain only normal or single-repeat records.")
    state.recent[command.opcode] = command.clone()
    state.previous_opcode = command.opcode
    return command


def _plan_primitive_representations(
    commands: Sequence[VectorCommand], palette: Sequence[str],
) -> Tuple[List[VectorCommand], int, int]:
    """Keep generic SVG geometry unquantized until local-space normalization."""
    planned: List[VectorCommand] = []
    comparison_state = EncoderState()
    primitive_selected = 0
    vectorized_selected = 0
    for source in commands:
        quantized = quantize_command(source)
        if source.opcode != OP_PRIMITIVE:
            planned.append(source.clone())
            _bits, comparison_state, _repeats = _simulate_sequence([quantized], comparison_state, palette)
            continue
        expansion = primitive_to_vectors(quantized)
        if not expansion:
            planned.append(quantized)
            _bits, comparison_state, _repeats = _simulate_sequence([quantized], comparison_state, palette)
            primitive_selected += 1
            continue
        vectors = [quantize_command(item) for item in expansion]
        primitive_bits, primitive_state, _ = _simulate_sequence([quantized], comparison_state, palette)
        vector_bits, vector_state, _ = _simulate_sequence(vectors, comparison_state, palette)
        if primitive_bits < vector_bits:
            planned.append(quantized)
            comparison_state = primitive_state
            primitive_selected += 1
        else:
            for vector in vectors:
                vector.editor_group = None
            planned.extend(vectors)
            comparison_state = vector_state
            vectorized_selected += 1
    return planned, primitive_selected, vectorized_selected

# ==========================================================================
# Codec statistics
# ==========================================================================

@dataclass
class CodecStats:
    command_count: int
    palette_count: int
    bit_count: int
    packed_bytes: int
    base91_chars: int
    frame_count: int
    repeat_count: int
    group_repeat_count: int = 0
    primitive_count: int = 0
    vectorized_primitive_count: int = 0
    source_command_count: int = 0
    transformed_group_count: int = 0
    transformed_group_reference_count: int = 0
    local_group_extent: int = 0
    coding_mode: int = 1
    parity_frame_count: int = 0

    @property
    def fits(self) -> bool:
        # Parity frames are deliberately outside the ten-message data budget.
        return self.frame_count <= MAX_MESSAGES

    @property
    def message_count(self) -> int:
        """Messages actually transmitted: data frames plus parity frames."""
        return self.frame_count + self.parity_frame_count


@dataclass
class EncodedImage:
    raw: bytes
    payload: str
    frames: List[str]
    stats: CodecStats
    image_id: str
    palette: List[str]
    parity_frames: List[str] = field(default_factory=list)

    @property
    def transmit_frames(self) -> List[str]:
        """Everything to send, in order: the data frames, then P, then Q."""
        return list(self.frames) + list(self.parity_frames)


# ==========================================================================
# Geometry simplification
# ==========================================================================
#
# Transport coordinates are integers, so any vertex that survives rounding onto
# the same pixel as its neighbours, or that sits within half a pixel of the
# line between them, cannot change the decoded image. Removing those vertices
# is therefore *lossless at transport precision* and needs no quality
# trade-off and no search.
#
# This matters because coordinates dominate the payload. Real SVG artwork —
# traced logos, map exports, illustrator output — routinely carries far more
# vertices than a 720x480 canvas can resolve.
#
# This is an encoder-side optimization. The decoder is unaffected, so raising
# SIMPLIFY_TOLERANCE is not a protocol change; it only trades fidelity for
# size, and above 0.5 it stops being lossless.

SIMPLIFY_TOLERANCE = 0.5


def _perpendicular_distance(point, start, end) -> float:
    (px, py), (ax, ay), (bx, by) = point, start, end
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _simplify_run(points: Sequence[Tuple[float, float]], tolerance: float) -> List[Tuple[float, float]]:
    """Douglas-Peucker, iterative so deep paths cannot exhaust the stack."""
    if len(points) < 3:
        return list(points)
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        first, last = stack.pop()
        if last <= first + 1:
            continue
        worst, worst_index = tolerance, -1
        for i in range(first + 1, last):
            d = _perpendicular_distance(points[i], points[first], points[last])
            if d > worst:
                worst, worst_index = d, i
        if worst_index >= 0:
            keep[worst_index] = True
            stack.append((first, worst_index))
            stack.append((worst_index, last))
    return [p for p, k in zip(points, keep) if k]


def _drop_repeated(points: Sequence[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """Remove vertices that quantize onto the same integer pixel as the last."""
    out: List[Tuple[float, float]] = []
    for p in points:
        if out and (round(p[0]), round(p[1])) == (round(out[-1][0]), round(out[-1][1])):
            continue
        out.append(tuple(p))
    return out


CORNER_ANGLE_DEG = 38.0


def _corner_indices(points, threshold_deg: float = CORNER_ANGLE_DEG) -> List[int]:
    """Indices where the outline turns sharply enough to be a real feature.

    Douglas-Peucker measures distance from a chord, so a sharp corner sitting
    close to that chord can be discarded even though removing it is exactly
    what makes simplification look wrong. These indices are pinned.
    """
    keep: List[int] = []
    for i in range(1, len(points) - 1):
        ax, ay = points[i][0] - points[i - 1][0], points[i][1] - points[i - 1][1]
        bx, by = points[i + 1][0] - points[i][0], points[i + 1][1] - points[i][1]
        na, nb = math.hypot(ax, ay), math.hypot(bx, by)
        if na < 1e-9 or nb < 1e-9:
            continue
        cosine = max(-1.0, min(1.0, (ax * bx + ay * by) / (na * nb)))
        if math.degrees(math.acos(cosine)) >= threshold_deg:
            keep.append(i)
    return keep


def _simplify_protected(points, tolerance: float) -> List[Tuple[float, float]]:
    """Douglas-Peucker applied between pinned corners rather than across them."""
    corners = _corner_indices(points)
    if not corners:
        return _simplify_run(points, tolerance)
    out: List[Tuple[float, float]] = []
    bounds = [0] + corners + [len(points) - 1]
    for a, b in zip(bounds, bounds[1:]):
        piece = _simplify_run(points[a:b + 1], tolerance)
        out.extend(piece[:-1])
    out.append(points[-1])
    return out


def _feature_size(points) -> float:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return math.hypot(max(xs) - min(xs), max(ys) - min(ys))


def _deviation_budget(command: VectorCommand, points) -> float:
    """How far this object may move, in pixels.

    Error that is invisible on a large smooth contour is obvious on a small
    one, so the budget scales with feature size. Small objects, hairline
    strokes and anything with a fill-less thin profile stay at the strict
    half-pixel floor.
    """
    size = _feature_size(points)
    if size < 24.0:
        return 0.5
    if command.style.fill is None and command.style.stroke_width <= 2.0 and size < 120.0:
        return 0.5
    return max(0.5, min(2.0, size * 0.004))


def _simplify_points(points, minimum: int, tolerance: float) -> List[Tuple[float, float]]:
    reduced = _simplify_protected(_drop_repeated(points), tolerance)
    if len(reduced) < minimum:
        # Never violate the opcode's structural minimum; fall back to the
        # deduplicated form, then to the original.
        reduced = _drop_repeated(points)
    if len(reduced) < minimum:
        reduced = [tuple(p) for p in points]
    return reduced


def _simplify_path_segments(segments, tolerance: float):
    """Simplify runs of straight segments while leaving curves untouched.

    Bezier control points are structural: dropping one changes the curve, not
    just its sampling. Only maximal runs of LineTo are reduced, and the run's
    anchor point is included so the fit is measured against the real geometry.
    """
    out: List[Dict[str, Any]] = []
    index = 0
    anchor: Optional[Tuple[float, float]] = None
    while index < len(segments):
        segment = segments[index]
        op = int(segment.get("op", -1))
        if op != SEG_L:
            out.append(segment)
            pts = segment.get("points", [])
            if pts:
                anchor = tuple(pts[-1])
            index += 1
            continue
        run = []
        while index < len(segments) and int(segments[index].get("op", -1)) == SEG_L:
            run.append(tuple(segments[index]["points"][0]))
            index += 1
        if anchor is None or len(run) < 2:
            out.extend({"op": SEG_L, "points": [p]} for p in run)
        else:
            reduced = _simplify_run(_drop_repeated([anchor] + run), tolerance)[1:]
            if not reduced:
                reduced = [run[-1]]
            out.extend({"op": SEG_L, "points": [p]} for p in reduced)
        if run:
            anchor = run[-1]
    return out


def simplify_commands(commands: Sequence[VectorCommand],
                      tolerance: float = SIMPLIFY_TOLERANCE) -> List[VectorCommand]:
    """Return commands with transport-invisible vertices removed.

    Structural minimums are always honoured: a polygon keeps three points and a
    polyline two, so simplification can never produce geometry the validator
    would reject.
    """
    out: List[VectorCommand] = []
    for command in commands:
        if command.opcode == OP_POLYLINE or command.opcode == OP_POLYGON:
            minimum = 2 if command.opcode == OP_POLYLINE else 3
            points = [tuple(p) for p in command.geom.get("points", [])]
            reduced = _simplify_points(points, minimum, tolerance)
            if len(reduced) < len(points):
                command = command.clone()
                command.geom = dict(command.geom)
                command.geom["points"] = reduced
        elif command.opcode == OP_PATH:
            segments = command.geom.get("segments", [])
            reduced = _simplify_path_segments(segments, tolerance)
            if len(reduced) < len(segments) and reduced and int(reduced[0].get("op", -1)) == SEG_M:
                command = command.clone()
                command.geom = dict(command.geom)
                command.geom["segments"] = reduced
        out.append(command)
    return out


# ==========================================================================
# Stream codec
# ==========================================================================

def _record_cost(command: VectorCommand, palette: Sequence[str]) -> int:
    """Cost of writing one command as a normal record, in pre-entropy bits.

    A fresh encoder state is used, so same-opcode and same-style shortcuts are
    unavailable to every candidate equally. That handicaps the absolute number
    but keeps the comparison between representations fair, which is all the
    caller needs.
    """
    try:
        writer = BitWriter()
        _write_normal_record(writer, EncoderState(), quantize_command(command), palette)
        return writer.cost
    except Exception:
        return 1 << 30


def refit_commands(commands: Sequence[VectorCommand], palette: Sequence[str],
                   tolerance: float = REFIT_TOLERANCE) -> Tuple[List[VectorCommand], int]:
    """Replace traced runs with cheaper equivalent shapes.

    Accepts a candidate only when it encodes strictly smaller. ``refit_candidates``
    has already enforced the geometric budget, so this stage is purely about
    size and can never trade accuracy for bits.
    """
    out: List[VectorCommand] = []
    refits = 0
    for command in commands:
        candidates = refit_candidates(command, tolerance)
        if not candidates:
            out.append(command)
            continue
        best = command
        best_cost = _record_cost(command, palette)
        for candidate in candidates:
            cost = _record_cost(candidate, palette)
            if cost < best_cost:
                best, best_cost = candidate, cost
        if best is not command:
            refits += 1
        out.append(best)
    return out, refits


def encode_commands(
    commands: Sequence[VectorCommand],
    local_extent: int = LOCAL_GROUP_EXTENT,
    preprocessed: bool = False,
) -> Tuple[bytes, int, Dict[str, int], List[str]]:
    """Compress commands into the protocol-v6 bitstream.

    ``local_extent`` selects the normalized coordinate range used for
    local-space SVG groups. Higher values keep more shape fidelity and cost
    more bits; :func:`encode_image` searches this space automatically.

    Returns ``(payload_bytes, bit_count, metrics, palette)``.
    """
    source = [command.clone() for command in commands]
    palette = build_palette(source)
    if preprocessed:
        # encode_image already ran geometry selection; repeating it once per
        # precision level tripled encode time for identical output.
        raw_source, coarsened, refit_count = source, 0, 0
    else:
        raw_source, coarsened = simplify_rate_distortion(source, palette)
        raw_source, refit_count = refit_commands(raw_source, palette)
    planned, primitive_count, vectorized_count = _plan_primitive_representations(raw_source, palette)
    if len(planned) > MAX_COMMANDS:
        raise MCIError(f"Optimized image expands to {len(planned)} commands; maximum is {MAX_COMMANDS}.")

    display_commands = [quantize_command(command) for command in planned]
    transform_plans: Dict[int, TransformGroupPlan] = {}
    transform_covered: set[int] = set()
    index = 0
    while index < len(planned):
        group_id = planned[index].editor_group
        if group_id is None or planned[index].opcode == OP_PRIMITIVE:
            index += 1
            continue
        end = index + 1
        while end < len(planned) and planned[end].editor_group == group_id and planned[end].opcode != OP_PRIMITIVE:
            end += 1
        plan = _make_transform_group(planned, index, end, local_extent)
        if plan is not None:
            transform_plans[index] = plan
            transform_covered.update(range(index, end))
            display_commands[index:end] = plan.display_commands
        index = end

    w = BitWriter()
    w.bits_n(PROTOCOL_VERSION, 4)
    w.ue(len(palette) - 1)
    for color in palette:
        w.bits_n(rgb565(color), 16)
        w.bits_n(alpha4(color), 4)
    w.ue(len(display_commands))

    state = EncoderState()
    signatures: Dict[str, List[int]] = {}
    for position, command in enumerate(display_commands):
        signatures.setdefault(_command_invariant_signature(command), []).append(position)

    definition_indices: Dict[str, int] = {}
    definitions: List[TransformGroupPlan] = []
    single_repeats = 0
    translated_group_repeats = 0
    transformed_groups = 0
    transformed_references = 0
    copied_commands = 0
    index = 0
    while index < len(display_commands):
        plan = transform_plans.get(index)
        if plan is not None:
            w.bits_n(REC_TRANSFORM_GROUP, 2, "rec")
            definition_index = definition_indices.get(plan.signature)
            is_reference = definition_index is not None
            w.bit(is_reference, "grp.ref")
            _write_transform_box(w, plan)
            if is_reference:
                assert definition_index is not None
                w.ue(definition_index)
                transformed_references += 1
                copied_commands += len(plan.local_commands)
            else:
                w.ue(len(plan.local_commands) - 2)
                _write_local_definition(w, plan.local_commands, palette, plan.local_width, plan.local_height)
                definition_indices[plan.signature] = len(definitions)
                definitions.append(plan)
            transformed_groups += 1
            _update_history(state, plan.display_commands)
            index = plan.end
            continue

        group = _best_group_repeat(display_commands, index, state, palette, signatures)
        if group is not None:
            source_start, length, dx, dy, _saved = group
            # Do not swallow a local-space target group. Those groups must keep
            # their scale-independent representation.
            if any(position in transform_covered for position in range(index, index + length)):
                group = None
        if group is not None:
            source_start, length, dx, dy, _saved = group
            _write_group_record(w, state, index - source_start, length, (dx, dy), display_commands[index:index + length])
            translated_group_repeats += 1
            copied_commands += length
            index += length
            continue
        record, state, used_repeat = _best_single_or_normal(display_commands[index], state, palette)
        w.extend(record)
        single_repeats += int(used_repeat)
        index += 1

    metrics = {
        "single_repeats": single_repeats,
        "group_repeats": translated_group_repeats + transformed_references,
        "translated_group_repeats": translated_group_repeats,
        "transformed_groups": transformed_groups,
        "transformed_references": transformed_references,
        "copied_commands": copied_commands,
        "primitive_count": primitive_count,
        "vectorized_primitive_count": vectorized_count,
        "transport_commands": len(display_commands),
        "source_commands": len(raw_source),
    }
    data, mode = w.best_bytes()
    metrics["coding_mode"] = mode
    metrics["refits"] = refit_count
    metrics["coarsened"] = coarsened
    return data, len(data) * 8, metrics, palette


def decode_commands(data: bytes, mode: int = 1) -> Tuple[List[VectorCommand], List[str]]:
    r = BitReader(data, mode)
    version = r.bits_n(4)
    if version != PROTOCOL_VERSION:
        raise MCIError(f"Unsupported MCoreIMG protocol version {version}; expected {PROTOCOL_VERSION}.")
    palette = [from_rgb565_a4(r.bits_n(16), r.bits_n(4)) for _ in range(r.ue(MAX_PALETTE - 1) + 1)]
    output_count = r.ue(MAX_COMMANDS)
    state = DecoderState()
    result: List[VectorCommand] = []
    definitions: List[Tuple[List[VectorCommand], int, int]] = []

    while len(result) < output_count:
        record_type = r.bits_n(2, "rec")
        if record_type == REC_NORMAL:
            # The helper expects to consume the record type itself. Recreate a
            # tiny reader prefix by decoding this normal record inline.
            same = bool(r.bit("sameop"))
            if same:
                if state.previous_opcode is None:
                    raise MCIError("Same opcode before initialization.")
                opcode = state.previous_opcode
            else:
                opcode = r.bits_n(3, "op")
            if opcode not in OP_NAMES:
                raise MCIError("Invalid opcode.")
            same_style = bool(r.bit("samestyle"))
            if same_style:
                if opcode not in state.style_states:
                    raise MCIError("Style reuse before initialization.")
                style = copy.deepcopy(state.style_states[opcode])
            else:
                style = read_style(r, palette)
                state.style_states[opcode] = copy.deepcopy(style)
            command = VectorCommand(opcode, style, read_geometry(r, opcode, state.point_states[opcode], palette))
            validate_command(command)
            generated = [command]
        elif record_type == REC_SINGLE_REPEAT:
            opcode = r.bits_n(3, "op")
            if opcode not in state.recent:
                raise MCIError("Repeat references missing opcode history.")
            symmetry = r.bits_n(3, "rep.symcode") if r.bit("rep.sym") else SYM_IDENTITY
            source = apply_symmetry(state.recent[opcode], symmetry)
            command = quantize_command(
                translate_command(source, r.se(2048, "rep.dx"), r.se(2048, "rep.dy"))
            )
            generated = [command]
        elif record_type == REC_GROUP_REPEAT:
            length = r.ue(MAX_COMMANDS - 2) + 2
            adjacent = bool(r.bit("grp.adj"))
            distance = length if adjacent else length + r.ue(MAX_COMMANDS - length)
            dx, dy = r.se(2048, "rep.dx"), r.se(2048, "rep.dy")
            source_start = len(result) - distance
            if source_start < 0 or source_start + length > len(result):
                raise MCIError("Group-copy reference is outside decoded history.")
            generated = [
                quantize_command(translate_command(result[source_start + offset], dx, dy))
                for offset in range(length)
            ]
        elif record_type == REC_TRANSFORM_GROUP:
            is_reference = bool(r.bit("grp.ref"))
            x, y, width, height, local_width, local_height = _read_transform_box(r)
            if is_reference:
                definition_index = r.ue(MAX_COMMANDS)
                if definition_index >= len(definitions):
                    raise MCIError("SVG group references an undefined local definition.")
                local_commands, saved_local_width, saved_local_height = definitions[definition_index]
                if local_width != saved_local_width or local_height != saved_local_height:
                    raise MCIError("SVG group reference has mismatched local dimensions.")
            else:
                length = r.ue(MAX_COMMANDS - 2) + 2
                nested_state = DecoderState()
                local_commands = [
                    _decode_regular_record(r, palette, nested_state, local_width, local_height)
                    for _ in range(length)
                ]
                definitions.append((copy.deepcopy(local_commands), local_width, local_height))
            generated = _apply_transform_group(
                local_commands, x, y, width, height, local_width, local_height,
            )
        else:
            raise MCIError("Invalid record type.")

        if len(result) + len(generated) > output_count:
            raise MCIError("Record expands beyond declared command count.")
        result.extend(generated)
        for command in generated:
            state.recent[command.opcode] = command.clone()
            state.previous_opcode = command.opcode

    return result, palette

# ==========================================================================
# Base91 payload, Base62 headers, and MeshCore framing
# ==========================================================================

def base91_encode(data:bytes)->str:
    b=0;n=0;out=[]
    for byte in data:
        b|=byte<<n;n+=8
        if n>13:
            v=b&8191
            if v>88: b>>=13;n-=13
            else: v=b&16383;b>>=14;n-=14
            out.append(BASE91[v%91]);out.append(BASE91[v//91])
    if n:
        out.append(BASE91[b%91])
        if n>7 or b>90: out.append(BASE91[b//91])
    return "".join(out)


def base91_decode(text:str)->bytes:
    b=0;n=0;v=-1;out=bytearray()
    for ch in text:
        if ch not in BASE91_INDEX: raise MCIError(f"Invalid Base91 character {ch!r}.")
        c=BASE91_INDEX[ch]
        if v<0: v=c
        else:
            v+=c*91;b|=v<<n;n+=13 if (v&8191)>88 else 14
            while n>=8: out.append(b&255);b>>=8;n-=8
            v=-1
    if v>=0:
        b|=v<<n;n+=7
        while n>=8: out.append(b&255);b>>=8;n-=8
    return bytes(out)


def enc62(v:int,width:int)->str:
    if v<0 or v>=62**width: raise MCIError("Base62 field overflow.")
    chars=[]
    for _ in range(width): v,r=divmod(v,62);chars.append(BASE62[r])
    return "".join(reversed(chars))


def dec62(s:str)->int:
    v=0
    for ch in s:
        if ch not in BASE62: raise FrameError("Invalid Base62 character.")
        v=v*62+BASE62.index(ch)
    return v



# ==========================================================================
# Image layer
# ==========================================================================


# ==========================================================================
# Erasure parity (RAID 6)
# ==========================================================================
#
# Two extra messages, P and Q, let a receiver rebuild any two lost messages
# out of the k + 2 sent -- data, parity, or one of each -- exactly as RAID 6
# survives two failed disks. The k data frames are untouched: a receiver that
# ignores parity frames decodes them as before.
#
# WHY NOT GF(256)
#
# Parity is normally computed over bytes in GF(256). Here that would produce
# arbitrary bytes, which MeshCore cannot carry; Base91-encoding them grows the
# parity by about 23%, so a parity frame would need ~175 characters for 142
# characters of data and would not fit in a message.
#
# THE ALPHABET-PRESERVING CODE
#
# Instead the code works directly on Base91 symbols. 91 = 7 * 13, so by the
# Chinese remainder theorem a symbol c in 0..90 is exactly the pair
# (c mod 7, c mod 13), and any such pair is exactly one symbol. Parity is
# computed independently in each residue and recombined, so every parity
# symbol is again a Base91 character and a parity frame is exactly as long as
# the data it protects.
#
# P + Q RAID 6 needs a field whose multiplicative group has at least k
# distinct elements, to give each data frame its own Q coefficient:
#
#   mod 13  GF(13) has order 12; generator 2.          One symbol at a time.
#   mod 7   GF(7) has only order 6, too few for k=10.  Adjacent symbols are
#           paired into GF(49) = GF(7)[i], i^2 = -1 (irreducible because
#           7 = 3 mod 4), which has order 48.
#
# With distinct coefficients, every 2x2 system the decoder can meet is
# non-singular, which is the whole of the RAID 6 guarantee.
#
# FRAME LAYOUT
#
#   MCP   parity magic    3     distinct from MCI, so a receiver can tell a
#   iiii  image id        4     parity frame apart before parsing it
#   d     descriptor      1     which + 2*(k-1) + 20*mode, 0..39
#   ...  parity symbols   L     L = longest data payload, rounded up to even
#
# The descriptor carries k and the coding mode, so a receiver that has lost
# the final data frame -- or every data frame, when k = 1 -- still knows how
# many frames exist and how to decode them. That also gives a receive engine
# a denominator before the final data frame arrives.
#
# The one thing parity cannot carry is the exact length of the final data
# frame: a parity frame is already full at MESSAGE_LEN. When the final
# frame has to be rebuilt, its length is resolved by trying each candidate
# and keeping the one whose stream is canonical Base91, passes the stream
# CRC-32, and parses. More than one survivor is reported, never guessed.

PARITY_MAGIC = "MCP"
PARITY_FRAMES = 2
PARITY_P = 0
PARITY_Q = 1
_PAD_SYMBOL = 0  # BASE91[0]; data shorter than L is padded with it

_G13 = 2
_G49 = 9  # 2 + 1*i, the smallest generator of GF(49)* in re + 7*im order


def image_id_for(raw: bytes) -> str:
    """The image id for an encoded stream (stream bytes plus stream CRC-32)."""
    return enc62(zlib.crc32(raw) % IMAGE_ID_SPACE, IMAGE_ID_LEN)


def _data_header(image_id: str, index: int, final: bool, mode: int) -> str:
    part = index + (MAX_MESSAGES if final else 0) + (2 * MAX_MESSAGES if mode else 0)
    return FRAME_MAGIC + image_id + enc62(part, 1)


def _parity_header(image_id: str, which: int, k: int, mode: int) -> str:
    return PARITY_MAGIC + image_id + enc62(which + 2 * (k - 1) + 2 * MAX_MESSAGES * mode, 1)


def _gf13_inv(a: int) -> int:
    return pow(a, 11, 13)


def _gf49_add(x: int, y: int) -> int:
    return ((x % 7 + y % 7) % 7) + 7 * ((x // 7 + y // 7) % 7)


def _gf49_sub(x: int, y: int) -> int:
    return ((x % 7 - y % 7) % 7) + 7 * ((x // 7 - y // 7) % 7)


def _gf49_mul(x: int, y: int) -> int:
    a, b, c, d = x % 7, x // 7, y % 7, y // 7
    return ((a * c - b * d) % 7) + 7 * ((a * d + b * c) % 7)


_GF49_INV = {x: next(y for y in range(1, 49) if _gf49_mul(x, y) == 1) for x in range(1, 49)}

_COEF13 = [pow(_G13, j, 13) for j in range(MAX_MESSAGES)]
_COEF49 = [1]
for _j in range(MAX_MESSAGES - 1):
    _COEF49.append(_gf49_mul(_COEF49[-1], _G49))
del _j

# The guarantee rests on these. Changing a generator is a protocol change, so
# make it impossible to do silently.
assert len(set(_COEF13)) == MAX_MESSAGES and 0 not in _COEF13
assert len(set(_COEF49)) == MAX_MESSAGES and 0 not in _COEF49
assert all((78 * (c % 7) + 14 * (c % 13)) % 91 == c for c in range(91))

# (add, sub, mul, inverse, coefficients) per residue field
_FIELD13 = (lambda a, b: (a + b) % 13, lambda a, b: (a - b) % 13,
            lambda a, b: a * b % 13, _gf13_inv, _COEF13)
_FIELD49 = (_gf49_add, _gf49_sub, _gf49_mul, _GF49_INV.__getitem__, _COEF49)


def _crt91(r7: int, r13: int) -> int:
    """Recombine residues into a Base91 symbol: 78 = 13*inv(13,7), 14 = 7*inv(7,13)."""
    return (78 * r7 + 14 * r13) % 91


def _symbols(payload: str, length: int) -> List[int]:
    try:
        values = [BASE91_INDEX[ch] for ch in payload]
    except KeyError as exc:
        raise FrameError(f"Invalid Base91 character {exc.args[0]!r} in frame payload.") from None
    return values + [_PAD_SYMBOL] * (length - len(values))


def _parity_length(payload_lengths: Sequence[int]) -> int:
    longest = max(payload_lengths) if payload_lengths else 0
    return longest + (longest & 1)


def _parity_symbols(rows: Sequence[Sequence[int]], length: int) -> Tuple[List[int], List[int]]:
    """P and Q over equal-length symbol rows."""
    add13, _sub13, mul13, _inv13, c13 = _FIELD13
    p13 = [0] * length
    q13 = [0] * length
    for j, row in enumerate(rows):
        for t in range(length):
            r = row[t] % 13
            p13[t] = add13(p13[t], r)
            q13[t] = add13(q13[t], mul13(c13[j], r))
    p7 = [0] * length
    q7 = [0] * length
    for s in range(0, length, 2):
        pe = qe = 0
        for j, row in enumerate(rows):
            e = row[s] % 7 + 7 * (row[s + 1] % 7)
            pe = _gf49_add(pe, e)
            qe = _gf49_add(qe, _gf49_mul(_COEF49[j], e))
        p7[s], p7[s + 1] = pe % 7, pe // 7
        q7[s], q7[s + 1] = qe % 7, qe // 7
    return ([_crt91(p7[t], p13[t]) for t in range(length)],
            [_crt91(q7[t], q13[t]) for t in range(length)])


def _solve_erasures(fld, known: Dict[int, int], p: Optional[int], q: Optional[int],
                    missing: Sequence[int]) -> Dict[int, int]:
    """Solve one symbol position for up to two missing data values."""
    add, sub, mul, inv, coef = fld
    if not missing:
        return {}
    if len(missing) == 1:
        x = missing[0]
        if p is not None:
            v = p
            for d in known.values():
                v = sub(v, d)
            return {x: v}
        v = q
        for j, d in known.items():
            v = sub(v, mul(coef[j], d))
        return {x: mul(v, inv(coef[x]))}
    x, y = missing
    pp, qq = p, q
    for j, d in known.items():
        pp = sub(pp, d)
        qq = sub(qq, mul(coef[j], d))
    dx = mul(sub(qq, mul(coef[y], pp)), inv(sub(coef[x], coef[y])))
    return {x: dx, y: sub(pp, dx)}


def build_parity_frames(frames: Sequence[str]) -> List[str]:
    """Return the P and Q frames protecting a complete, in-order data frame set."""
    if not frames:
        return []
    head = frames[0][:FRAME_HEADER_LEN]
    image_id, part = head[3:3 + IMAGE_ID_LEN], dec62(head[-1])
    mode = 1 if part >= 2 * MAX_MESSAGES else 0
    payloads = [f[FRAME_HEADER_LEN:] for f in frames]
    length = _parity_length([len(x) for x in payloads])
    rows = [_symbols(x, length) for x in payloads]
    p_sym, q_sym = _parity_symbols(rows, length)
    k = len(frames)
    out = []
    for which, symbols in ((PARITY_P, p_sym), (PARITY_Q, q_sym)):
        out.append(_parity_header(image_id, which, k, mode) + "".join(BASE91[v] for v in symbols))
    return out


@dataclass
class _DataFrame:
    index: int
    final: bool
    mode: int
    image_id: str
    payload: str


@dataclass
class _ParityFrame:
    which: int
    k: int
    mode: int
    image_id: str
    payload: str


def _parse_frame(frame: str):
    """Classify and validate one frame's header. Returns a _DataFrame or _ParityFrame."""
    if len(frame) < FRAME_HEADER_LEN or len(frame) > MESSAGE_LEN:
        raise FrameError("Invalid frame length.")
    head, payload = frame[:FRAME_HEADER_LEN], frame[FRAME_HEADER_LEN:]
    magic = head[:3]
    if magic not in (FRAME_MAGIC, PARITY_MAGIC):
        raise FrameError("Wrong frame magic.")
    image_id = head[3:3 + IMAGE_ID_LEN]
    dec62(image_id)  # validates the alphabet
    descriptor = dec62(head[-1])

    if magic == PARITY_MAGIC:
        if descriptor >= 4 * MAX_MESSAGES:
            raise FrameError("Parity descriptor is outside the transport profile.")
        mode = 1 if descriptor >= 2 * MAX_MESSAGES else 0
        descriptor -= 2 * MAX_MESSAGES * mode
        k = descriptor // 2 + 1
        if not payload or len(payload) % 2:
            raise FrameError("Parity frame payload must be a non-empty even length.")
        if k > 1 and len(payload) != FRAME_PAYLOAD_LEN:
            raise FrameError(
                f"Parity frame for {k} data frames carries {len(payload)} symbols; "
                f"expected {FRAME_PAYLOAD_LEN}.")
        return _ParityFrame(descriptor % 2, k, mode, image_id, payload)

    mode = 1 if descriptor >= 2 * MAX_MESSAGES else 0
    descriptor -= 2 * MAX_MESSAGES * mode
    final = descriptor >= MAX_MESSAGES
    index = descriptor - MAX_MESSAGES if final else descriptor
    if not 0 <= index < MAX_MESSAGES:
        raise FrameError("Frame index is outside the transport profile.")
    # Chunking fills every frame except the last, so a non-final frame that is
    # not exactly full has been truncated or concatenated.
    if not final and len(payload) != FRAME_PAYLOAD_LEN:
        raise FrameError(
            f"Non-final frame {index} carries {len(payload)} payload characters; "
            f"expected {FRAME_PAYLOAD_LEN}.")
    return _DataFrame(index, final, mode, image_id, payload)


def _stream_from_payloads(payloads: Sequence[str], image_id: str) -> Optional[bytes]:
    """Return stream data if the payload text is canonical, CRC-valid, and is
    the content its image id names."""
    text = "".join(payloads)
    try:
        raw = base91_decode(text)
    except MCIError:
        return None
    if len(raw) < 4 or base91_encode(raw) != text:
        return None
    data, crc = raw[:-4], int.from_bytes(raw[-4:], "big")
    if zlib.crc32(data) != crc or image_id_for(raw) != image_id:
        return None
    return data


@dataclass
class Reassembly:
    """A complete, in-order data frame set, and what it took to get there."""

    frames: List[str]
    recovered: List[int]
    mode: int
    k: int
    parity_received: int
    image_id: str = ""


def reassemble_frames(frames: Sequence[str]) -> Reassembly:
    """Validate a received set and rebuild up to two missing frames from parity.

    Accepts any mix of data and parity frames in any order, with duplicates.
    Raises FrameError when the set cannot be completed, saying what is missing
    and how much parity arrived.
    """
    data: Dict[int, _DataFrame] = {}
    parity: Dict[int, _ParityFrame] = {}
    image_id: Optional[str] = None
    mode: Optional[int] = None
    final_index: Optional[int] = None
    k_parity: Optional[int] = None

    for raw in frames:
        frame = _parse_frame(raw.strip())
        if image_id is None:
            image_id = frame.image_id
        elif frame.image_id != image_id:
            raise FrameError("Mixed image frames.")
        if mode is None:
            mode = frame.mode
        elif frame.mode != mode:
            raise FrameError("Frames disagree about the coding mode.")

        if isinstance(frame, _ParityFrame):
            if k_parity is not None and frame.k != k_parity:
                raise FrameError("Parity frames disagree about the data frame count.")
            k_parity = frame.k
            previous = parity.get(frame.which)
            if previous is not None and previous.payload != frame.payload:
                raise FrameError("Conflicting duplicate parity frame.")
            parity[frame.which] = frame
            continue

        if frame.final:
            if final_index is not None and final_index != frame.index:
                raise FrameError("Frame set declares two different final frames.")
            final_index = frame.index
        previous = data.get(frame.index)
        if previous is not None and previous.payload != frame.payload:
            raise FrameError("Conflicting duplicate frame.")
        data[frame.index] = frame

    if final_index is not None and k_parity is not None and final_index + 1 != k_parity:
        raise FrameError("Parity and data frames disagree about the frame count.")
    if final_index is not None:
        k = final_index + 1
    elif k_parity is not None:
        k = k_parity
    else:
        raise FrameError("Frame set has no final frame; parts are missing.")
    if any(index >= k for index in data):
        raise FrameError("Data frame index lies beyond the final frame.")
    if len(parity) == 2 and len(parity[PARITY_P].payload) != len(parity[PARITY_Q].payload):
        raise FrameError("P and Q parity frames differ in length.")

    missing = [j for j in range(k) if j not in data]
    if not missing:
        ordered = [_data_header(image_id, j, j == k - 1, mode) + data[j].payload for j in range(k)]
        return Reassembly(ordered, [], mode, k, len(parity), image_id)
    if len(missing) > len(parity):
        # Parts are numbered from 1 here, matching the Constructor's frame
        # listing and the Reconstructor's recovery report.
        lost = ", ".join(str(j + 1) for j in missing)
        raise FrameError(
            f"Missing data parts {lost} of {k}. {len(parity)} of {PARITY_FRAMES} parity "
            f"frame(s) received, enough to rebuild {len(parity)} missing part(s).")

    length = len(next(iter(parity.values())).payload)
    rows = {j: _symbols(f.payload, length) for j, f in data.items()}
    if any(len(f.payload) > length for f in data.values()):
        raise FrameError("A data frame is longer than its parity.")
    p_row = _symbols(parity[PARITY_P].payload, length) if PARITY_P in parity else None
    q_row = _symbols(parity[PARITY_Q].payload, length) if PARITY_Q in parity else None

    rebuilt = {j: [0] * length for j in missing}
    r13 = {j: [0] * length for j in missing}
    for t in range(length):
        known = {j: row[t] % 13 for j, row in rows.items()}
        solved = _solve_erasures(_FIELD13, known,
                                 None if p_row is None else p_row[t] % 13,
                                 None if q_row is None else q_row[t] % 13, missing)
        for j, v in solved.items():
            r13[j][t] = v
    for s in range(0, length, 2):
        known = {j: row[s] % 7 + 7 * (row[s + 1] % 7) for j, row in rows.items()}
        pe = None if p_row is None else p_row[s] % 7 + 7 * (p_row[s + 1] % 7)
        qe = None if q_row is None else q_row[s] % 7 + 7 * (q_row[s + 1] % 7)
        solved = _solve_erasures(_FIELD49, known, pe, qe, missing)
        for j, v in solved.items():
            rebuilt[j][s] = _crt91(v % 7, r13[j][s])
            rebuilt[j][s + 1] = _crt91(v // 7, r13[j][s + 1])

    payloads: Dict[int, str] = {j: f.payload for j, f in data.items()}
    for j in missing:
        if j != k - 1:
            if length != FRAME_PAYLOAD_LEN:
                raise FrameError("Parity length does not match a full data frame.")
            payloads[j] = "".join(BASE91[v] for v in rebuilt[j])

    final = k - 1
    if final in missing:
        # The final frame is padded to the parity length, and its true length
        # is not carried anywhere. Every position past the true end must have
        # rebuilt to the pad symbol; each remaining candidate is tried, and
        # only one may yield a canonical, CRC-valid, parseable stream.
        symbols = rebuilt[final]
        last_real = max((t for t, v in enumerate(symbols) if v != _PAD_SYMBOL), default=-1)
        survivors = []
        for n in range(max(1, last_real + 1), length + 1):
            text = "".join(BASE91[v] for v in symbols[:n])
            trial = [payloads[j] for j in range(final)] + [text]
            stream = _stream_from_payloads(trial, image_id)
            if stream is None:
                continue
            try:
                decode_commands(stream, mode)
            except MCIError:
                continue
            survivors.append(text)
        if not survivors:
            raise FrameError("Parity reconstruction did not produce a valid stream.")
        if len(survivors) > 1:
            raise FrameError(
                f"Rebuilt final frame has {len(survivors)} valid lengths; refusing to guess.")
        payloads[final] = survivors[0]

    ordered = [_data_header(image_id, j, j == final, mode) + payloads[j] for j in range(k)]
    return Reassembly(ordered, missing, mode, k, len(parity), image_id)


def _encode_image_once(commands: Sequence[VectorCommand], local_extent: int) -> EncodedImage:
    """Encode once at a particular local-coordinate precision.

    The precision is a plain argument, so this function is re-entrant and safe
    to call concurrently or from a library context.
    """
    bit_bytes, bit_count, metrics, palette = encode_commands(commands, int(local_extent), preprocessed=True)
    mode = metrics.get("coding_mode", 1)
    raw = bit_bytes + zlib.crc32(bit_bytes).to_bytes(4, "big")
    payload = base91_encode(raw)
    image_id = image_id_for(raw)
    chunks = [payload[i:i + FRAME_PAYLOAD_LEN] for i in range(0, len(payload), FRAME_PAYLOAD_LEN)] or [""]
    total = len(chunks)
    # Over-budget images are framed as an empty set rather than as frames whose
    # one-character part descriptors would silently alias past index 9.
    #
    # This must not raise. encode_image walks the precision ladder from highest
    # fidelity downward and the first rungs are routinely over budget; aborting
    # there made an image that fits at a lower precision fail outright.
    # stats.fits carries the verdict, and every consumer of .frames checks it.
    over_limit = total > MAX_MESSAGES
    if total > MAX_MESSAGES:
        # Still framed so the caller can report an honest over-limit result;
        # stats.fits is what decides whether it may be transmitted.
        pass
    frames = []
    for index, chunk in enumerate([] if over_limit else chunks):
        final = index == total - 1
        frames.append(_data_header(image_id, index, final, mode) + chunk)
    stats = CodecStats(
        metrics["transport_commands"], len(palette), bit_count, len(raw), len(payload), total,
        metrics["single_repeats"] + metrics["group_repeats"], metrics["group_repeats"],
        metrics["primitive_count"], metrics["vectorized_primitive_count"], metrics["source_commands"],
        metrics["transformed_groups"], metrics["transformed_references"], int(local_extent),
        mode,
    )
    return EncodedImage(raw, payload, frames, stats, image_id, palette)


def prepare_geometry(commands: Sequence[VectorCommand]) -> List[VectorCommand]:
    """Run the precision-independent geometry passes once.

    Per-object tolerance selection and shape refitting depend only on the
    source outlines, not on the local-coordinate extent, so doing them inside
    the precision ladder repeats identical work up to fifteen times.
    """
    working = [command.clone() for command in commands]
    palette = build_palette(working)
    working, _coarsened = simplify_rate_distortion(working, palette)
    working, _refits = refit_commands(working, palette)
    working, _palette, _merges = merge_palette(working, build_palette(working))
    return working


def encode_image(commands: Sequence[VectorCommand], parity: bool = True) -> EncodedImage:
    """Encode, then add the two RAID 6 parity frames when the image fits.

    Parity is computed once on the chosen result, not on every rung of the
    precision ladder, and never on an over-limit image. ``parity=False`` sends
    the k data frames alone, saving two messages of airtime.
    """
    encoded = _search_encoding(commands)
    if parity and encoded.stats.fits and encoded.frames:
        encoded.parity_frames = build_parity_frames(encoded.frames)
        encoded.stats.parity_frame_count = len(encoded.parity_frames)
    return encoded


def _search_encoding(commands: Sequence[VectorCommand]) -> EncodedImage:
    """Encode with the highest local-space precision that fits ten messages.

    Images without eligible local groups take the direct path. For local SVG
    groups, the precision ladder trades coordinate fidelity for payload size.
    The first fitting candidate wins; if none fit, the smallest/last candidate
    is returned so the GUI can report an honest over-limit result.
    """
    # Geometry selection -- per-object simplification tolerance and shape
    # refitting -- is independent of local-coordinate precision, so it runs
    # once here rather than inside every rung of the ladder below. It must
    # happen before the early return too, or images without local groups would
    # skip it entirely.
    prepared = [command.clone() for command in prepare_geometry(commands)]
    commands = prepared

    # Keep as much local-coordinate precision as the ten-message budget allows.
    # The chosen precision depends on geometry complexity, never on displayed
    # scale, so resizing the same SVG leaves its payload and frame count stable.
    has_transform_group = False
    previous_group: Optional[int] = None
    group_run = 0
    for command in commands:
        if command.editor_group is not None and command.opcode != OP_PRIMITIVE:
            if command.editor_group == previous_group:
                group_run += 1
            else:
                previous_group = command.editor_group
                group_run = 1
            if group_run >= 2:
                has_transform_group = True
                break
        else:
            previous_group = None
            group_run = 0

    if not has_transform_group:
        return _encode_image_once(commands, LOCAL_GROUP_EXTENT)

    smallest: Optional[EncodedImage] = None
    for extent in PRECISION_LADDER:
        candidate = _encode_image_once(commands, extent)
        smallest = candidate
        if candidate.stats.fits:
            return candidate
    assert smallest is not None
    return smallest


def decode_frames(frames: Sequence[str]) -> List[VectorCommand]:
    """Validate, reassemble, and decompress a received frame set.

    Any mix of data and parity frames is accepted; up to two missing frames are
    rebuilt from parity. Integrity of an individual message is MeshCore's job.
    What is checked here is everything MeshCore cannot see: that the frames
    belong to one image, that none are missing or contradictory, and that the
    reassembled stream matches its CRC-32.
    """
    assembled = reassemble_frames(frames)
    payloads = [f[FRAME_HEADER_LEN:] for f in assembled.frames]
    raw = base91_decode("".join(payloads))
    if len(raw) < 4:
        raise FrameError("Stream too short.")
    data, crc = raw[:-4], int.from_bytes(raw[-4:], "big")
    if zlib.crc32(data) != crc:
        raise FrameError("Stream CRC-32 mismatch.")
    if image_id_for(raw) != assembled.image_id:
        raise FrameError("Stream does not match its image id; frames from different images were mixed.")
    return decode_commands(data, assembled.mode)[0]



# ===========================================================================
# Multi-image receive: sorting interleaved frames into sets
# ===========================================================================
#
# On a shared channel, messages from several images -- from several stations,
# or several images from one station -- arrive interleaved, out of order, with
# duplicates, gaps, and unrelated traffic. FrameSorter takes them one at a time
# and keeps each image's set apart by its image id, decoding each image as soon
# as enough of its messages have arrived: all k data frames, or any k of the
# k + 2 when parity is present.
#
# Guarantees:
#
#   * An image is only ever reported once its stream passes CRC-32 *and*
#     hashes to its own image id, so frames from two images cannot be mixed
#     into one result.
#   * Identical frames -- duplicates, or the same image sent by two stations --
#     merge silently, before and after the image completes.
#   * If two different images ever share an id, their frames are kept, and
#     each candidate assembly is tried until one proves itself; the frames it
#     used are taken out and the remainder carries on as the other image.
#   * Garbage never raises. Unparseable text is counted and dropped.
#   * Memory is bounded: pending sets, frames per set, and remembered
#     completed images are all capped, oldest first.
#
# Timing policy -- how long an incomplete set may wait -- belongs to the
# channel layer, so the sorter only offers expire(older_than); it never
# decides on its own that a set is abandoned.

SORTER_MAX_PENDING = 64
SORTER_MAX_FRAMES_PER_SET = 4 * (MAX_MESSAGES + PARITY_FRAMES)
SORTER_MAX_COMPLETED = 256
SORTER_COMBINATION_LIMIT = 512   # new candidate assemblies tried per arriving frame
SORTER_TRIED_MEMORY = 8192       # failed assemblies remembered per set


@dataclass
class DecodedImage:
    """One image the sorter has completed and decoded."""

    image_id: str
    commands: List[VectorCommand]
    frames: List[str]
    recovered: List[int]
    k: int
    parity_received: int
    first_seen: float
    completed_at: float


@dataclass
class SetStatus:
    """Progress of one incomplete image set."""

    image_id: str
    k: Optional[int]
    data_received: List[int]
    parity_received: int
    frames_held: int
    first_seen: float
    last_seen: float
    conflicting: bool

    @property
    def missing(self) -> Optional[List[int]]:
        if self.k is None:
            return None
        return [j for j in range(self.k) if j not in self.data_received]

    @property
    def recoverable_now(self) -> bool:
        """True when what has arrived should be enough to decode."""
        missing = self.missing
        return missing is not None and len(missing) <= self.parity_received

    def describe(self) -> str:
        if self.k is None:
            return (f"{self.image_id}: {len(self.data_received)} data frame(s), "
                    f"{self.parity_received} parity; total unknown until the final "
                    f"frame or a parity frame arrives")
        parts = ", ".join(str(j + 1) for j in self.missing) or "none"
        note = "; conflicting frames share this id" if self.conflicting else ""
        return (f"{self.image_id}: {len(self.data_received)}/{self.k} data + "
                f"{self.parity_received}/{PARITY_FRAMES} parity, missing part(s) {parts}{note}")


@dataclass
class _Pool:
    first_seen: float
    last_seen: float
    frames: Dict[str, Any] = field(default_factory=dict)  # frame text -> parsed
    tried: set = field(default_factory=set)  # frozensets of frames already disproved


class FrameSorter:
    """Sort interleaved frames from many images into sets and decode each."""

    def __init__(self, max_pending: int = SORTER_MAX_PENDING,
                 max_frames_per_set: int = SORTER_MAX_FRAMES_PER_SET,
                 max_completed: int = SORTER_MAX_COMPLETED) -> None:
        self.max_pending = max_pending
        self.max_frames_per_set = max_frames_per_set
        self.max_completed = max_completed
        self._pending: Dict[str, _Pool] = {}
        self._completed: Dict[str, List[set]] = {}  # id -> expected frame sets
        self.duplicates = 0
        self.rejected = 0
        self.evicted: List[SetStatus] = []

    # ------------------------------------------------------------------ input
    def add(self, frame: str, now: Optional[float] = None) -> List[DecodedImage]:
        """Accept one received message. Returns any images it completed."""
        now = time.time() if now is None else now
        text = frame.strip()
        try:
            parsed = _parse_frame(text)
        except MCIError:
            self.rejected += 1
            return []
        image_id = parsed.image_id
        if any(text in expected for expected in self._completed.get(image_id, ())):
            self.duplicates += 1
            return []
        pool = self._pending.get(image_id)
        if pool is None:
            pool = self._pending[image_id] = _Pool(now, now)
        pool.last_seen = now
        if text in pool.frames:
            self.duplicates += 1
            return []
        if len(pool.frames) >= self.max_frames_per_set:
            self.rejected += 1
            return []
        pool.frames[text] = parsed
        completed = self._complete(image_id, now)
        self._enforce_pending_limit()
        return completed

    def add_many(self, frames: Sequence[str], now: Optional[float] = None) -> List[DecodedImage]:
        out: List[DecodedImage] = []
        for frame in frames:
            out.extend(self.add(frame, now))
        return out

    # ------------------------------------------------------------------ state
    def pending(self) -> List[SetStatus]:
        """Every incomplete set, oldest first."""
        return [self._status(i, p) for i, p in
                sorted(self._pending.items(), key=lambda item: item[1].first_seen)]

    def expire(self, older_than: float) -> List[SetStatus]:
        """Drop incomplete sets not heard from since ``older_than``."""
        stale = [i for i, p in self._pending.items() if p.last_seen < older_than]
        dropped = [self._status(i, self._pending[i]) for i in stale]
        for image_id in stale:
            del self._pending[image_id]
        return dropped

    # ---------------------------------------------------------------- internals
    def _status(self, image_id: str, pool: _Pool) -> SetStatus:
        candidates = self._candidates(pool)
        conflicting = len(candidates) > 1 or self._has_slot_conflict(pool)
        if candidates:
            k, mode = max(candidates, key=lambda km: self._support(pool, *km))
            data = sorted({f.index for f in pool.frames.values()
                           if isinstance(f, _DataFrame) and f.mode == mode and f.index < k
                           and f.final == (f.index == k - 1)})
            parity = len({f.which for f in pool.frames.values()
                          if isinstance(f, _ParityFrame) and f.k == k and f.mode == mode})
        else:
            k = None
            data = sorted({f.index for f in pool.frames.values() if isinstance(f, _DataFrame)})
            parity = 0
        return SetStatus(image_id, k, data, parity, len(pool.frames),
                         pool.first_seen, pool.last_seen, conflicting)

    @staticmethod
    def _candidates(pool: _Pool) -> List[Tuple[int, int]]:
        """(k, mode) pairs that the frames held could belong to."""
        out = set()
        for f in pool.frames.values():
            if isinstance(f, _DataFrame) and f.final:
                out.add((f.index + 1, f.mode))
            elif isinstance(f, _ParityFrame):
                out.add((f.k, f.mode))
        return sorted(out)

    @staticmethod
    def _has_slot_conflict(pool: _Pool) -> bool:
        seen = set()
        for f in pool.frames.values():
            slot = ("d", f.index, f.final, f.mode) if isinstance(f, _DataFrame) else ("p", f.which, f.k, f.mode)
            if slot in seen:
                return True
            seen.add(slot)
        return False

    @staticmethod
    def _support(pool: _Pool, k: int, mode: int) -> int:
        return sum(1 for f in pool.frames.values()
                   if f.mode == mode and (
                       (isinstance(f, _DataFrame) and f.index < k and f.final == (f.index == k - 1))
                       or (isinstance(f, _ParityFrame) and f.k == k)))

    def _complete(self, image_id: str, now: float) -> List[DecodedImage]:
        """Decode every image the pool can now prove, peeling each one out."""
        done: List[DecodedImage] = []
        while True:
            pool = self._pending.get(image_id)
            if pool is None:
                return done
            result = self._try_pool(image_id, pool, now)
            if result is None:
                return done
            image, expected = result
            for frame in [f for f in pool.frames if f in expected]:
                del pool.frames[frame]
            if not pool.frames:
                del self._pending[image_id]
            self._remember(image_id, expected)
            done.append(image)

    def _try_pool(self, image_id: str, pool: _Pool, now: float):
        for k, mode in sorted(self._candidates(pool), key=lambda km: -self._support(pool, *km)):
            data_slots: List[List[str]] = [[] for _ in range(k)]
            parity_slots: List[List[str]] = [[], []]
            for text, f in pool.frames.items():
                if f.mode != mode:
                    continue
                if isinstance(f, _DataFrame):
                    if f.index < k and f.final == (f.index == k - 1):
                        data_slots[f.index].append(text)
                elif f.k == k:
                    parity_slots[f.which].append(text)
            for choice in self._assemblies(data_slots, parity_slots, pool.tried):
                try:
                    assembled = reassemble_frames(list(choice))
                    commands = decode_frames(assembled.frames)
                except MCIError:
                    # A wrong assembly stays wrong, so never pay for it twice.
                    if len(pool.tried) >= SORTER_TRIED_MEMORY:
                        pool.tried.clear()
                    pool.tried.add(frozenset(choice))
                    continue
                expected = set(assembled.frames) | set(build_parity_frames(assembled.frames))
                parity_used = sum(1 for text in choice if text.startswith(PARITY_MAGIC) and text in expected)
                image = DecodedImage(image_id, commands, assembled.frames, assembled.recovered,
                                     assembled.k, parity_used, pool.first_seen, now)
                return image, expected
        return None

    @staticmethod
    def _assemblies(data_slots: List[List[str]], parity_slots: List[List[str]], tried: set):
        """Candidate frame sets for one (k, mode), most-complete first.

        Without collisions every slot holds at most one frame and the first
        candidate is the answer. With two images sharing an id, a slot can hold
        a frame that belongs to the *other* image, so the search must also try
        leaving held frames out and rebuilding those parts from parity. A held
        frame from the wrong image makes the whole set fail its CRC and id
        check, so every candidate is proven or discarded, never trusted.

        When every data slot is filled, parity cannot change the answer, so no
        parity frames are enumerated for those candidates. Assemblies already
        disproved are skipped, and new attempts are capped per call, so a set
        flooded with conflicting frames costs bounded time per arriving frame.
        """
        k = len(data_slots)
        forced = [j for j in range(k) if not data_slots[j]]
        optional = [j for j in range(k) if data_slots[j]]
        parity_options = [slot + [None] for slot in parity_slots]
        attempts = 0
        for extra in range(0, PARITY_FRAMES - len(forced) + 1):
            for dropped in itertools.combinations(optional, extra):
                absent = set(forced) | set(dropped)
                if absent:
                    parity_choices = [[f for f in c if f is not None]
                                      for c in itertools.product(*parity_options)]
                    parity_choices = [c for c in parity_choices if len(c) >= len(absent)]
                else:
                    parity_choices = [[]]
                present = [data_slots[j] for j in range(k) if j not in absent]
                for chosen_parity in parity_choices:
                    for data_choice in itertools.product(*present):
                        candidate = list(data_choice) + chosen_parity
                        if frozenset(candidate) in tried:
                            continue
                        attempts += 1
                        if attempts > SORTER_COMBINATION_LIMIT:
                            return
                        yield candidate

    def _remember(self, image_id: str, expected: set) -> None:
        self._completed.setdefault(image_id, []).append(expected)
        self._completed[image_id] = self._completed.pop(image_id)  # most recent last
        while len(self._completed) > self.max_completed:
            del self._completed[next(iter(self._completed))]

    def _enforce_pending_limit(self) -> None:
        while len(self._pending) > self.max_pending:
            oldest = min(self._pending, key=lambda i: self._pending[i].last_seen)
            self.evicted.append(self._status(oldest, self._pending.pop(oldest)))


def sort_and_decode(frames: Sequence[str]) -> Tuple[List[DecodedImage], List[SetStatus], FrameSorter]:
    """Decode every complete image in a batch of frames, e.g. a channel log."""
    sorter = FrameSorter()
    images = sorter.add_many(frames, now=0.0)
    return images, sorter.pending(), sorter


# ===========================================================================
# Public API
# ===========================================================================
#
# Anything not listed here is an implementation detail of the codec and may be
# renamed or restructured without a protocol bump. Names marked "(model)" are
# re-exported from MCoreIMG-model.py for convenience and are defined there.

__all__ = [
    # build / protocol identity
    "COMPRESSION_BUILD", "PROTOCOL_VERSION",
    # transport profile
    "MAX_MESSAGES", "MESSAGE_LEN", "FRAME_HEADER_LEN", "FRAME_PAYLOAD_LEN",
    "IMAGE_ID_LEN", "IMAGE_ID_SPACE", "image_id_for",
    "MAX_PAYLOAD_CHARS", "FRAME_MAGIC", "MAX_COMMANDS", "MAX_PALETTE",
    "BASE62", "BASE91", "BASE91_INDEX",
    # record tags and precision
    "REC_NORMAL", "REC_SINGLE_REPEAT", "REC_GROUP_REPEAT", "REC_TRANSFORM_GROUP",
    "LOCAL_GROUP_EXTENT", "PRECISION_LADDER",
    # errors
    "FrameError",
    "MCIError",                                                     # (model)
    # palette and transport quantization
    "build_palette", "merge_palette", "PALETTE_MERGE_DISTANCE",
    "geom_translation",
    "simplify_commands", "SIMPLIFY_TOLERANCE",
    "refit_candidates", "refit_commands", "REFIT_TOLERANCE",
    "simplify_rate_distortion", "TOLERANCE_LADDER", "prepare_geometry",
    "apply_symmetry", "geom_symmetry", "SYMMETRY_MATRICES", "SYMMETRY_NAMES",
    # results
    "CodecStats", "EncodedImage",
    # codec entry points
    "encode_image", "decode_frames", "encode_commands", "decode_commands",
    # framing primitives (used by frame-repair tooling)
    "base91_encode", "base91_decode", "enc62", "dec62",
    # erasure parity
    "PARITY_MAGIC", "PARITY_FRAMES", "PARITY_P", "PARITY_Q",
    "build_parity_frames", "reassemble_frames", "Reassembly",
    # multi-image receive
    "FrameSorter", "DecodedImage", "SetStatus", "sort_and_decode",
    # the model module itself, for callers that want it directly
    "model", "load_model",
    # re-exported model vocabulary                                  # (model)
    "CANVAS_W", "CANVAS_H",
    "OP_RECT", "OP_ELLIPSE", "OP_LINE", "OP_POLYLINE", "OP_POLYGON", "OP_PATH",
    "OP_PRIMITIVE", "OP_NAMES",
    "SEG_M", "SEG_L", "SEG_Q", "SEG_C", "SEG_Z", "SEG_NAMES", "SEG_POINT_COUNTS",
    "PRIM_TEXT", "PRIM_TRIANGLE_OUTLINE", "PRIM_TRIANGLE_FILL", "PRIM_ARROW",
    "PRIM_STAR", "PRIM_ARC", "PRIM_YAGI", "PRIM_DISH", "PRIM_RADIO",
    "PRIM_RADIO_WAVES", "PRIM_MOON", "PRIM_DOUBLE_BOX",
    "PRIMITIVE_NAMES", "PRIMITIVE_BY_NAME", "TEXT_ALPHABET", "TEXT_INDEX",
    "MAX_TEXT_LEN", "MOON_CRATER_POINTS",
    "primitive_to_vectors", "primitive_kind", "clean_primitive_text",
    "primitive_anchor",
    "PaintStyle", "VectorCommand", "Matrix", "IDENTITY",
    "mat_mul", "mat_translate", "mat_scale", "mat_rotate", "apply_mat",
    "is_axis_aligned", "clamp_int",
    "normalize_hex", "color_to_rgba", "rgba_to_hex",
    "RangeEncoder", "RangeDecoder", "BitWriter", "BitReader",
    "predict_point", "update_point_state",
    "rgb565", "alpha4", "from_rgb565", "from_rgb565_a4", "quantize_palette_color",
    "cubic_point", "quad_point", "flatten_path",
    "command_points", "commands_bbox", "transform_command", "translate_command",
    "quantize_command", "validate_command",
]
