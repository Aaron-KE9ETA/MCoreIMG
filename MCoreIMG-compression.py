#!/usr/bin/env python3
"""
MCoreIMG compression — protocol-v5 transport codec
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
5.  Local-space SVG group planning (protocol v5's scale-independent groups).
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
``decode_commands(data) -> (list[VectorCommand], palette)``
    Bitstream layer only, for tests and tooling that bypass framing.

RECORD LAYOUT (protocol 5)
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

import binascii
import copy
import importlib.util
import json
import math
import sys
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

COMPRESSION_BUILD = "2026.08.05-compression-v5.2-MODULAR"


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


# PROTOCOL_VERSION is written into both the bitstream and every frame header.
PROTOCOL_VERSION = 5

# MeshCore transport profile. Keep the arithmetic in one place so GUI counters,
# encoder limits, and decoder validation cannot drift apart.
MAX_MESSAGES = 10
MESSAGE_LEN = 150
FRAME_HEADER_LEN = 15
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

class BitWriter:
    def __init__(self): self.bits: List[int]=[]
    def bit(self,v: int|bool): self.bits.append(1 if v else 0)
    def bits_n(self,v:int,n:int):
        if v<0 or v >= (1<<n): raise MCIError(f"Value {v} does not fit in {n} bits.")
        self.bits.extend((v>>s)&1 for s in range(n-1,-1,-1))
    def ue(self,v:int):
        if v<0: raise MCIError("Unsigned Exp-Golomb cannot encode negative values.")
        n=v+1; width=n.bit_length(); self.bits.extend([0]*(width-1)); self.bits_n(n,width)
    def rice_signed(self,v:int,k:int=3):
        z=(-v*2-1) if v<0 else v*2
        q=z>>k; self.bits.extend([1]*q); self.bits.append(0)
        if k: self.bits_n(z&((1<<k)-1),k)
    def to_bytes(self)->bytes:
        out=bytearray((len(self.bits)+7)//8)
        for i,b in enumerate(self.bits):
            if b: out[i//8]|=1<<(7-i%8)
        return bytes(out)


class BitReader:
    def __init__(self,data:bytes): self.data=data; self.pos=0
    def bit(self)->int:
        if self.pos>=len(self.data)*8: raise MCIError("Unexpected end of stream.")
        b=(self.data[self.pos//8]>>(7-self.pos%8))&1; self.pos+=1; return b
    def bits_n(self,n:int)->int:
        v=0
        for _ in range(n): v=(v<<1)|self.bit()
        return v
    def ue(self,max_value:int=10_000_000)->int:
        z=0
        while self.bit()==0:
            z+=1
            if z>31: raise MCIError("Exp-Golomb prefix too long.")
        v=(1<<z)+(self.bits_n(z) if z else 0)-1
        if v>max_value: raise MCIError("Exp-Golomb value exceeds limit.")
        return v
    def rice_signed(self,k:int=3,max_abs:int=4096)->int:
        q=0
        while self.bit()==1:
            q+=1
            if q>8192: raise MCIError("Rice quotient too long.")
        z=(q<<k)|(self.bits_n(k) if k else 0)
        v=-(z//2)-1 if z&1 else z//2
        if abs(v)>max_abs: raise MCIError("Rice value exceeds limit.")
        return v


@dataclass
class PointState:
    initialized: bool=False
    x:int=0
    y:int=0


def rice_signed_length(v:int,k:int=3)->int:
    z=(-v*2-1) if v<0 else v*2
    return (z>>k)+1+k


def write_point(w:BitWriter,state:PointState,p:Tuple[int,int]):
    x,y=p
    use_delta=state.initialized and (1+rice_signed_length(x-state.x)+rice_signed_length(y-state.y) <= 20)
    w.bit(use_delta)
    if use_delta:
        w.rice_signed(x-state.x); w.rice_signed(y-state.y)
    else:
        w.bits_n(x,10); w.bits_n(y,9)
    state.initialized=True; state.x=x; state.y=y


def read_point(r:BitReader,state:PointState)->Tuple[int,int]:
    if r.bit():
        if not state.initialized: raise MCIError("Delta point before absolute point.")
        x=state.x+r.rice_signed(max_abs=1440); y=state.y+r.rice_signed(max_abs=960)
    else:
        x=r.bits_n(10); y=r.bits_n(9)
    if not (0<=x<CANVAS_W and 0<=y<CANVAS_H): raise MCIError(f"Point outside canvas: {x},{y}")
    state.initialized=True; state.x=x; state.y=y
    return x,y

# ==========================================================================
# Style records
# ==========================================================================

def style_key(style:PaintStyle,palette:Sequence[str])->Tuple[Any,...]:
    def idx(c): return palette.index(quantize_palette_color(c)) if c else -1
    return idx(style.fill),idx(style.stroke),max(1,min(64,int(round(style.stroke_width)))) if style.stroke else 0,style.fill_rule


def write_style(w:BitWriter,style:PaintStyle,palette:Sequence[str]):
    fill=style.fill is not None; stroke=style.stroke is not None
    w.bit(fill); w.bit(stroke); w.bit(style.fill_rule=="evenodd")
    width=max(1,(len(palette)-1).bit_length())
    if fill: w.bits_n(palette.index(quantize_palette_color(style.fill)),width)
    if stroke:
        w.bits_n(palette.index(quantize_palette_color(style.stroke)),width)
        w.ue(max(1,min(64,int(round(style.stroke_width))))-1)


def read_style(r:BitReader,palette:Sequence[str])->PaintStyle:
    has_fill=bool(r.bit()); has_stroke=bool(r.bit()); even=bool(r.bit())
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
    w.bit(same)
    if not same:
        w.bits_n(value, width)
        params[key] = value


def _read_stateful_fixed(r: BitReader, state: PointState, key: str, width: int) -> int:
    params = _state_params(state)
    if r.bit():
        if key not in params:
            raise MCIError(f"Primitive state {key!r} reused before initialization.")
        return params[key]
    value = r.bits_n(width)
    params[key] = value
    return value


def _write_stateful_ue(w: BitWriter, state: PointState, key: str, value: int) -> None:
    params = _state_params(state)
    same = params.get(key) == value
    w.bit(same)
    if not same:
        w.ue(value)
        params[key] = value


def _read_stateful_ue(r: BitReader, state: PointState, key: str, max_value: int) -> int:
    params = _state_params(state)
    if r.bit():
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
                w.bits_n(int(segment["op"]), 3)
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
                operation = r.bits_n(3)
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
    w.bits_n(REC_NORMAL, 2)
    same_opcode = state.previous_opcode == cmd.opcode
    w.bit(same_opcode)
    if not same_opcode:
        w.bits_n(cmd.opcode, 3)
    key = style_key(cmd.style, palette)
    same_style = state.style_states.get(cmd.opcode) == key
    w.bit(same_style)
    if not same_style:
        write_style(w, cmd.style, palette)
        state.style_states[cmd.opcode] = key
    write_geometry(w, cmd, state.point_states[cmd.opcode], palette)
    _update_history(state, [cmd])


def _write_single_repeat_record(w: BitWriter, state: EncoderState, cmd: VectorCommand, delta: Tuple[int, int]) -> None:
    w.bits_n(REC_SINGLE_REPEAT, 2)
    w.bits_n(cmd.opcode, 3)
    w.rice_signed(delta[0], 2)
    w.rice_signed(delta[1], 2)
    _update_history(state, [cmd])


def _best_single_or_normal(cmd: VectorCommand, state: EncoderState, palette: Sequence[str]) -> Tuple[BitWriter, EncoderState, bool]:
    normal_writer = BitWriter(); normal_state = state.clone()
    _write_normal_record(normal_writer, normal_state, cmd, palette)
    repeat_writer: Optional[BitWriter] = None
    repeat_state: Optional[EncoderState] = None
    reference = state.recent.get(cmd.opcode)
    delta = geom_translation(reference, cmd) if reference is not None else None
    if delta is not None:
        repeat_writer = BitWriter(); repeat_state = state.clone()
        _write_single_repeat_record(repeat_writer, repeat_state, cmd, delta)
    if repeat_writer is not None and len(repeat_writer.bits) < len(normal_writer.bits):
        assert repeat_state is not None
        return repeat_writer, repeat_state, True
    return normal_writer, normal_state, False


def _simulate_sequence(sequence: Sequence[VectorCommand], state: EncoderState, palette: Sequence[str]) -> Tuple[int, EncoderState, int]:
    current = state.clone(); bits = 0; repeats = 0
    for command in sequence:
        writer, current, used_repeat = _best_single_or_normal(command, current, palette)
        bits += len(writer.bits)
        repeats += int(used_repeat)
    return bits, current, repeats


def _command_invariant_signature(cmd: VectorCommand) -> str:
    anchor = primitive_anchor(cmd) if cmd.opcode == OP_PRIMITIVE else (command_points(cmd)[0] if command_points(cmd) else (0, 0))
    shifted = quantize_command(translate_command(cmd, -int(round(anchor[0])), -int(round(anchor[1]))))
    return json.dumps({"op": shifted.opcode, "style": shifted.style.to_json(), "geom": shifted.geom}, sort_keys=True, separators=(",", ":"))


def _write_group_record(w: BitWriter, state: EncoderState, source_distance: int, length: int,
                        delta: Tuple[int, int], generated: Sequence[VectorCommand]) -> None:
    w.bits_n(REC_GROUP_REPEAT, 2)
    w.ue(length - 2)
    adjacent = source_distance == length
    w.bit(adjacent)
    if not adjacent:
        # A nonoverlapping source is always at least `length` commands back.
        w.ue(source_distance - length)
    w.rice_signed(delta[0], 2)
    w.rice_signed(delta[1], 2)
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
        saved = baseline_bits - len(group_writer.bits)
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
    delta_bits = 1 + rice_signed_length(x - state.x, 3) + rice_signed_length(y - state.y, 3) if state.initialized else 10**9
    use_delta = state.initialized and delta_bits <= absolute_bits
    w.bit(use_delta)
    if use_delta:
        w.rice_signed(x - state.x, 3)
        w.rice_signed(y - state.y, 3)
    else:
        w.bits_n(x, x_bits)
        w.bits_n(y, y_bits)
    state.initialized = True
    state.x, state.y = x, y


def _read_local_point(
    r: BitReader, state: PointState, local_width: int, local_height: int,
) -> Tuple[int, int]:
    """Inverse of _write_local_point with strict local-box validation."""
    if r.bit():
        if not state.initialized:
            raise MCIError("Local delta point before initialization.")
        x = state.x + r.rice_signed(3, 1024)
        y = state.y + r.rice_signed(3, 1024)
    else:
        x = r.bits_n(_local_bits(local_width))
        y = r.bits_n(_local_bits(local_height))
    if not (0 <= x <= local_width and 0 <= y <= local_height):
        raise MCIError(f"Point outside local SVG group: {x},{y}.")
    state.initialized = True
    state.x, state.y = x, y
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
            w.bits_n(int(segment["op"]), 3)
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
            operation = r.bits_n(3)
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
    w.bits_n(REC_NORMAL, 2)
    same_opcode = state.previous_opcode == command.opcode
    w.bit(same_opcode)
    if not same_opcode:
        w.bits_n(command.opcode, 3)
    key = style_key(command.style, palette)
    same_style = state.style_states.get(command.opcode) == key
    w.bit(same_style)
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
    if len(repeated.bits) < len(normal.bits):
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
        w.bits.extend(record.bits)


def _decode_regular_record(r: BitReader, palette: Sequence[str], state: DecoderState, local_width: int, local_height: int) -> VectorCommand:
    record_type = r.bits_n(2)
    if record_type == REC_NORMAL:
        same = bool(r.bit())
        if same:
            if state.previous_opcode is None:
                raise MCIError("Same opcode before initialization.")
            opcode = state.previous_opcode
        else:
            opcode = r.bits_n(3)
        if opcode not in OP_NAMES:
            raise MCIError("Invalid opcode.")
        same_style = bool(r.bit())
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
        opcode = r.bits_n(3)
        if opcode not in state.recent:
            raise MCIError("Repeat references missing opcode history.")
        command = quantize_command(
            translate_command(state.recent[opcode], r.rice_signed(2, 719), r.rice_signed(2, 479))
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
    palette: List[str]

# ==========================================================================
# Stream codec
# ==========================================================================

def encode_commands(
    commands: Sequence[VectorCommand],
    local_extent: int = LOCAL_GROUP_EXTENT,
) -> Tuple[bytes, int, Dict[str, int], List[str]]:
    """Compress commands into the protocol-v5 bitstream.

    ``local_extent`` selects the normalized coordinate range used for
    local-space SVG groups. Higher values keep more shape fidelity and cost
    more bits; :func:`encode_image` searches this space automatically.

    Returns ``(payload_bytes, bit_count, metrics, palette)``.
    """
    raw_source = [command.clone() for command in commands]
    palette = build_palette(raw_source)
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
            w.bits_n(REC_TRANSFORM_GROUP, 2)
            definition_index = definition_indices.get(plan.signature)
            is_reference = definition_index is not None
            w.bit(is_reference)
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
        w.bits.extend(record.bits)
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
    return w.to_bytes(), len(w.bits), metrics, palette


def decode_commands(data: bytes) -> Tuple[List[VectorCommand], List[str]]:
    r = BitReader(data)
    version = r.bits_n(4)
    if version != PROTOCOL_VERSION:
        raise MCIError(f"Unsupported MCoreIMG protocol version {version}; expected {PROTOCOL_VERSION}.")
    palette = [from_rgb565_a4(r.bits_n(16), r.bits_n(4)) for _ in range(r.ue(MAX_PALETTE - 1) + 1)]
    output_count = r.ue(MAX_COMMANDS)
    state = DecoderState()
    result: List[VectorCommand] = []
    definitions: List[Tuple[List[VectorCommand], int, int]] = []

    while len(result) < output_count:
        record_type = r.bits_n(2)
        if record_type == REC_NORMAL:
            # The helper expects to consume the record type itself. Recreate a
            # tiny reader prefix by decoding this normal record inline.
            same = bool(r.bit())
            if same:
                if state.previous_opcode is None:
                    raise MCIError("Same opcode before initialization.")
                opcode = state.previous_opcode
            else:
                opcode = r.bits_n(3)
            if opcode not in OP_NAMES:
                raise MCIError("Invalid opcode.")
            same_style = bool(r.bit())
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
            opcode = r.bits_n(3)
            if opcode not in state.recent:
                raise MCIError("Repeat references missing opcode history.")
            command = quantize_command(
                translate_command(state.recent[opcode], r.rice_signed(2, 719), r.rice_signed(2, 479))
            )
            generated = [command]
        elif record_type == REC_GROUP_REPEAT:
            length = r.ue(MAX_COMMANDS - 2) + 2
            adjacent = bool(r.bit())
            distance = length if adjacent else length + r.ue(MAX_COMMANDS - length)
            dx, dy = r.rice_signed(2, 719), r.rice_signed(2, 479)
            source_start = len(result) - distance
            if source_start < 0 or source_start + length > len(result):
                raise MCIError("Group-copy reference is outside decoded history.")
            generated = [
                quantize_command(translate_command(result[source_start + offset], dx, dy))
                for offset in range(length)
            ]
        elif record_type == REC_TRANSFORM_GROUP:
            is_reference = bool(r.bit())
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


def frame_crc(payload:str)->int:
    return binascii.crc_hqx(payload.encode("ascii"),0xFFFF)

# ==========================================================================
# Image layer
# ==========================================================================

def _encode_image_once(commands: Sequence[VectorCommand], local_extent: int) -> EncodedImage:
    """Encode once at a particular local-coordinate precision.

    The precision is a plain argument, so this function is re-entrant and safe
    to call concurrently or from a library context.
    """
    bit_bytes, bit_count, metrics, palette = encode_commands(commands, int(local_extent))
    raw = bit_bytes + zlib.crc32(bit_bytes).to_bytes(4, "big")
    payload = base91_encode(raw)
    image_id = enc62(zlib.crc32(raw) % (62 ** 3), 3)
    chunks = [payload[i:i + FRAME_PAYLOAD_LEN] for i in range(0, len(payload), FRAME_PAYLOAD_LEN)] or [""]
    total = len(chunks)
    frames = []
    for index, chunk in enumerate(chunks):
        header = FRAME_MAGIC + enc62(PROTOCOL_VERSION, 1) + image_id + enc62(index, 1) + enc62(total, 1) + enc62(len(chunk), 2) + enc62(frame_crc(chunk), 3) + "0"
        frames.append(header + chunk)
    stats = CodecStats(
        metrics["transport_commands"], len(palette), bit_count, len(raw), len(payload), total,
        metrics["single_repeats"] + metrics["group_repeats"], metrics["group_repeats"],
        metrics["primitive_count"], metrics["vectorized_primitive_count"], metrics["source_commands"],
        metrics["transformed_groups"], metrics["transformed_references"], int(local_extent),
    )
    return EncodedImage(raw, payload, frames, stats, image_id, palette)


def encode_image(commands: Sequence[VectorCommand]) -> EncodedImage:
    """Encode with the highest local-space precision that fits ten messages.

    Images without eligible local groups take the direct path. For local SVG
    groups, the precision ladder trades coordinate fidelity for payload size.
    The first fitting candidate wins; if none fit, the smallest/last candidate
    is returned so the GUI can report an honest over-limit result.
    """
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


def decode_frames(frames:Sequence[str])->List[VectorCommand]:
    parts={};expected_total=None;image_id=None
    for frame in frames:
        frame=frame.strip()
        if len(frame)<FRAME_HEADER_LEN or len(frame)>MESSAGE_LEN: raise FrameError("Invalid frame length.")
        h,p=frame[:FRAME_HEADER_LEN],frame[FRAME_HEADER_LEN:]
        if h[:3]!=FRAME_MAGIC or dec62(h[3])!=PROTOCOL_VERSION: raise FrameError("Wrong frame magic/version.")
        iid=h[4:7];idx=dec62(h[7]);total=dec62(h[8]);length=dec62(h[9:11]);crc=dec62(h[11:14])
        if total < 1 or total > MAX_MESSAGES:
            raise FrameError(f"Frame set declares {total} parts; maximum is {MAX_MESSAGES}.")
        if idx >= total:
            raise FrameError("Frame index is outside the declared frame count.")
        if length!=len(p) or crc!=frame_crc(p): raise FrameError("Frame length or CRC mismatch.")
        if image_id is None: image_id=iid;expected_total=total
        if iid!=image_id or total!=expected_total: raise FrameError("Mixed image frames.")
        if idx in parts and parts[idx]!=p: raise FrameError("Conflicting duplicate frame.")
        parts[idx]=p
    if expected_total is None or set(parts)!=set(range(expected_total)): raise FrameError("Missing frame parts.")
    raw=base91_decode("".join(parts[i] for i in range(expected_total)))
    if len(raw)<4: raise FrameError("Stream too short.")
    data,crc=raw[:-4],int.from_bytes(raw[-4:],"big")
    if zlib.crc32(data)!=crc: raise FrameError("Stream CRC-32 mismatch.")
    return decode_commands(data)[0]


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
    "MAX_PAYLOAD_CHARS", "FRAME_MAGIC", "MAX_COMMANDS", "MAX_PALETTE",
    "BASE62", "BASE91", "BASE91_INDEX",
    # record tags and precision
    "REC_NORMAL", "REC_SINGLE_REPEAT", "REC_GROUP_REPEAT", "REC_TRANSFORM_GROUP",
    "LOCAL_GROUP_EXTENT", "PRECISION_LADDER",
    # errors
    "FrameError",
    "MCIError",                                                     # (model)
    # palette and transport quantization
    "build_palette", "geom_translation",
    # results
    "CodecStats", "EncodedImage",
    # codec entry points
    "encode_image", "decode_frames", "encode_commands", "decode_commands",
    # framing primitives (used by frame-repair tooling)
    "base91_encode", "base91_decode", "enc62", "dec62", "frame_crc",
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
    "rgb565", "alpha4", "from_rgb565", "from_rgb565_a4", "quantize_palette_color",
    "cubic_point", "quad_point", "flatten_path",
    "command_points", "commands_bbox", "transform_command", "translate_command",
    "quantize_command", "validate_command",
]