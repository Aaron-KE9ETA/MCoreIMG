#!/usr/bin/env python3
from __future__ import annotations
"""
MCoreIMG Hybrid Constructor — protocol-v5 local-space SVG + old drawing branch
===============================================================================

This file is both the GUI Constructor and the reference encoder for MCoreIMG
protocol version 5.  It imports a practical SVG subset, combines imported SVGs
with compact legacy drawing primitives, previews the transport-visible result,
and emits at most ten 150-character MeshCore messages.

The comments in this build are intentionally extensive.  They are aimed at a
future maintainer who needs to reconcile the technical debt created while the
protocol evolved rapidly from the original vector-only branch through the
hybrid primitive branch and finally into local-space SVG groups.


MAINTAINER ORIENTATION
----------------------

The most important fact about this source file is that it contains several
historical implementation layers.  Python resolves a global name at runtime,
and a later ``def`` or ``class`` statement replaces an earlier object with the
same name.  Therefore, the last definition of ``encode_commands``,
``decode_commands``, ``encode_image``, ``CodecStats``, ``ConstructorApp``, and
``run_self_test`` is the active implementation.

That layering is deliberate for now because it allowed the branch to retain a
working foundation while new protocol experiments were added.  It is also the
largest remaining source of technical debt.  Do not edit the first function
with a familiar name and assume it is live.  Search from the bottom upward or
use ``inspect.getsource``/``__qualname__`` while debugging.

Approximate source map:

1. CORE VECTOR/SVG FOUNDATION
   - Data model, matrix math, SVG parsing, path normalization, renderer,
     original stateful point codec, Base91 framing, and the first GUI.
   - Some of these definitions remain active helpers; some are superseded.

2. HYBRID PRIMITIVE / REPEAT FOUNDATION
   - Adds ``OP_PRIMITIVE`` and the legacy/manual shapes.
   - Adds exact primitive-versus-vector bit-cost comparison.
   - Adds single-command and translated contiguous-group references.
   - Redefines several codec and geometry helpers.

3. PROTOCOL-V5 LOCAL-SPACE SVG LAYER
   - Adds ``REC_TRANSFORM_GROUP``.
   - Normalizes a contiguous imported SVG group into a stable local box.
   - Sends a fixed-width display box separately from the local geometry.
   - Reuses identical local definitions for copied SVG instances.
   - Redefines the active codec and image encoder.

4. HYBRID GUI LAYER
   - Adds multi-SVG import and the Old Drawing Mode.
   - Adds editor-only grouping for imported SVGs.

5. FINAL DIRECT-EDITING / UNDO GUI LAYER
   - The last ``ConstructorApp`` is the class instantiated by ``main``.
   - Adds drag-to-move, drag-to-scale, color selection, duplication, and undo.

6. FINAL REGRESSION TESTS AND ENTRY POINT
   - The last ``run_self_test`` is authoritative.
   - ``verify_build_integrity`` guards the required feature set before startup.


END-TO-END DATA FLOW
--------------------

The normal path from source file to radio messages is:

    SVG/XML or editable JSON
        -> VectorDocument
        -> ordered VectorCommand objects
        -> editor/document transforms
        -> primitive representation planning
        -> local-space SVG group planning
        -> palette construction (RGB565 + 4-bit alpha)
        -> record selection and bit writing
        -> stream CRC-32
        -> Base91
        -> 1..10 framed MeshCore messages, each with CRC-16

The preview and PNG export intentionally decode the encoded stream when the
image fits.  This is a major invariant: the GUI should show the result the
Reconstructor will receive, including palette quantization, local-coordinate
precision, alpha quantization, and command expansion.


COORDINATE SPACES
-----------------

There are three coordinate spaces.  Confusing them previously caused the bug
where a smaller displayed SVG required fewer messages.

1. SVG SOURCE SPACE
   Coordinates from the SVG's viewBox before import normalization.

2. CANVAS / DISPLAY SPACE
   The fixed 720 x 480 editor and reconstructed image space.  Generic vector
   commands and compact primitives ultimately render here.

3. LOCAL GROUP SPACE
   A normalized integer coordinate box used only for protocol-v5 imported SVG
   groups.  The geometry is stable when the user drags or scales the group.
   A fixed-width transform box carries x, y, width, and height separately.

The defining invariant is:

    changing only a local SVG group's displayed position or displayed size
    must not change the encoded local geometry bit count.

A small Base91 length variation may still occur because byte padding and CRC
values alter the final text representation, but the underlying geometry bits
and frame count should remain stable.


TRANSPORT RECORD TYPES
----------------------

Every command-stream record begins with two bits:

    REC_NORMAL          regular opcode/style/geometry record
    REC_SINGLE_REPEAT   repeat the most recent command of one opcode + dx/dy
    REC_GROUP_REPEAT    repeat an earlier contiguous command range + dx/dy
    REC_TRANSFORM_GROUP protocol-v5 local SVG definition or definition ref

``REC_TRANSFORM_GROUP`` reuses the old reserved record value.  A transform
record carries a display box and either:

- a new local command definition, or
- an index referring to an identical earlier local definition.

Definitions are stream-local.  Their numeric index is meaningful only while
decoding one image and must never be persisted as editor metadata.


COMPRESSION DECISION ORDER
--------------------------

Compression is not selected from fixed estimates.  The encoder simulates real
bitstreams and chooses representations based on actual bit count in context.
The current order is roughly:

1. For every manual primitive, compare the compact primitive record with its
   deterministic generic-vector expansion.  Keep the smaller representation.
2. Detect contiguous editor groups eligible for local-space SVG records.
3. Reuse an identical earlier local definition when possible.
4. For non-local commands, test translated contiguous-group references.
5. Otherwise compare a single-opcode repeat with a normal record.

Because codec state affects cost, changing this order can change compression.
Any reordering needs regression images, not only isolated unit tests.


EDITOR METADATA VERSUS TRANSPORT DATA
-------------------------------------

``VectorCommand.editor_group`` exists only to make imported SVGs behave as one
object in the GUI and to identify contiguous local-space candidates.  It is
saved in editable source JSON, but is not itself transmitted as an image
field.  The encoder infers records from command order and group boundaries.

Important group assumptions:

- Commands belonging to one imported SVG should remain contiguous.
- Layer reordering can split a group and therefore disable local-group reuse.
- A primitive is intentionally excluded from local SVG groups.
- Direct editing may bake the document-level transform so object manipulation
  happens in one predictable canvas coordinate system.


COLOR AND ALPHA
---------------

Colors are normalized to ``#RRGGBB`` or ``#RRGGBBAA`` strings.  Transmission
uses RGB565 plus A4:

- 16 bits for red/green/blue (5/6/5)
- 4 bits for alpha (0..15)

The renderer uses source-over compositing in draw order.  Never pre-blend a
transparent SVG color against white during import; doing so destroys overlap
information.  Color chooser operations preserve the existing alpha nibble
when only RGB is changed.


FRAME ENVELOPE
--------------

The message profile is intentionally separate from the vector codec:

- maximum messages: 10
- message length: 150 characters
- frame header: 15 characters
- maximum payload text: 1,350 Base91 characters
- per-frame CRC: CRC-16 over the text chunk
- stream CRC: CRC-32 over encoded bytes

The frame header also carries protocol version, image ID, part index, total
parts, and chunk length.  A decoder must reject mixed image IDs, duplicate or
missing parts, invalid lengths, unsupported versions, and either CRC failure.


ERROR-HANDLING PRINCIPLES
-------------------------

``MCIError`` means valid program flow reached invalid MCoreIMG data or editor
state.  ``SVGImportError`` adds source-import context.  ``FrameError`` means
the text envelope is damaged or inconsistent.

Decoder limits are security and reliability boundaries, not conveniences.
Keep maximum counts and coordinate bounds when refactoring.  A corrupt radio
message must fail quickly rather than allocate an unbounded structure.


TESTING EXPECTATIONS
--------------------

Before distributing a modified Constructor, run:

    python <file>.py --version
    python <file>.py --self-test

The final self-test covers at least:

- protocol and frame-profile constants
- encode/decode round trips
- alpha preservation and compositing
- full ten-message framing and corruption rejection
- primitive-versus-vector decisions
- translated group-copy records
- local-space scaling invariance
- copied local-definition references
- required GUI feature presence through ``verify_build_integrity``

For UI work, also launch the application and manually check:

- import one SVG and several SVGs
- drag a whole imported group
- scale with the blue handle
- draw and color a primitive
- undo the import and drawing action
- preview frames and export PNG


KNOWN TECHNICAL DEBT
--------------------

1. DUPLICATE DEFINITIONS
   The file should eventually be split into modules and each active symbol
   should have one definition.  Until then, comments marked ``ACTIVE V5`` and
   ``SUPERSEDED FOUNDATION`` describe which layer wins.

2. GLOBAL LOCAL_GROUP_EXTENT
   ``_encode_image_once_v5`` temporarily mutates this global to test precision
   candidates.  The GUI is single-threaded, so it is currently safe, but this
   is not reentrant or thread-safe.  Pass an explicit codec-options object in a
   future cleanup.

3. DICTIONARY-SHAPED GEOMETRY
   ``VectorCommand.geom`` is flexible but weakly typed.  Dedicated dataclasses
   or tagged immutable records would move many runtime checks to type checking.

4. GUI INHERITANCE STACK
   The final ConstructorApp subclasses an earlier ConstructorApp that already
   subclasses the base GUI.  This preserves behavior but obscures method
   resolution.  Collapse it after protocol work stabilizes.

5. IMPORT AND PROTOCOL COUPLING
   SVG normalization, editor grouping, and transport planning live in one file.
   Separating importer, model, optimizer, codec, renderer, and GUI would make
   tests smaller and protocol compatibility easier to reason about.

6. GREEDY RECORD PLANNING
   The encoder performs local comparisons and bounded group searches rather
   than a global dynamic-programming optimum.  It is practical for the message
   budget, but future changes should preserve deterministic runtime limits.

7. RASTER PREVIEW COST
   The GUI re-encodes and decodes frequently to preserve preview parity.  Large
   SVGs can make dragging expensive.  A debounced preview or cached local
   definitions would improve responsiveness without changing transport data.


SAFE REFACTORING ORDER
----------------------

A lower-risk cleanup sequence is:

1. Freeze protocol-v5 regression fixtures and expected decoded command lists.
2. Extract pure data classes and matrix/color helpers.
3. Extract SVG import and rendering.
4. Extract bitstream/framing utilities.
5. Move the active v5 encoder/decoder only; do not carry superseded functions.
6. Replace editor inheritance layers with one GUI class.
7. Replace mutable globals with an explicit CodecConfig.
8. Remove the historical definitions only after all fixtures match.

Do not combine a structural cleanup with a protocol-format change.  Those are
two separate review problems and should be committed separately.


Run:
    python MCoreIMG-SVG-Constructor-v5.1-DOCUMENTED.py

Self-test:
    python MCoreIMG-SVG-Constructor-v5.1-DOCUMENTED.py --self-test

Arch Linux dependencies:
    sudo pacman -Syu python tk python-pillow
"""

import argparse
import binascii
import copy
import gzip
import json
import math
import re
import subprocess
import tkinter as tk
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox, simpledialog, ttk
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple
from xml.etree import ElementTree as ET

try:
    from PIL import Image, ImageColor, ImageDraw, ImageTk
except ImportError:
    Image = None
    ImageColor = None
    ImageDraw = None
    ImageTk = None


# ---------------------------------------------------------------------------
# Protocol and editor constants
# ---------------------------------------------------------------------------

# Canvas dimensions are part of the current protocol profile. Generic vector
# coordinates are validated against these bounds, while local SVG definitions
# use their own normalized coordinate box plus a display transform.
CANVAS_W = 720
CANVAS_H = 480

# BACKGROUND affects preview/export compositing but is not transmitted as an
# explicit command. A future configurable background would need either a
# protocol field or an agreed Reconstructor default.
BACKGROUND = "#FFFFFF"
DEFAULT_MARGIN = 8

# PROTOCOL_VERSION is written into both the bitstream and every frame header.
# SOURCE_VERSION belongs only to editable JSON and may evolve independently.
PROTOCOL_VERSION = 5
SOURCE_FORMAT = "MCoreIMG-SVG-source"
SOURCE_VERSION = 5
CONSTRUCTOR_BUILD = "2026.08.02-svg-v5.1-DOCUMENTED-LOCALSPACE-HYBRID-10MSG"
FEATURE_SIGNATURE = "PROTO5|LOCALSPACE|HYBRID|PRIMITIVES|GROUPCOPY|ALPHA|UNDO|10MSG"

# MeshCore transport profile. Keep the arithmetic expressed in one place so
# GUI counters, encoder limits, and decoder validation cannot drift apart.
MAX_MESSAGES = 10
MESSAGE_LEN = 150
FRAME_HEADER_LEN = 15
FRAME_PAYLOAD_LEN = MESSAGE_LEN - FRAME_HEADER_LEN
MAX_PAYLOAD_CHARS = MAX_MESSAGES * FRAME_PAYLOAD_LEN
FRAME_MAGIC = "MCI"

# Defensive decoder limits. These cap allocation and malformed-count loops.
MAX_COMMANDS = 2048
MAX_PALETTE = 32

BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
BASE91 = "".join(chr(c) for c in range(33, 127) if chr(c) not in {'"', "'", "\\"})
BASE91_INDEX = {ch: i for i, ch in enumerate(BASE91)}
assert len(BASE91) == 91

OP_RECT = 0
OP_ELLIPSE = 1
OP_LINE = 2
OP_POLYLINE = 3
OP_POLYGON = 4
OP_PATH = 5
OP_NAMES = {
    OP_RECT: "Rectangle",
    OP_ELLIPSE: "Ellipse",
    OP_LINE: "Line",
    OP_POLYLINE: "Polyline",
    OP_POLYGON: "Polygon",
    OP_PATH: "Path",
}

SEG_M = 0
SEG_L = 1
SEG_Q = 2
SEG_C = 3
SEG_Z = 4
SEG_NAMES = {SEG_M: "M", SEG_L: "L", SEG_Q: "Q", SEG_C: "C", SEG_Z: "Z"}

CSS_NAMED_FALLBACK = {
    "black": "#000000", "white": "#FFFFFF", "red": "#FF0000",
    "green": "#008000", "blue": "#0000FF", "yellow": "#FFFF00",
    "gray": "#808080", "grey": "#808080", "silver": "#C0C0C0",
    "maroon": "#800000", "purple": "#800080", "fuchsia": "#FF00FF",
    "lime": "#00FF00", "olive": "#808000", "navy": "#000080",
    "teal": "#008080", "aqua": "#00FFFF", "orange": "#FFA500",
    "transparent": "#FFFFFF",
}


class MCIError(ValueError):
    """Base exception for invalid MCoreIMG model, codec, or stream data."""

    pass


class SVGImportError(MCIError):
    """Raised when SVG/XML cannot be normalized into the supported model."""

    pass


class FrameError(MCIError):
    """Raised for damaged, incomplete, mixed, or inconsistent text frames."""

    pass


# ---------------------------------------------------------------------------
# Vector model
# ---------------------------------------------------------------------------


@dataclass
class PaintStyle:
    """Transport-visible paint state shared by generic and primitive commands.

    ``fill`` and ``stroke`` are normalized RGB/RGBA strings or ``None``.
    ``stroke_width`` is a canvas-space width before final quantization.
    ``fill_rule`` is retained because compound SVG paths can require even-odd
    filling to preserve holes.
    """

    fill: Optional[str] = "#000000"
    stroke: Optional[str] = None
    stroke_width: float = 1.0
    fill_rule: str = "nonzero"

    def normalized(self) -> "PaintStyle":
        return PaintStyle(
            normalize_hex(self.fill) if self.fill else None,
            normalize_hex(self.stroke) if self.stroke else None,
            max(0.0, float(self.stroke_width)),
            "evenodd" if str(self.fill_rule).lower() == "evenodd" else "nonzero",
        )

    def to_json(self) -> Dict[str, Any]:
        return {
            "fill": self.fill,
            "stroke": self.stroke,
            "stroke_width": self.stroke_width,
            "fill_rule": self.fill_rule,
        }

    @classmethod
    def from_json(cls, obj: Dict[str, Any]) -> "PaintStyle":
        return cls(
            obj.get("fill"), obj.get("stroke"), float(obj.get("stroke_width", 1.0)),
            str(obj.get("fill_rule", "nonzero")),
        ).normalized()


@dataclass
class VectorCommand:
    """One ordered drawing operation in the editor/intermediate model.

    ``geom`` is opcode-specific. ``editor_group`` is GUI/source metadata used
    to move an imported SVG as one object and to propose local-space groups; it
    is not directly serialized into the radio command stream.
    """

    opcode: int
    style: PaintStyle
    geom: Dict[str, Any]
    label: str = ""
    visible: bool = True
    editor_group: Optional[int] = None

    def clone(self) -> "VectorCommand":
        return copy.deepcopy(self)

    def to_json(self) -> Dict[str, Any]:
        return {
            "opcode": self.opcode,
            "type": OP_NAMES.get(self.opcode, "Unknown"),
            "style": self.style.to_json(),
            "geom": copy.deepcopy(self.geom),
            "label": self.label,
            "visible": self.visible,
            "editor_group": self.editor_group,
        }

    @classmethod
    def from_json(cls, obj: Dict[str, Any]) -> "VectorCommand":
        opcode = int(obj["opcode"])
        if opcode not in OP_NAMES:
            raise MCIError(f"Unknown vector opcode {opcode}.")
        cmd = cls(
            opcode,
            PaintStyle.from_json(dict(obj.get("style", {}))),
            copy.deepcopy(dict(obj.get("geom", {}))),
            str(obj.get("label", "")),
            bool(obj.get("visible", True)),
            obj.get("editor_group"),
        )
        validate_command(cmd)
        return cmd


@dataclass
class VectorDocument:
    """Editable document plus one non-destructive document-level transform.

    Individual object edits normally bake this transform first. Keeping it at
    document level is convenient for Fit/Center operations and source JSON,
    while protocol-v5 local SVG transforms are planned later by the encoder.
    """

    commands: List[VectorCommand] = field(default_factory=list)
    scale: float = 1.0
    offset_x: float = 0.0
    offset_y: float = 0.0
    source_name: str = "Untitled"
    warnings: List[str] = field(default_factory=list)

    def visible_commands(self) -> List[VectorCommand]:
        return [c for c in self.commands if c.visible]

    def transformed_commands(self) -> List[VectorCommand]:
        if not self.commands:
            return []
        bbox = commands_bbox(self.commands)
        cx = (bbox[0] + bbox[2]) / 2.0
        cy = (bbox[1] + bbox[3]) / 2.0
        matrix = mat_mul(
            mat_translate(self.offset_x, self.offset_y),
            mat_mul(mat_translate(cx, cy), mat_mul(mat_scale(self.scale, self.scale), mat_translate(-cx, -cy))),
        )
        return [transform_command(c, matrix) for c in self.commands if c.visible]

    def transformed_bbox(self) -> Tuple[float, float, float, float]:
        cmds = self.transformed_commands()
        return commands_bbox(cmds) if cmds else (0.0, 0.0, 0.0, 0.0)

    def bake_transform(self) -> None:
        self.commands = self.transformed_commands()
        self.scale = 1.0
        self.offset_x = 0.0
        self.offset_y = 0.0

    def to_json(self) -> Dict[str, Any]:
        return {
            "format": SOURCE_FORMAT,
            "version": SOURCE_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "canvas": {"width": CANVAS_W, "height": CANVAS_H},
            "source_name": self.source_name,
            "transform": {
                "scale": self.scale,
                "offset_x": self.offset_x,
                "offset_y": self.offset_y,
            },
            "commands": [c.to_json() for c in self.commands],
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_json(cls, obj: Dict[str, Any]) -> "VectorDocument":
        if obj.get("format") != SOURCE_FORMAT:
            raise MCIError(f"Not a {SOURCE_FORMAT} document.")
        transform = dict(obj.get("transform", {}))
        doc = cls(
            commands=[VectorCommand.from_json(v) for v in obj.get("commands", [])],
            scale=float(transform.get("scale", 1.0)),
            offset_x=float(transform.get("offset_x", 0.0)),
            offset_y=float(transform.get("offset_y", 0.0)),
            source_name=str(obj.get("source_name", "Untitled")),
            warnings=[str(v) for v in obj.get("warnings", [])],
        )
        return doc


@dataclass
class CodecStats:
    command_count: int
    palette_count: int
    bit_count: int
    packed_bytes: int
    base91_chars: int
    frame_count: int
    repeat_count: int

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


# ---------------------------------------------------------------------------
# Affine matrices and geometry helpers
# ---------------------------------------------------------------------------

# SVG affine tuple: (a,b,c,d,e,f), x'=a*x+c*y+e, y'=b*x+d*y+f
Matrix = Tuple[float, float, float, float, float, float]
IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def mat_mul(left: Matrix, right: Matrix) -> Matrix:
    a1, b1, c1, d1, e1, f1 = left
    a2, b2, c2, d2, e2, f2 = right
    return (
        a1*a2 + c1*b2,
        b1*a2 + d1*b2,
        a1*c2 + c1*d2,
        b1*c2 + d1*d2,
        a1*e2 + c1*f2 + e1,
        b1*e2 + d1*f2 + f1,
    )


def mat_translate(x: float, y: float) -> Matrix:
    return (1.0, 0.0, 0.0, 1.0, x, y)


def mat_scale(x: float, y: float) -> Matrix:
    return (x, 0.0, 0.0, y, 0.0, 0.0)


def mat_rotate(degrees: float) -> Matrix:
    r = math.radians(degrees)
    c, s = math.cos(r), math.sin(r)
    return (c, s, -s, c, 0.0, 0.0)


def apply_mat(m: Matrix, p: Tuple[float, float]) -> Tuple[float, float]:
    a, b, c, d, e, f = m
    x, y = p
    return a*x + c*y + e, b*x + d*y + f


def parse_transform(text: Optional[str]) -> Matrix:
    if not text:
        return IDENTITY
    result = IDENTITY
    for name, args_text in re.findall(r"([A-Za-z]+)\s*\(([^)]*)\)", text):
        args = [float(v) for v in re.findall(r"[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?", args_text)]
        name = name.lower()
        if name == "matrix" and len(args) == 6:
            m = tuple(args)  # type: ignore[assignment]
        elif name == "translate" and args:
            m = mat_translate(args[0], args[1] if len(args) > 1 else 0.0)
        elif name == "scale" and args:
            m = mat_scale(args[0], args[1] if len(args) > 1 else args[0])
        elif name == "rotate" and args:
            if len(args) >= 3:
                m = mat_mul(mat_translate(args[1], args[2]), mat_mul(mat_rotate(args[0]), mat_translate(-args[1], -args[2])))
            else:
                m = mat_rotate(args[0])
        elif name == "skewx" and args:
            m = (1.0, 0.0, math.tan(math.radians(args[0])), 1.0, 0.0, 0.0)
        elif name == "skewy" and args:
            m = (1.0, math.tan(math.radians(args[0])), 0.0, 1.0, 0.0, 0.0)
        else:
            continue
        result = mat_mul(result, m)
    return result


def is_axis_aligned(m: Matrix, eps: float = 1e-9) -> bool:
    return abs(m[1]) < eps and abs(m[2]) < eps


def clamp_int(v: float, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(round(v))))


def normalize_hex(value: str) -> str:
    value = value.strip().upper()
    if re.fullmatch(r"#[0-9A-F]{8}", value):
        return value if not value.endswith("FF") else value[:7]
    if re.fullmatch(r"#[0-9A-F]{6}", value):
        return value
    if re.fullmatch(r"#[0-9A-F]{4}", value):
        return "#" + "".join(ch*2 for ch in value[1:])
    if re.fullmatch(r"#[0-9A-F]{3}", value):
        return "#" + "".join(ch*2 for ch in value[1:])
    parsed = parse_color(value)
    return parsed or "#000000"


def color_to_rgba(color: Optional[str]) -> Optional[Tuple[int, int, int, int]]:
    if color is None:
        return None
    value = normalize_hex(color)
    if re.fullmatch(r"#[0-9A-F]{6}", value):
        return int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16), 255
    if re.fullmatch(r"#[0-9A-F]{8}", value):
        return int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16), int(value[7:9], 16)
    return None


def rgba_to_hex(r: int, g: int, b: int, a: int = 255) -> str:
    r = max(0, min(255, int(round(r))))
    g = max(0, min(255, int(round(g))))
    b = max(0, min(255, int(round(b))))
    a = max(0, min(255, int(round(a))))
    return f"#{r:02X}{g:02X}{b:02X}" if a >= 255 else f"#{r:02X}{g:02X}{b:02X}{a:02X}"


def parse_color(value: Optional[str], opacity: float = 1.0) -> Optional[str]:
    if value is None:
        return None
    text = value.strip()
    if not text or text.lower() == "none":
        return None
    if text.lower().startswith("url("):
        return None
    try:
        if ImageColor is not None:
            rgb = ImageColor.getrgb(text)
            if len(rgb) == 4:
                r, g, b, a = rgb
                opacity *= a / 255.0
            else:
                r, g, b = rgb[:3]
                a = 255
        else:
            fallback = CSS_NAMED_FALLBACK.get(text.lower(), text)
            if re.fullmatch(r"#[0-9A-Fa-f]{4}", fallback):
                fallback = "#" + "".join(ch*2 for ch in fallback[1:])
            elif re.fullmatch(r"#[0-9A-Fa-f]{3}", fallback):
                fallback = "#" + "".join(ch*2 for ch in fallback[1:])
            if re.fullmatch(r"#[0-9A-Fa-f]{8}", fallback):
                r, g, b, a = int(fallback[1:3], 16), int(fallback[3:5], 16), int(fallback[5:7], 16), int(fallback[7:9], 16)
            elif re.fullmatch(r"#[0-9A-Fa-f]{6}", fallback):
                r, g, b = int(fallback[1:3], 16), int(fallback[3:5], 16), int(fallback[5:7], 16)
                a = 255
            else:
                return "#000000"
    except Exception:
        return "#000000"
    opacity = max(0.0, min(1.0, opacity))
    a = round(a * opacity)
    return rgba_to_hex(r, g, b, a)


def parse_length(value: Optional[str], default: float = 0.0, reference: float = 1.0) -> float:
    if value is None or value == "":
        return default
    text = value.strip()
    match = re.fullmatch(r"([-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?)\s*([A-Za-z%]*)", text)
    if not match:
        return default
    number = float(match.group(1))
    unit = match.group(2).lower()
    factors = {"": 1.0, "px": 1.0, "pt": 96/72, "pc": 16.0, "in": 96.0, "cm": 96/2.54, "mm": 96/25.4, "q": 96/101.6}
    if unit == "%":
        return number * reference / 100.0
    return number * factors.get(unit, 1.0)


def command_points(cmd: VectorCommand) -> List[Tuple[float, float]]:
    g = cmd.geom
    if cmd.opcode == OP_RECT:
        x, y, w, h = g["x"], g["y"], g["w"], g["h"]
        return [(x, y), (x+w, y), (x+w, y+h), (x, y+h)]
    if cmd.opcode == OP_ELLIPSE:
        cx, cy, rx, ry = g["cx"], g["cy"], g["rx"], g["ry"]
        return [(cx-rx, cy-ry), (cx+rx, cy+ry)]
    if cmd.opcode == OP_LINE:
        return [tuple(g["p1"]), tuple(g["p2"])]
    if cmd.opcode in {OP_POLYLINE, OP_POLYGON}:
        return [tuple(p) for p in g["points"]]
    if cmd.opcode == OP_PATH:
        # A Bezier control point is not necessarily on the visible curve.
        # Bounding paths by raw control points can falsely report artwork as
        # outside the canvas and can make Fit shrink it far too much.  Use the
        # same flattened geometry that the preview/export renderer displays.
        points: List[Tuple[float, float]] = []
        for subpath, _closed in flatten_path(g["segments"], 24):
            points.extend(subpath)
        return points
    return []


def commands_bbox(commands: Sequence[VectorCommand]) -> Tuple[float, float, float, float]:
    points = [p for c in commands for p in command_points(c)]
    if not points:
        return 0.0, 0.0, 0.0, 0.0
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    # Include half stroke width in the bounds.
    pad = max((c.style.stroke_width/2 for c in commands if c.style.stroke), default=0.0)
    return min(xs)-pad, min(ys)-pad, max(xs)+pad, max(ys)+pad


def transform_command(cmd: VectorCommand, m: Matrix) -> VectorCommand:
    out = cmd.clone()
    a, b, c, d, _e, _f = m
    sx = math.hypot(a, b)
    sy = math.hypot(c, d)
    stroke_scale = (sx + sy) / 2.0
    out.style.stroke_width *= max(stroke_scale, 1e-9)
    g = out.geom
    if cmd.opcode == OP_RECT:
        corners = command_points(cmd)
        mapped = [apply_mat(m, p) for p in corners]
        if is_axis_aligned(m):
            xs, ys = [p[0] for p in mapped], [p[1] for p in mapped]
            out.geom = {"x": min(xs), "y": min(ys), "w": max(xs)-min(xs), "h": max(ys)-min(ys)}
        else:
            out.opcode = OP_POLYGON
            out.geom = {"points": mapped}
    elif cmd.opcode == OP_ELLIPSE:
        cx, cy, rx, ry = g["cx"], g["cy"], g["rx"], g["ry"]
        if is_axis_aligned(m):
            center = apply_mat(m, (cx, cy))
            out.geom = {"cx": center[0], "cy": center[1], "rx": abs(rx*a), "ry": abs(ry*d)}
        else:
            k = 0.5522847498307936
            raw = [
                {"op": SEG_M, "points": [(cx+rx, cy)]},
                {"op": SEG_C, "points": [(cx+rx, cy+k*ry), (cx+k*rx, cy+ry), (cx, cy+ry)]},
                {"op": SEG_C, "points": [(cx-k*rx, cy+ry), (cx-rx, cy+k*ry), (cx-rx, cy)]},
                {"op": SEG_C, "points": [(cx-rx, cy-k*ry), (cx-k*rx, cy-ry), (cx, cy-ry)]},
                {"op": SEG_C, "points": [(cx+k*rx, cy-ry), (cx+rx, cy-k*ry), (cx+rx, cy)]},
                {"op": SEG_Z, "points": []},
            ]
            out.opcode = OP_PATH
            out.geom = {"segments": [{"op": s["op"], "points": [apply_mat(m, p) for p in s["points"]]} for s in raw]}
    elif cmd.opcode == OP_LINE:
        out.geom = {"p1": apply_mat(m, tuple(g["p1"])), "p2": apply_mat(m, tuple(g["p2"]))}
    elif cmd.opcode in {OP_POLYLINE, OP_POLYGON}:
        out.geom = {"points": [apply_mat(m, tuple(p)) for p in g["points"]]}
    elif cmd.opcode == OP_PATH:
        out.geom = {"segments": [
            {"op": int(seg["op"]), "points": [apply_mat(m, tuple(p)) for p in seg.get("points", [])]}
            for seg in g["segments"]
        ]}
    return out


def quantize_command(cmd: VectorCommand) -> VectorCommand:
    out = cmd.clone()
    out.style.stroke_width = max(1, min(64, int(round(out.style.stroke_width)))) if out.style.stroke else 0
    g = out.geom
    def qp(p: Tuple[float, float]) -> Tuple[int, int]:
        return clamp_int(p[0], 0, CANVAS_W-1), clamp_int(p[1], 0, CANVAS_H-1)
    if out.opcode == OP_RECT:
        x1, y1 = qp((g["x"], g["y"]))
        x2, y2 = qp((g["x"]+g["w"], g["y"]+g["h"]))
        out.geom = {"x": min(x1, x2), "y": min(y1, y2), "w": max(1, abs(x2-x1)), "h": max(1, abs(y2-y1))}
    elif out.opcode == OP_ELLIPSE:
        cx, cy = qp((g["cx"], g["cy"]))
        out.geom = {"cx": cx, "cy": cy, "rx": max(1, min(719, int(round(abs(g["rx"]))))), "ry": max(1, min(479, int(round(abs(g["ry"])))))}
    elif out.opcode == OP_LINE:
        out.geom = {"p1": qp(tuple(g["p1"])), "p2": qp(tuple(g["p2"]))}
    elif out.opcode in {OP_POLYLINE, OP_POLYGON}:
        out.geom = {"points": [qp(tuple(p)) for p in g["points"]]}
    elif out.opcode == OP_PATH:
        out.geom = {"segments": [{"op": int(s["op"]), "points": [qp(tuple(p)) for p in s.get("points", [])]} for s in g["segments"]]}
    validate_command(out)
    return out


def validate_command(cmd: VectorCommand) -> None:
    if cmd.opcode not in OP_NAMES:
        raise MCIError(f"Invalid opcode {cmd.opcode}.")
    style = cmd.style.normalized()
    if style.fill is None and style.stroke is None:
        raise MCIError("A command must have a fill or stroke.")
    g = cmd.geom
    if cmd.opcode == OP_RECT:
        if float(g.get("w", 0)) <= 0 or float(g.get("h", 0)) <= 0:
            raise MCIError("Rectangle width and height must be positive.")
    elif cmd.opcode == OP_ELLIPSE:
        if float(g.get("rx", 0)) <= 0 or float(g.get("ry", 0)) <= 0:
            raise MCIError("Ellipse radii must be positive.")
    elif cmd.opcode == OP_LINE:
        if "p1" not in g or "p2" not in g:
            raise MCIError("Line requires p1 and p2.")
    elif cmd.opcode in {OP_POLYLINE, OP_POLYGON}:
        minimum = 2 if cmd.opcode == OP_POLYLINE else 3
        if len(g.get("points", [])) < minimum:
            raise MCIError(f"{OP_NAMES[cmd.opcode]} requires at least {minimum} points.")
    elif cmd.opcode == OP_PATH:
        segments = g.get("segments", [])
        if not segments or segments[0].get("op") != SEG_M:
            raise MCIError("Path must begin with MoveTo.")
        for seg in segments:
            op = int(seg.get("op", -1))
            expected = {SEG_M: 1, SEG_L: 1, SEG_Q: 2, SEG_C: 3, SEG_Z: 0}.get(op)
            if expected is None or len(seg.get("points", [])) != expected:
                raise MCIError("Malformed path segment.")


# ---------------------------------------------------------------------------
# SVG path parser
# ---------------------------------------------------------------------------

PATH_TOKEN_RE = re.compile(r"[AaCcHhLlMmQqSsTtVvZz]|[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?")
PATH_PARAM_COUNTS = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "T": 2, "A": 7, "Z": 0}


def arc_to_points(start: Tuple[float, float], rx: float, ry: float, rotation: float,
                  large_arc: int, sweep: int, end: Tuple[float, float]) -> List[Tuple[float, float]]:
    x1, y1 = start
    x2, y2 = end
    rx, ry = abs(rx), abs(ry)
    if rx == 0 or ry == 0 or (abs(x1-x2) < 1e-12 and abs(y1-y2) < 1e-12):
        return [end]
    phi = math.radians(rotation % 360.0)
    cp, sp = math.cos(phi), math.sin(phi)
    dx2, dy2 = (x1-x2)/2.0, (y1-y2)/2.0
    xp = cp*dx2 + sp*dy2
    yp = -sp*dx2 + cp*dy2
    lam = xp*xp/(rx*rx) + yp*yp/(ry*ry)
    if lam > 1:
        s = math.sqrt(lam)
        rx *= s; ry *= s
    sign = -1.0 if large_arc == sweep else 1.0
    num = max(0.0, (rx*rx*ry*ry - rx*rx*yp*yp - ry*ry*xp*xp))
    den = max(1e-18, rx*rx*yp*yp + ry*ry*xp*xp)
    coef = sign * math.sqrt(num/den)
    cxp = coef * (rx*yp/ry)
    cyp = coef * (-ry*xp/rx)
    cx = cp*cxp - sp*cyp + (x1+x2)/2.0
    cy = sp*cxp + cp*cyp + (y1+y2)/2.0

    def angle(u: Tuple[float,float], v: Tuple[float,float]) -> float:
        dot = u[0]*v[0] + u[1]*v[1]
        det = u[0]*v[1] - u[1]*v[0]
        return math.atan2(det, dot)
    u = ((xp-cxp)/rx, (yp-cyp)/ry)
    v = ((-xp-cxp)/rx, (-yp-cyp)/ry)
    theta1 = angle((1,0), u)
    delta = angle(u, v)
    if not sweep and delta > 0: delta -= 2*math.pi
    if sweep and delta < 0: delta += 2*math.pi
    steps = max(2, min(64, int(math.ceil(abs(delta) / (math.pi/12)))))
    result = []
    for i in range(1, steps+1):
        t = theta1 + delta*i/steps
        x = cx + cp*rx*math.cos(t) - sp*ry*math.sin(t)
        y = cy + sp*rx*math.cos(t) + cp*ry*math.sin(t)
        result.append((x,y))
    result[-1] = end
    return result


def parse_svg_path(data: str) -> List[Dict[str, Any]]:
    tokens = PATH_TOKEN_RE.findall(data or "")
    i = 0
    cmd: Optional[str] = None
    current = (0.0, 0.0)
    sub_start = (0.0, 0.0)
    last_cubic_ctrl: Optional[Tuple[float,float]] = None
    last_quad_ctrl: Optional[Tuple[float,float]] = None
    out: List[Dict[str, Any]] = []

    def number() -> float:
        nonlocal i
        if i >= len(tokens) or re.fullmatch(r"[A-Za-z]", tokens[i]):
            raise SVGImportError("Malformed SVG path data.")
        value = float(tokens[i]); i += 1
        return value

    while i < len(tokens):
        if re.fullmatch(r"[A-Za-z]", tokens[i]):
            cmd = tokens[i]; i += 1
        elif cmd is None:
            raise SVGImportError("SVG path data begins without a command.")
        assert cmd is not None
        upper = cmd.upper()
        relative = cmd.islower()
        if upper == "Z":
            out.append({"op": SEG_Z, "points": []})
            current = sub_start
            last_cubic_ctrl = last_quad_ctrl = None
            cmd = None
            continue
        count = PATH_PARAM_COUNTS[upper]
        first_for_m = True
        while i < len(tokens) and not re.fullmatch(r"[A-Za-z]", tokens[i]):
            vals = [number() for _ in range(count)]
            x0, y0 = current
            if upper == "M":
                p = (vals[0] + (x0 if relative else 0), vals[1] + (y0 if relative else 0))
                if first_for_m:
                    out.append({"op": SEG_M, "points": [p]}); sub_start = p
                    first_for_m = False
                else:
                    out.append({"op": SEG_L, "points": [p]})
                current = p
            elif upper == "L":
                p = (vals[0] + (x0 if relative else 0), vals[1] + (y0 if relative else 0))
                out.append({"op": SEG_L, "points": [p]}); current = p
            elif upper == "H":
                p = (vals[0] + (x0 if relative else 0), y0)
                out.append({"op": SEG_L, "points": [p]}); current = p
            elif upper == "V":
                p = (x0, vals[0] + (y0 if relative else 0))
                out.append({"op": SEG_L, "points": [p]}); current = p
            elif upper == "C":
                pts = [(vals[j] + (x0 if relative else 0), vals[j+1] + (y0 if relative else 0)) for j in (0,2,4)]
                out.append({"op": SEG_C, "points": pts}); current = pts[-1]; last_cubic_ctrl = pts[-2]
            elif upper == "S":
                c1 = (2*x0-last_cubic_ctrl[0], 2*y0-last_cubic_ctrl[1]) if last_cubic_ctrl else current
                c2 = (vals[0] + (x0 if relative else 0), vals[1] + (y0 if relative else 0))
                p = (vals[2] + (x0 if relative else 0), vals[3] + (y0 if relative else 0))
                out.append({"op": SEG_C, "points": [c1,c2,p]}); current = p; last_cubic_ctrl = c2
            elif upper == "Q":
                c = (vals[0] + (x0 if relative else 0), vals[1] + (y0 if relative else 0))
                p = (vals[2] + (x0 if relative else 0), vals[3] + (y0 if relative else 0))
                out.append({"op": SEG_Q, "points": [c,p]}); current = p; last_quad_ctrl = c
            elif upper == "T":
                c = (2*x0-last_quad_ctrl[0], 2*y0-last_quad_ctrl[1]) if last_quad_ctrl else current
                p = (vals[0] + (x0 if relative else 0), vals[1] + (y0 if relative else 0))
                out.append({"op": SEG_Q, "points": [c,p]}); current = p; last_quad_ctrl = c
            elif upper == "A":
                p = (vals[5] + (x0 if relative else 0), vals[6] + (y0 if relative else 0))
                for arc_p in arc_to_points(current, vals[0], vals[1], vals[2], int(vals[3] != 0), int(vals[4] != 0), p):
                    out.append({"op": SEG_L, "points": [arc_p]})
                current = p
            if upper not in {"C", "S"}: last_cubic_ctrl = None
            if upper not in {"Q", "T"}: last_quad_ctrl = None
            if upper == "M": upper = "L"
            if i >= len(tokens) or re.fullmatch(r"[A-Za-z]", tokens[i]):
                break
    if out and out[0]["op"] != SEG_M:
        raise SVGImportError("SVG path does not begin with MoveTo.")
    return out


# ---------------------------------------------------------------------------
# SVG importer
# ---------------------------------------------------------------------------


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_style_declarations(text: Optional[str]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    if not text:
        return result
    for item in text.split(";"):
        if ":" in item:
            k, v = item.split(":", 1)
            result[k.strip().lower()] = v.strip()
    return result


def inherited_style(parent: Dict[str, str], elem: ET.Element) -> Dict[str, str]:
    result = dict(parent)
    result.update(parse_style_declarations(elem.get("style")))
    for key in ["fill", "stroke", "stroke-width", "fill-rule", "opacity", "fill-opacity", "stroke-opacity", "display", "visibility", "color"]:
        if elem.get(key) is not None:
            result[key] = str(elem.get(key))
    return result


def style_to_paint(style: Dict[str, str], warnings: List[str], label: str) -> Optional[PaintStyle]:
    if style.get("display", "").lower() == "none" or style.get("visibility", "").lower() in {"hidden", "collapse"}:
        return None
    opacity = float_or(style.get("opacity"), 1.0)
    fill_opacity = opacity * float_or(style.get("fill-opacity"), 1.0)
    stroke_opacity = opacity * float_or(style.get("stroke-opacity"), 1.0)
    fill_value = style.get("fill", "#000000")
    stroke_value = style.get("stroke", "none")
    current_color = style.get("color", "#000000")
    if fill_value == "currentColor": fill_value = current_color
    if stroke_value == "currentColor": stroke_value = current_color
    if fill_value.strip().lower().startswith("url("):
        warnings.append(f"{label}: gradient/pattern fill approximated as black.")
        fill_value = "#000000"
    if stroke_value.strip().lower().startswith("url("):
        warnings.append(f"{label}: gradient/pattern stroke approximated as black.")
        stroke_value = "#000000"
    fill = parse_color(fill_value, fill_opacity)
    stroke = parse_color(stroke_value, stroke_opacity)
    width = max(0.0, parse_length(style.get("stroke-width"), 1.0))
    if fill is None and stroke is None:
        return None
    return PaintStyle(fill, stroke, width, style.get("fill-rule", "nonzero")).normalized()


def float_or(value: Optional[str], default: float) -> float:
    try:
        return float(value) if value is not None else default
    except ValueError:
        return default


def parse_points(text: Optional[str]) -> List[Tuple[float,float]]:
    nums = [float(v) for v in re.findall(r"[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?", text or "")]
    return list(zip(nums[0::2], nums[1::2]))


def rounded_rect_path(x: float, y: float, w: float, h: float, rx: float, ry: float) -> List[Dict[str,Any]]:
    rx = min(abs(rx), w/2); ry = min(abs(ry), h/2)
    k = 0.5522847498307936
    return [
        {"op": SEG_M, "points": [(x+rx,y)]},
        {"op": SEG_L, "points": [(x+w-rx,y)]},
        {"op": SEG_C, "points": [(x+w-rx+k*rx,y),(x+w,y+ry-k*ry),(x+w,y+ry)]},
        {"op": SEG_L, "points": [(x+w,y+h-ry)]},
        {"op": SEG_C, "points": [(x+w,y+h-ry+k*ry),(x+w-rx+k*rx,y+h),(x+w-rx,y+h)]},
        {"op": SEG_L, "points": [(x+rx,y+h)]},
        {"op": SEG_C, "points": [(x+rx-k*rx,y+h),(x,y+h-ry+k*ry),(x,y+h-ry)]},
        {"op": SEG_L, "points": [(x,y+ry)]},
        {"op": SEG_C, "points": [(x,y+ry-k*ry),(x+rx-k*rx,y),(x+rx,y)]},
        {"op": SEG_Z, "points": []},
    ]


def parse_css_rules(root: ET.Element) -> List[Tuple[str, Dict[str, str]]]:
    rules: List[Tuple[str, Dict[str, str]]] = []
    for elem in root.iter():
        if local_name(elem.tag) != "style" or not elem.text:
            continue
        css = re.sub(r"/\*.*?\*/", "", elem.text, flags=re.S)
        for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
            declarations = parse_style_declarations(body)
            for selector in selectors.split(","):
                selector = selector.strip()
                if selector and all(ch not in selector for ch in ">+~[:"):
                    rules.append((selector, declarations))
    return rules


def css_selector_matches(selector: str, elem: ET.Element) -> bool:
    tag = local_name(elem.tag)
    elem_id = elem.get("id", "")
    classes = set(elem.get("class", "").split())
    if selector.startswith("#"):
        return elem_id == selector[1:]
    if selector.startswith("."):
        return selector[1:] in classes
    match = re.fullmatch(r"([A-Za-z_][\w-]*)?(#[\w-]+)?((?:\.[\w-]+)*)", selector)
    if not match:
        return False
    sel_tag, sel_id, class_blob = match.groups()
    if sel_tag and sel_tag.lower() != tag:
        return False
    if sel_id and elem_id != sel_id[1:]:
        return False
    required = {v for v in class_blob.split(".") if v}
    return required.issubset(classes)


def import_svg(path: str | Path) -> VectorDocument:
    path = Path(path)
    try:
        if path.suffix.lower() == ".svgz":
            raw = gzip.decompress(path.read_bytes())
        else:
            raw = path.read_bytes()
        root = ET.fromstring(raw)
    except Exception as exc:
        raise SVGImportError(f"Could not parse SVG: {exc}") from exc
    if local_name(root.tag) != "svg":
        raise SVGImportError("Root element is not <svg>.")

    warnings: List[str] = []
    css_rules = parse_css_rules(root)
    id_map = {elem.get("id"): elem for elem in root.iter() if elem.get("id")}
    active_uses: set[str] = set()
    width = parse_length(root.get("width"), 0.0)
    height = parse_length(root.get("height"), 0.0)
    view_box = [float(v) for v in re.findall(r"[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?", root.get("viewBox", ""))]
    if len(view_box) == 4 and view_box[2] > 0 and view_box[3] > 0:
        vx, vy, vw, vh = view_box
    elif width > 0 and height > 0:
        vx, vy, vw, vh = 0.0, 0.0, width, height
    else:
        vx, vy, vw, vh = 0.0, 0.0, float(CANVAS_W), float(CANVAS_H)
        warnings.append("SVG has no usable viewBox or size; assumed 720x480.")

    # Fit entire SVG viewport inside canvas with an 8 px margin, centered.
    fit = min((CANVAS_W-2*DEFAULT_MARGIN)/vw, (CANVAS_H-2*DEFAULT_MARGIN)/vh)
    root_map = mat_mul(
        mat_translate((CANVAS_W-vw*fit)/2.0, (CANVAS_H-vh*fit)/2.0),
        mat_mul(mat_scale(fit, fit), mat_translate(-vx, -vy)),
    )

    commands: List[VectorCommand] = []
    unsupported_seen: set[str] = set()

    def walk(elem: ET.Element, parent_style: Dict[str,str], parent_matrix: Matrix) -> None:
        tag = local_name(elem.tag)
        css_base = dict(parent_style)
        for selector, declarations in css_rules:
            if css_selector_matches(selector, elem):
                css_base.update(declarations)
        style = inherited_style(css_base, elem)
        matrix = mat_mul(parent_matrix, parse_transform(elem.get("transform")))
        label = elem.get("id") or tag
        lx = lambda name, default=0.0: parse_length(elem.get(name), default, vw)
        ly = lambda name, default=0.0: parse_length(elem.get(name), default, vh)
        lr = lambda name, default=0.0: parse_length(elem.get(name), default, min(vw, vh))
        if tag in {"svg", "g", "a", "switch"}:
            for child in elem:
                walk(child, style, matrix)
            return
        if tag in {"defs", "metadata", "title", "desc", "style", "lineargradient", "radialgradient", "clippath", "mask", "symbol"}:
            return
        paint = style_to_paint(style, warnings, label)
        if paint is None:
            return
        cmd: Optional[VectorCommand] = None
        try:
            if tag == "rect":
                x = lx("x"); y = ly("y")
                w = lx("width"); h = ly("height")
                if w <= 0 or h <= 0: return
                rx = lx("rx"); ry = ly("ry")
                if rx or ry:
                    if not rx: rx = ry
                    if not ry: ry = rx
                    cmd = VectorCommand(OP_PATH, paint, {"segments": rounded_rect_path(x,y,w,h,rx,ry)}, label)
                else:
                    cmd = VectorCommand(OP_RECT, paint, {"x":x,"y":y,"w":w,"h":h}, label)
            elif tag == "circle":
                cx = lx("cx"); cy = ly("cy"); r = lr("r")
                if r <= 0: return
                cmd = VectorCommand(OP_ELLIPSE, paint, {"cx":cx,"cy":cy,"rx":r,"ry":r}, label)
            elif tag == "ellipse":
                cx = lx("cx"); cy = ly("cy"); rx = lx("rx"); ry = ly("ry")
                if rx <= 0 or ry <= 0: return
                cmd = VectorCommand(OP_ELLIPSE, paint, {"cx":cx,"cy":cy,"rx":rx,"ry":ry}, label)
            elif tag == "line":
                p1 = (lx("x1"), ly("y1"))
                p2 = (lx("x2"), ly("y2"))
                cmd = VectorCommand(OP_LINE, PaintStyle(None, paint.stroke or paint.fill, paint.stroke_width, paint.fill_rule), {"p1":p1,"p2":p2}, label)
            elif tag in {"polyline", "polygon"}:
                pts = parse_points(elem.get("points"))
                if len(pts) < (2 if tag == "polyline" else 3): return
                cmd = VectorCommand(OP_POLYLINE if tag == "polyline" else OP_POLYGON, paint, {"points":pts}, label)
            elif tag == "path":
                segs = parse_svg_path(elem.get("d", ""))
                if not segs: return
                cmd = VectorCommand(OP_PATH, paint, {"segments":segs}, label)
            elif tag == "use":
                href = elem.get("href") or elem.get("{http://www.w3.org/1999/xlink}href") or ""
                ref_id = href[1:] if href.startswith("#") else ""
                target = id_map.get(ref_id)
                if target is None:
                    warnings.append(f"{label}: unresolved <use> reference {href!r} was skipped.")
                    return
                if ref_id in active_uses:
                    warnings.append(f"{label}: recursive <use> reference was skipped.")
                    return
                active_uses.add(ref_id)
                use_matrix = mat_mul(matrix, mat_translate(lx("x"), ly("y")))
                if local_name(target.tag) == "symbol":
                    for child in target:
                        walk(child, style, use_matrix)
                else:
                    walk(target, style, use_matrix)
                active_uses.remove(ref_id)
                return
            elif tag in {"text", "image", "foreignobject"}:
                warnings.append(f"{label}: <{tag}> is unsupported and was skipped.")
                return
            else:
                if tag not in unsupported_seen:
                    warnings.append(f"Unsupported SVG element <{tag}> was skipped.")
                    unsupported_seen.add(tag)
                return
            if cmd is not None:
                cmd = transform_command(cmd, matrix)
                validate_command(cmd)
                commands.append(cmd)
        except Exception as exc:
            warnings.append(f"{label}: skipped because {exc}")

    base_style = {"fill": "#000000", "stroke": "none", "stroke-width": "1", "fill-rule": "nonzero", "opacity": "1"}
    walk(root, base_style, root_map)
    if not commands:
        raise SVGImportError("SVG contained no supported visible vector elements.")
    return VectorDocument(commands, 1.0, 0.0, 0.0, path.name, warnings)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def cubic_point(p0, p1, p2, p3, t):
    u = 1-t
    return (u**3*p0[0]+3*u*u*t*p1[0]+3*u*t*t*p2[0]+t**3*p3[0],
            u**3*p0[1]+3*u*u*t*p1[1]+3*u*t*t*p2[1]+t**3*p3[1])


def quad_point(p0, p1, p2, t):
    u = 1-t
    return (u*u*p0[0]+2*u*t*p1[0]+t*t*p2[0], u*u*p0[1]+2*u*t*p1[1]+t*t*p2[1])


def flatten_path(segments: Sequence[Dict[str,Any]], steps: int = 12) -> List[Tuple[List[Tuple[float,float]], bool]]:
    subpaths: List[Tuple[List[Tuple[float,float]], bool]] = []
    current: List[Tuple[float,float]] = []
    pos = (0.0,0.0)
    start = (0.0,0.0)
    closed = False
    for seg in segments:
        op = int(seg["op"]); pts = [tuple(p) for p in seg.get("points", [])]
        if op == SEG_M:
            if current: subpaths.append((current, closed))
            pos = pts[0]; start = pos; current = [pos]; closed = False
        elif op == SEG_L:
            pos = pts[0]; current.append(pos)
        elif op == SEG_Q:
            c, end = pts
            for i in range(1, steps+1): current.append(quad_point(pos,c,end,i/steps))
            pos = end
        elif op == SEG_C:
            c1,c2,end = pts
            for i in range(1, steps+1): current.append(cubic_point(pos,c1,c2,end,i/steps))
            pos = end
        elif op == SEG_Z:
            if current and current[-1] != start: current.append(start)
            pos = start; closed = True
    if current: subpaths.append((current, closed))
    return subpaths


def render_to_tk(canvas: tk.Canvas, commands: Sequence[VectorCommand]) -> None:
    """Render the GUI preview with the same rasterizer used for PNG export.

    The earlier branch maintained a separate Tk Canvas path renderer.  That
    allowed open-path fill semantics and compound SVG paths to render
    differently in the GUI than in the exported PNG.  Keeping one renderer
    makes the preview authoritative: what is shown is what will be exported.
    """
    canvas.delete("all")
    if ImageTk is not None and Image is not None and ImageDraw is not None:
        image = render_to_pillow(commands)
        photo = ImageTk.PhotoImage(image)
        # Tk discards images whose Python reference is collected.  Retain both
        # objects on the canvas until the next redraw.
        canvas._mcoreimg_preview_image = image  # type: ignore[attr-defined]
        canvas._mcoreimg_preview_photo = photo  # type: ignore[attr-defined]
        canvas.create_image(0, 0, anchor="nw", image=photo)
        canvas.create_rectangle(0, 0, CANVAS_W-1, CANVAS_H-1, outline="#777777")
        return

    # Pillow-less fallback.  This keeps the editor usable, although PNG export
    # and exact SVG preview parity require Pillow.
    canvas.create_rectangle(0,0,CANVAS_W,CANVAS_H,fill=BACKGROUND,outline="")
    for cmd in commands:
        s = cmd.style.normalized(); g = cmd.geom
        fill = s.fill or ""
        stroke = s.stroke or ""
        width = max(1, int(round(s.stroke_width))) if stroke else 1
        try:
            if cmd.opcode == OP_RECT:
                canvas.create_rectangle(g["x"],g["y"],g["x"]+g["w"],g["y"]+g["h"],fill=fill,outline=stroke,width=width)
            elif cmd.opcode == OP_ELLIPSE:
                canvas.create_oval(g["cx"]-g["rx"],g["cy"]-g["ry"],g["cx"]+g["rx"],g["cy"]+g["ry"],fill=fill,outline=stroke,width=width)
            elif cmd.opcode == OP_LINE:
                canvas.create_line(*g["p1"],*g["p2"],fill=stroke or fill,width=width)
            elif cmd.opcode in {OP_POLYLINE,OP_POLYGON}:
                flat = [v for point in g["points"] for v in point]
                if cmd.opcode == OP_POLYGON:
                    canvas.create_polygon(*flat,fill=fill,outline=stroke,width=width)
                else:
                    canvas.create_line(*flat,fill=stroke or fill,width=width)
            elif cmd.opcode == OP_PATH:
                for points, _closed in flatten_path(g["segments"]):
                    flat = [v for point in points for v in point]
                    if fill and len(points) >= 3:
                        canvas.create_polygon(*flat,fill=fill,outline="")
                    if stroke and len(points) >= 2:
                        canvas.create_line(*flat,fill=stroke,width=width)
        except tk.TclError:
            continue
    canvas.create_rectangle(0,0,CANVAS_W-1,CANVAS_H-1,outline="#777777")


def render_to_pillow(commands: Sequence[VectorCommand]):
    """Rasterize commands with true source-over alpha compositing.

    Every fill and stroke is first drawn into an 8-bit coverage mask.  Its
    palette alpha is then multiplied into that mask before the colored layer
    is composited over the accumulated image.  This avoids Pillow/Tk drawing
    modes that can replace pixels instead of blending them and guarantees that
    the GUI preview and exported PNG use identical SVG-style draw-order alpha.
    """
    if Image is None or ImageDraw is None:
        raise MCIError("Pillow is required for PNG export.")

    background = color_to_rgba(BACKGROUND) or (255, 255, 255, 255)
    image = Image.new("RGBA", (CANVAS_W, CANVAS_H), background)

    def composite_mask(draw_mask_fn, color: Tuple[int, int, int, int]) -> None:
        nonlocal image
        r, g, b, alpha = color
        if alpha <= 0:
            return
        mask = Image.new("L", (CANVAS_W, CANVAS_H), 0)
        mask_draw = ImageDraw.Draw(mask)
        draw_mask_fn(mask_draw)
        if alpha < 255:
            # Multiply geometric coverage by the shape's transmitted alpha.
            mask = mask.point(lambda coverage, a=alpha: (coverage * a + 127) // 255)
        layer = Image.new("RGBA", (CANVAS_W, CANVAS_H), (r, g, b, 255))
        layer.putalpha(mask)
        image = Image.alpha_composite(image, layer)

    for cmd in commands:
        s = cmd.style.normalized()
        g = cmd.geom
        fill = color_to_rgba(s.fill) if s.fill else None
        stroke = color_to_rgba(s.stroke) if s.stroke else None
        width = max(1, int(round(s.stroke_width))) if stroke else 1

        if cmd.opcode == OP_RECT:
            box = [g["x"], g["y"], g["x"] + g["w"], g["y"] + g["h"]]
            if fill:
                composite_mask(lambda d, box=box: d.rectangle(box, fill=255), fill)
            if stroke:
                composite_mask(lambda d, box=box, width=width: d.rectangle(box, outline=255, width=width), stroke)

        elif cmd.opcode == OP_ELLIPSE:
            box = [g["cx"] - g["rx"], g["cy"] - g["ry"], g["cx"] + g["rx"], g["cy"] + g["ry"]]
            if fill:
                composite_mask(lambda d, box=box: d.ellipse(box, fill=255), fill)
            if stroke:
                composite_mask(lambda d, box=box, width=width: d.ellipse(box, outline=255, width=width), stroke)

        elif cmd.opcode == OP_LINE:
            color = stroke or fill
            if color:
                pts = [tuple(g["p1"]), tuple(g["p2"])]
                composite_mask(lambda d, pts=pts, width=width: d.line(pts, fill=255, width=width), color)

        elif cmd.opcode == OP_POLYLINE:
            color = stroke or fill
            if color:
                pts = [tuple(point) for point in g["points"]]
                composite_mask(lambda d, pts=pts, width=width: d.line(pts, fill=255, width=width), color)

        elif cmd.opcode == OP_POLYGON:
            pts = [tuple(point) for point in g["points"]]
            if fill:
                composite_mask(lambda d, pts=pts: d.polygon(pts, fill=255), fill)
            if stroke and pts:
                closed_pts = pts + [pts[0]]
                composite_mask(lambda d, pts=closed_pts, width=width: d.line(pts, fill=255, width=width), stroke)

        elif cmd.opcode == OP_PATH:
            for pts, closed in flatten_path(g["segments"], 16):
                if fill and len(pts) >= 3:
                    composite_mask(lambda d, pts=pts: d.polygon(pts, fill=255), fill)
                if stroke and len(pts) >= 2:
                    line_pts = pts + ([pts[0]] if closed else [])
                    composite_mask(lambda d, pts=line_pts, width=width: d.line(pts, fill=255, width=width), stroke)

    # PNG and Tk preview are intentionally flattened against the configured
    # canvas background after all source-over blending is complete.
    return image.convert("RGB")


# ---------------------------------------------------------------------------
# Bit codec
# ---------------------------------------------------------------------------


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


def rgb565(color:str)->int:
    rgba=color_to_rgba(color)
    if rgba is None: raise MCIError("Invalid color.")
    r,g,b,_a=rgba
    return ((r>>3)<<11)|((g>>2)<<5)|(b>>3)


def alpha4(color: str) -> int:
    rgba = color_to_rgba(color)
    if rgba is None:
        raise MCIError("Invalid color.")
    return max(0, min(15, int(round(rgba[3] * 15 / 255))))


def from_rgb565(v:int)->str:
    r=((v>>11)&31)*255//31; g=((v>>5)&63)*255//63; b=(v&31)*255//31
    return f"#{r:02X}{g:02X}{b:02X}"


def from_rgb565_a4(v:int, a4: int) -> str:
    a4 = max(0, min(15, int(a4)))
    base = from_rgb565(v)
    a = a4 * 255 // 15 if a4 else 0
    return base if a >= 255 else f"{base}{a:02X}"


def quantize_palette_color(color: str) -> str:
    return from_rgb565_a4(rgb565(color), alpha4(color))


def build_palette(commands:Sequence[VectorCommand])->List[str]:
    palette=[]
    for c in commands:
        for color in (c.style.fill,c.style.stroke):
            if color:
                quant=quantize_palette_color(color)
                if quant not in palette: palette.append(quant)
    if not palette: palette=["#000000"]
    if len(palette)>MAX_PALETTE: raise MCIError(f"Image needs {len(palette)} colors; protocol v{PROTOCOL_VERSION} supports {MAX_PALETTE}.")
    return palette


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


def geom_translation(prev:VectorCommand,cur:VectorCommand)->Optional[Tuple[int,int]]:
    if prev.opcode!=cur.opcode: return None
    if prev.style.to_json()!=cur.style.to_json(): return None
    pa=command_points(prev); pb=command_points(cur)
    if len(pa)!=len(pb) or not pa: return None
    dx=int(pb[0][0])-int(pa[0][0]); dy=int(pb[0][1])-int(pa[0][1])
    for a,b in zip(pa,pb):
        if int(b[0])-int(a[0])!=dx or int(b[1])-int(a[1])!=dy: return None
    # Geometric structure must also match, not merely its flattened point count.
    a=prev.clone(); b=cur.clone()
    a=translate_command(a,dx,dy)
    return (dx,dy) if a.geom==b.geom else None


def translate_command(cmd:VectorCommand,dx:int,dy:int)->VectorCommand:
    return transform_command(cmd,mat_translate(dx,dy))


def write_geometry(w:BitWriter,cmd:VectorCommand,state:PointState):
    g=cmd.geom
    if cmd.opcode==OP_RECT:
        write_point(w,state,(int(g["x"]),int(g["y"]))); w.ue(int(g["w"])-1); w.ue(int(g["h"])-1)
    elif cmd.opcode==OP_ELLIPSE:
        write_point(w,state,(int(g["cx"]),int(g["cy"]))); w.ue(int(g["rx"])-1); w.ue(int(g["ry"])-1)
    elif cmd.opcode==OP_LINE:
        write_point(w,state,tuple(g["p1"])); write_point(w,state,tuple(g["p2"]))
    elif cmd.opcode in {OP_POLYLINE,OP_POLYGON}:
        pts=[tuple(p) for p in g["points"]]; minimum=2 if cmd.opcode==OP_POLYLINE else 3
        w.ue(len(pts)-minimum)
        for p in pts: write_point(w,state,p)
    elif cmd.opcode==OP_PATH:
        segs=g["segments"]; w.ue(len(segs)-1)
        for seg in segs:
            op=int(seg["op"]); w.bits_n(op,3)
            for p in seg.get("points",[]): write_point(w,state,tuple(p))


def read_geometry(r:BitReader,opcode:int,state:PointState)->Dict[str,Any]:
    if opcode==OP_RECT:
        x,y=read_point(r,state); return {"x":x,"y":y,"w":r.ue(719)+1,"h":r.ue(479)+1}
    if opcode==OP_ELLIPSE:
        cx,cy=read_point(r,state); return {"cx":cx,"cy":cy,"rx":r.ue(719)+1,"ry":r.ue(479)+1}
    if opcode==OP_LINE:
        return {"p1":read_point(r,state),"p2":read_point(r,state)}
    if opcode in {OP_POLYLINE,OP_POLYGON}:
        minimum=2 if opcode==OP_POLYLINE else 3; n=r.ue(MAX_COMMANDS)+minimum
        return {"points":[read_point(r,state) for _ in range(n)]}
    if opcode==OP_PATH:
        n=r.ue(MAX_COMMANDS*8)+1; segs=[]
        counts={SEG_M:1,SEG_L:1,SEG_Q:2,SEG_C:3,SEG_Z:0}
        for _ in range(n):
            op=r.bits_n(3)
            if op not in counts: raise MCIError("Invalid path segment opcode.")
            segs.append({"op":op,"points":[read_point(r,state) for _ in range(counts[op])]})
        return {"segments":segs}
    raise MCIError("Unknown opcode.")


def encode_commands(commands:Sequence[VectorCommand])->Tuple[bytes,int,int,List[str]]:
    commands=[quantize_command(c) for c in commands]
    palette=build_palette(commands)
    w=BitWriter(); w.bits_n(PROTOCOL_VERSION,4); w.ue(len(palette)-1)
    for color in palette:
        w.bits_n(rgb565(color),16)
        w.bits_n(alpha4(color),4)
    w.ue(len(commands))
    point_states={op:PointState() for op in OP_NAMES}
    style_states:Dict[int,Tuple[Any,...]]={}
    recent:Dict[int,VectorCommand]={}
    previous_opcode:Optional[int]=None
    repeats=0
    for cmd in commands:
        ref=recent.get(cmd.opcode); delta=geom_translation(ref,cmd) if ref else None
        w.bit(delta is not None)
        if delta is not None:
            w.bits_n(cmd.opcode,3); w.rice_signed(delta[0],2); w.rice_signed(delta[1],2); repeats+=1
        else:
            same_op=previous_opcode==cmd.opcode; w.bit(same_op)
            if not same_op: w.bits_n(cmd.opcode,3)
            key=style_key(cmd.style,palette); same_style=style_states.get(cmd.opcode)==key; w.bit(same_style)
            if not same_style: write_style(w,cmd.style,palette); style_states[cmd.opcode]=key
            write_geometry(w,cmd,point_states[cmd.opcode])
        recent[cmd.opcode]=cmd.clone(); previous_opcode=cmd.opcode
    return w.to_bytes(),len(w.bits),repeats,palette


def decode_commands(data:bytes)->Tuple[List[VectorCommand],List[str]]:
    r=BitReader(data)
    if r.bits_n(4)!=PROTOCOL_VERSION: raise MCIError("Unsupported MCoreIMG SVG protocol version.")
    palette=[from_rgb565_a4(r.bits_n(16), r.bits_n(4)) for _ in range(r.ue(MAX_PALETTE-1)+1)]
    count=r.ue(MAX_COMMANDS)
    point_states={op:PointState() for op in OP_NAMES}
    style_states:Dict[int,PaintStyle]={}
    recent:Dict[int,VectorCommand]={}
    previous_opcode:Optional[int]=None
    result=[]
    for _ in range(count):
        if r.bit():
            op=r.bits_n(3)
            if op not in recent: raise MCIError("Repeat references missing opcode history.")
            cmd=translate_command(recent[op],r.rice_signed(2,719),r.rice_signed(2,479))
            cmd=quantize_command(cmd)
        else:
            same=bool(r.bit())
            if same:
                if previous_opcode is None: raise MCIError("Same opcode before initialization.")
                op=previous_opcode
            else: op=r.bits_n(3)
            if op not in OP_NAMES: raise MCIError("Invalid opcode.")
            same_style=bool(r.bit())
            if same_style:
                if op not in style_states: raise MCIError("Style reuse before initialization.")
                style=copy.deepcopy(style_states[op])
            else:
                style=read_style(r,palette); style_states[op]=copy.deepcopy(style)
            cmd=VectorCommand(op,style,read_geometry(r,op,point_states[op]))
            validate_command(cmd)
        result.append(cmd); recent[cmd.opcode]=cmd.clone(); previous_opcode=cmd.opcode
    return result,palette


# ---------------------------------------------------------------------------
# Base91 and MeshCore framing
# ---------------------------------------------------------------------------


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


def encode_image(commands:Sequence[VectorCommand])->EncodedImage:
    bit_bytes,bit_count,repeats,palette=encode_commands(commands)
    raw=bit_bytes+zlib.crc32(bit_bytes).to_bytes(4,"big")
    payload=base91_encode(raw)
    image_id=enc62(zlib.crc32(raw)%(62**3),3)
    chunks=[payload[i:i+FRAME_PAYLOAD_LEN] for i in range(0,len(payload),FRAME_PAYLOAD_LEN)] or [""]
    total=len(chunks);frames=[]
    for i,chunk in enumerate(chunks):
        header=FRAME_MAGIC+enc62(PROTOCOL_VERSION,1)+image_id+enc62(i,1)+enc62(total,1)+enc62(len(chunk),2)+enc62(frame_crc(chunk),3)+"0"
        frames.append(header+chunk)
    stats=CodecStats(len(commands),len(palette),bit_count,len(raw),len(payload),total,repeats)
    return EncodedImage(raw,payload,frames,stats,image_id,palette)


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


# ---------------------------------------------------------------------------
# Source file helpers
# ---------------------------------------------------------------------------


def save_source(path:str|Path,doc:VectorDocument)->None:
    Path(path).write_text(json.dumps(doc.to_json(),indent=2)+"\n",encoding="utf-8")


def load_source(path:str|Path)->VectorDocument:
    path=Path(path)
    if path.suffix.lower() in {".svg",".svgz"}: return import_svg(path)
    obj=json.loads(path.read_text(encoding="utf-8"))
    return VectorDocument.from_json(obj)


# ---------------------------------------------------------------------------
# Tk Constructor
# ---------------------------------------------------------------------------


class ConstructorApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"MCoreIMG SVG Constructor — {CONSTRUCTOR_BUILD} — {Path(__file__).name}")
        self.geometry("1180x760")
        self.minsize(1040,680)
        self.doc=VectorDocument()
        self.selected_index:Optional[int]=None
        self.var_scale=tk.DoubleVar(value=100.0)
        self.var_offset_x=tk.DoubleVar(value=0.0)
        self.var_offset_y=tk.DoubleVar(value=0.0)
        self.var_status=tk.StringVar(value=f"{CONSTRUCTOR_BUILD} | Protocol v{PROTOCOL_VERSION} | RGB565+A4 alpha | {MAX_MESSAGES} messages")
        self.var_stats=tk.StringVar(value="No image loaded")
        self._build_ui()
        self._refresh()

    def _build_ui(self):
        toolbar=ttk.Frame(self,padding=6);toolbar.pack(fill="x")
        for text,cmd in [
            ("Open SVG / Source",self.open_file),("Save Source",self.save_file),("Export MCI",self.export_mci),
            ("Export PNG",self.export_png),("Preview Frames",self.preview_frames),
        ]: ttk.Button(toolbar,text=text,command=cmd).pack(side="left",padx=3)
        ttk.Separator(toolbar,orient="vertical").pack(side="left",fill="y",padx=8)
        ttk.Button(toolbar,text="Fit",command=self.fit_document).pack(side="left",padx=3)
        ttk.Button(toolbar,text="Center",command=self.center_document).pack(side="left",padx=3)
        ttk.Button(toolbar,text="Bake Transform",command=self.bake_transform).pack(side="left",padx=3)
        ttk.Label(toolbar,text=f"TRUE ALPHA ACTIVE  •  Protocol v{PROTOCOL_VERSION}  •  RGB565+A4  •  {MAX_MESSAGES} messages").pack(side="right",padx=8)

        main=ttk.Panedwindow(self,orient="horizontal");main.pack(fill="both",expand=True,padx=6,pady=(0,6))
        left=ttk.Frame(main);right=ttk.Frame(main,width=350);main.add(left,weight=4);main.add(right,weight=2)
        holder=ttk.Frame(left);holder.pack(fill="both",expand=True)
        self.canvas=tk.Canvas(holder,width=CANVAS_W,height=CANVAS_H,bg=BACKGROUND,highlightthickness=0)
        self.canvas.pack(anchor="center",padx=8,pady=8)

        transform=ttk.LabelFrame(right,text="SVG Transform",padding=8);transform.pack(fill="x",pady=(0,6))
        ttk.Label(transform,text="Scale (%)").grid(row=0,column=0,sticky="w")
        scale=ttk.Scale(transform,from_=10,to=400,variable=self.var_scale,command=lambda _v:self._transform_changed())
        scale.grid(row=0,column=1,sticky="ew",padx=5)
        self.scale_entry=ttk.Spinbox(transform,from_=1,to=1000,textvariable=self.var_scale,width=8,command=self._transform_changed)
        self.scale_entry.grid(row=0,column=2)
        ttk.Label(transform,text="Offset X").grid(row=1,column=0,sticky="w")
        ttk.Spinbox(transform,from_=-1440,to=1440,textvariable=self.var_offset_x,width=10,command=self._transform_changed).grid(row=1,column=1,sticky="w")
        ttk.Label(transform,text="Offset Y").grid(row=2,column=0,sticky="w")
        ttk.Spinbox(transform,from_=-960,to=960,textvariable=self.var_offset_y,width=10,command=self._transform_changed).grid(row=2,column=1,sticky="w")
        transform.columnconfigure(1,weight=1)
        for var in (self.var_scale,self.var_offset_x,self.var_offset_y): var.trace_add("write",lambda *_:self._transform_changed())

        layer_frame=ttk.LabelFrame(right,text="Draw Order / Layers",padding=6);layer_frame.pack(fill="both",expand=True,pady=(0,6))
        self.layer_list=tk.Listbox(layer_frame,exportselection=False,font=("TkFixedFont",9))
        self.layer_list.pack(fill="both",expand=True)
        self.layer_list.bind("<<ListboxSelect>>",self._select_layer)
        row=ttk.Frame(layer_frame);row.pack(fill="x",pady=(5,0))
        for text,cmd in [("Toggle",self.toggle_layer),("Up",lambda:self.move_layer(-1)),("Down",lambda:self.move_layer(1)),("Delete",self.delete_layer)]:
            ttk.Button(row,text=text,command=cmd).pack(side="left",padx=2)

        warn_frame=ttk.LabelFrame(right,text="Import Notes",padding=6);warn_frame.pack(fill="both",expand=False)
        self.warning_text=tk.Text(warn_frame,height=7,wrap="word",font=("TkDefaultFont",9))
        self.warning_text.pack(fill="both",expand=True)
        self.warning_text.configure(state="disabled")

        status=ttk.Frame(self,padding=(6,2));status.pack(fill="x")
        ttk.Label(status,textvariable=self.var_status).pack(side="left",fill="x",expand=True)
        ttk.Label(status,textvariable=self.var_stats).pack(side="right")

    def _transform_changed(self):
        try:
            self.doc.scale=max(0.01,float(self.var_scale.get())/100.0)
            self.doc.offset_x=float(self.var_offset_x.get());self.doc.offset_y=float(self.var_offset_y.get())
            self._refresh_preview_and_stats()
        except (ValueError,tk.TclError): pass

    def _refresh(self):
        self.var_scale.set(self.doc.scale*100);self.var_offset_x.set(self.doc.offset_x);self.var_offset_y.set(self.doc.offset_y)
        self.layer_list.delete(0,"end")
        for i,c in enumerate(self.doc.commands):
            mark="✓" if c.visible else "×"; self.layer_list.insert("end",f"{i:03d} {mark} {OP_NAMES[c.opcode]:9s} {c.label}")
        self.warning_text.configure(state="normal");self.warning_text.delete("1.0","end")
        self.warning_text.insert("end","\n".join(self.doc.warnings) if self.doc.warnings else "No import warnings.")
        self.warning_text.configure(state="disabled")
        self._refresh_preview_and_stats()

    def _refresh_preview_and_stats(self):
        source_cmds=self.doc.transformed_commands()
        if not source_cmds:
            render_to_tk(self.canvas, [])
            self.var_stats.set("No image loaded")
            return
        bbox=commands_bbox(source_cmds);outside=bbox[0]<0 or bbox[1]<0 or bbox[2]>CANVAS_W or bbox[3]>CANVAS_H
        try:
            # Preview the actual transport-visible result, not the raw SVG
            # objects.  This forces alpha through RGB565+A4 palette encoding,
            # frame generation, decoding, and the same Pillow compositor used
            # by PNG export.
            quantized=[quantize_command(c) for c in source_cmds]
            enc=encode_image(quantized)
            preview_cmds=decode_frames(enc.frames) if enc.stats.fits else quantized
            render_to_tk(self.canvas,preview_cmds)
            tag="OVER CANVAS | " if outside else ""
            fit="fits" if enc.stats.fits else "TOO LARGE"
            alpha_colors = len({quantize_palette_color(color) for c in preview_cmds for color in (c.style.fill, c.style.stroke) if color and alpha4(color) < 15})
            alpha_tag = f" | {alpha_colors} alpha" if alpha_colors else ""
            self.var_stats.set(f"{tag}{len(source_cmds)} cmds | {enc.stats.palette_count} colors{alpha_tag} | {enc.stats.base91_chars} chars | {enc.stats.frame_count}/{MAX_MESSAGES} frames {fit}")
        except Exception as exc:
            # Rendering the raw source remains preferable to a blank canvas if
            # a malformed/oversized stream cannot be round-tripped.
            render_to_tk(self.canvas,source_cmds)
            self.var_stats.set(f"Cannot encode: {exc}")

    def open_file(self):
        path=filedialog.askopenfilename(filetypes=[("SVG / MCoreIMG source","*.svg *.svgz *.mci.json *.json"),("All files","*.*")])
        if not path:return
        try:self.doc=load_source(path)
        except Exception as exc:messagebox.showerror("Open failed",str(exc));return
        self.selected_index=None;self._refresh();self.var_status.set(f"Loaded {len(self.doc.commands)} vector commands from {path}")

    def save_file(self):
        path=filedialog.asksaveasfilename(defaultextension=".mci.json",filetypes=[("MCoreIMG SVG source","*.mci.json"),("JSON","*.json")])
        if not path:return
        try:save_source(path,self.doc)
        except Exception as exc:messagebox.showerror("Save failed",str(exc));return
        self.var_status.set(f"Saved editable source to {path}")

    def _export_commands(self)->List[VectorCommand]:
        cmds=self.doc.transformed_commands()
        if not cmds:raise MCIError("There is nothing to export.")
        bbox=commands_bbox(cmds)
        if bbox[0]<0 or bbox[1]<0 or bbox[2]>CANVAS_W or bbox[3]>CANVAS_H:
            raise MCIError(f"Scaled artwork extends outside 720x480 (bounds {bbox[0]:.1f},{bbox[1]:.1f} to {bbox[2]:.1f},{bbox[3]:.1f}). Use Fit or reduce scale.")
        return [c.clone() for c in cmds]

    def export_mci(self):
        try:enc=encode_image(self._export_commands())
        except Exception as exc:messagebox.showerror("Export failed",str(exc));return
        if not enc.stats.fits:
            messagebox.showerror("Image exceeds MeshCore profile",f"Needs {enc.stats.frame_count} messages ({enc.stats.base91_chars} payload characters); maximum is {MAX_MESSAGES} messages / {MAX_PAYLOAD_CHARS} payload characters.")
            return
        path=filedialog.asksaveasfilename(defaultextension=".mci",filetypes=[("MCoreIMG frames","*.mci"),("Text","*.txt")])
        if not path:return
        Path(path).write_text("\n".join(enc.frames)+"\n",encoding="ascii")
        self.var_status.set(f"Exported protocol-v{PROTOCOL_VERSION} image {enc.image_id}: {len(enc.frames)} frame(s) to {path}")

    def export_png(self):
        try:
            quantized=self._export_commands()
            enc=encode_image(quantized)
            # Export exactly what a protocol-v3 Reconstructor receives when
            # the image fits.  For an oversized image, retain the same A4
            # quantization and compositor without requiring a decodable frame
            # set.
            rendered=decode_frames(enc.frames) if enc.stats.fits else quantized
            image=render_to_pillow(rendered)
        except Exception as exc:
            messagebox.showerror("PNG export failed",str(exc));return
        path=filedialog.asksaveasfilename(defaultextension=".png",filetypes=[("PNG","*.png")])
        if not path:return
        image.save(path);image.close();self.var_status.set(f"Exported protocol-v{PROTOCOL_VERSION} decoded PNG to {path}")

    def preview_frames(self):
        try:enc=encode_image(self._export_commands())
        except Exception as exc:messagebox.showerror("Preview failed",str(exc));return
        win=tk.Toplevel(self);win.title(f"MCoreIMG SVG Frames — {enc.image_id}");win.geometry("1000x470")
        text=tk.Text(win,wrap="none",font=("TkFixedFont",10));text.pack(fill="both",expand=True,padx=8,pady=8)
        text.insert("end",f"Build: {CONSTRUCTOR_BUILD}\nTransport limit: {MAX_MESSAGES} messages / {MAX_PAYLOAD_CHARS} payload characters\nProtocol: {PROTOCOL_VERSION}\nPalette: {', '.join(enc.palette)}\nCommands: {enc.stats.command_count}\nTranslated repeats: {enc.stats.repeat_count}\nPayload: {enc.stats.base91_chars} chars\nFrames: {enc.stats.frame_count}/{MAX_MESSAGES}\n\n")
        for i,f in enumerate(enc.frames):text.insert("end",f"Part {i+1}/{len(enc.frames)} — {len(f)} chars\n{f}\n\n")
        text.configure(state="disabled")
        def copy_all():self.clipboard_clear();self.clipboard_append("\n".join(enc.frames))
        ttk.Button(win,text="Copy All Frames",command=copy_all).pack(pady=(0,8))

    def fit_document(self):
        if not self.doc.commands:return
        # Commands imported from SVG are already fitted. This refits after edits/baking.
        self.doc.scale=1.0;self.doc.offset_x=0;self.doc.offset_y=0
        bbox=commands_bbox(self.doc.commands);w=max(1e-9,bbox[2]-bbox[0]);h=max(1e-9,bbox[3]-bbox[1])
        scale=min((CANVAS_W-2*DEFAULT_MARGIN)/w,(CANVAS_H-2*DEFAULT_MARGIN)/h)
        self.doc.scale=scale
        cx=(bbox[0]+bbox[2])/2;cy=(bbox[1]+bbox[3])/2
        self.doc.offset_x=CANVAS_W/2-cx;self.doc.offset_y=CANVAS_H/2-cy
        # offset is applied after centered scaling; compensate correctly.
        self.doc.offset_x=CANVAS_W/2-cx;self.doc.offset_y=CANVAS_H/2-cy
        self._refresh();self.var_status.set("Fitted artwork inside the canvas.")

    def center_document(self):
        if not self.doc.commands:return
        self.doc.offset_x=0;self.doc.offset_y=0
        bbox=self.doc.transformed_bbox();self.doc.offset_x=CANVAS_W/2-(bbox[0]+bbox[2])/2;self.doc.offset_y=CANVAS_H/2-(bbox[1]+bbox[3])/2
        self._refresh();self.var_status.set("Centered artwork on the canvas.")

    def bake_transform(self):
        if not self.doc.commands:return
        self.doc.bake_transform();self._refresh();self.var_status.set("Baked document scale and offset into geometry.")

    def _select_layer(self,_event=None):
        sel=self.layer_list.curselection();self.selected_index=sel[0] if sel else None

    def toggle_layer(self):
        if self.selected_index is None:return
        self.doc.commands[self.selected_index].visible=not self.doc.commands[self.selected_index].visible;self._refresh()

    def move_layer(self,delta:int):
        i=self.selected_index
        if i is None or not 0<=i+delta<len(self.doc.commands):return
        j=i+delta;self.doc.commands[i],self.doc.commands[j]=self.doc.commands[j],self.doc.commands[i];self.selected_index=j;self._refresh();self.layer_list.selection_set(j)

    def delete_layer(self):
        if self.selected_index is None:return
        del self.doc.commands[self.selected_index];self.selected_index=None;self._refresh()


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------


def sample_document()->VectorDocument:
    return VectorDocument([
        VectorCommand(OP_RECT,PaintStyle("#3366CC","#000000",2),{"x":30,"y":30,"w":200,"h":120},"box"),
        VectorCommand(OP_ELLIPSE,PaintStyle("#FFCC00","#000000",2),{"cx":360,"cy":170,"rx":70,"ry":45},"oval"),
        VectorCommand(OP_PATH,PaintStyle(None,"#CC0000",3),{"segments":[
            {"op":SEG_M,"points":[(100,300)]},{"op":SEG_C,"points":[(180,220),(260,380),(340,300)]},
            {"op":SEG_Q,"points":[(430,210),(520,300)]},{"op":SEG_L,"points":[(620,350)]},
        ]},"curve"),
        VectorCommand(OP_RECT,PaintStyle("#3366CC","#000000",2),{"x":50,"y":50,"w":200,"h":120},"translated box"),
    ],source_name="self-test")


def run_self_test():
    doc=sample_document();cmds=[quantize_command(c) for c in doc.commands]
    enc=encode_image(cmds)
    dec=decode_frames(enc.frames)
    def transport_signature(c:VectorCommand):
        style=c.style.normalized()
        return {
            "opcode":c.opcode,
            "fill":quantize_palette_color(style.fill) if style.fill else None,
            "stroke":quantize_palette_color(style.stroke) if style.stroke else None,
            "stroke_width":int(style.stroke_width),
            "fill_rule":style.fill_rule,
            "geom":c.geom,
        }
    assert [transport_signature(c) for c in dec]==[transport_signature(c) for c in cmds]
    assert enc.stats.repeat_count>=1
    assert MAX_MESSAGES == 10
    assert MAX_PAYLOAD_CHARS == 1350
    # Exercise the full ten-message envelope, including a 150-character frame.
    envelope_commands=[]
    ten_frame_image=None
    for i in range(1, 64):
        x=(i*37)%700; y=(i*53)%460
        segments=[{"op":SEG_M,"points":[(x,y)]}]
        for j in range(1,8):
            segments.append({"op":SEG_C,"points":[
                ((x+j*11+i)%720,(y+j*17+i*2)%480),
                ((x+j*19+i*3)%720,(y+j*23+i)%480),
                ((x+j*29+i)%720,(y+j*31+i*4)%480),
            ]})
        envelope_commands.append(VectorCommand(
            OP_PATH, PaintStyle(None, f"#{(i*7919)&0xFFFFFF:06X}", 1+(i%4)),
            {"segments":segments}, f"envelope-{i}"))
        candidate=encode_image(envelope_commands)
        if candidate.stats.frame_count == MAX_MESSAGES:
            ten_frame_image=candidate
            break
        if candidate.stats.frame_count > MAX_MESSAGES:
            break
    assert ten_frame_image is not None
    assert ten_frame_image.stats.fits
    assert max(map(len,ten_frame_image.frames)) == MESSAGE_LEN
    assert len(decode_frames(ten_frame_image.frames)) == len(envelope_commands)
    # SVG parser and default fit test.
    svg='''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 100"><rect width="1000" height="100" fill="#123456"/><path d="M0 50 C250 0 750 100 1000 50" fill="none" stroke="red"/></svg>'''
    temp=Path("/tmp/mcoreimg-svg-selftest.svg");temp.write_text(svg)
    imported=import_svg(temp);temp.unlink(missing_ok=True)
    bbox=commands_bbox(imported.commands)
    assert bbox[0]>=-0.01 and bbox[1]>=-0.01 and bbox[2]<=CANVAS_W+0.01 and bbox[3]<=CANVAS_H+0.01
    # SVG open subpaths are implicitly closed for filling.  This is common in
    # hand-authored/minified SVGs and was the reason Cartman rendered as only
    # a few outlines before this regression test was added.
    open_fill = VectorCommand(OP_PATH, PaintStyle("#FF0000", None, 1), {"segments":[
        {"op":SEG_M,"points":[(10,10)]},
        {"op":SEG_L,"points":[(50,10)]},
        {"op":SEG_L,"points":[(30,50)]},
    ]}, "open-filled-triangle")
    if Image is not None:
        open_fill_img = render_to_pillow([open_fill])
        assert open_fill_img.getpixel((30,25)) != ImageColor.getrgb(BACKGROUND)
    # Alpha-channel regression: 50% red/green/blue circles must retain alpha
    # through import, palette quantization, transport, decode, and compositing.
    alpha_svg='''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><circle r="32" cx="35" cy="65" fill="#F00" opacity="0.5"/><circle r="32" cx="65" cy="65" fill="#0F0" opacity="0.5"/><circle r="32" cx="50" cy="35" fill="#00F" opacity="0.5"/></svg>'''
    alpha_temp=Path("/tmp/mcoreimg-alpha-selftest.svg");alpha_temp.write_text(alpha_svg)
    alpha_doc=import_svg(alpha_temp);alpha_temp.unlink(missing_ok=True)
    assert all(alpha4(c.style.fill or "#000000") == 8 for c in alpha_doc.commands)
    alpha_cmds=[quantize_command(c) for c in alpha_doc.commands]
    alpha_encoded=encode_image(alpha_cmds)
    alpha_decoded=decode_frames(alpha_encoded.frames)
    assert all(alpha4(c.style.fill or "#000000") == 8 for c in alpha_decoded)
    assert alpha_encoded.stats.frame_count == 1
    if Image is not None:
        alpha_image=render_to_pillow(alpha_decoded)
        # A blended overlap must not equal a single-circle region and must
        # contain contributions from multiple source colors.
        single = alpha_image.getpixel((220,330))
        overlap = alpha_image.getpixel((360,330))
        assert overlap != single
        assert overlap[0] < 255 and overlap[1] < 255 and overlap[2] < 255
    # Frame corruption detection.
    broken=enc.frames.copy();pos=FRAME_HEADER_LEN
    broken[0]=broken[0][:pos]+BASE91[(BASE91_INDEX[broken[0][pos]]+1)%91]+broken[0][pos+1:]
    try:decode_frames(broken)
    except FrameError:pass
    else:raise AssertionError("Corrupted frame was accepted")
    print("MCoreIMG SVG Constructor self-test: PASS")
    print(f"commands={enc.stats.command_count} palette={enc.stats.palette_count} bits={enc.stats.bit_count} payload={enc.stats.base91_chars} frames={enc.stats.frame_count} repeats={enc.stats.repeat_count}")



# ---------------------------------------------------------------------------
# HYBRID OVERRIDE LAYER: primitives and repeated command groups
# ---------------------------------------------------------------------------
#
# IMPORTANT MAINTENANCE NOTE
# --------------------------
# This block intentionally redefines several helpers and codec entry points
# from the vector-only foundation above. From this point onward, later global
# definitions supersede earlier ones. The earlier code remains useful as the
# imported-SVG/model/rendering foundation and as historical reference, but the
# final encoder is defined below in the protocol-v5 layer.
#
# Refactor target: extract only the final active definitions into a codec_v5
# module, then delete the superseded implementations after fixture parity.

# The hybrid foundation keeps the RGB565+A4 palette and ten-message envelope, and
# adds two orthogonal compression tools:
#   * compact legacy/manual primitives, used only when their actual encoded
#     representation is smaller than the equivalent generic vector commands;
#   * translated group-copy records for repeated SVGs or repeated contiguous
#     command groups, including nonadjacent copies.
PROTOCOL_VERSION = 5
SOURCE_VERSION = 5
CONSTRUCTOR_BUILD = "2026.08.02-svg-v5.1-DOCUMENTED-LOCALSPACE-HYBRID-10MSG"

OP_PRIMITIVE = 6
OP_NAMES[OP_PRIMITIVE] = "Primitive"

PRIM_TEXT = 0
PRIM_TRIANGLE_OUTLINE = 1
PRIM_TRIANGLE_FILL = 2
PRIM_ARROW = 3
PRIM_STAR = 4
PRIM_ARC = 5
PRIM_YAGI = 6
PRIM_DISH = 7
PRIM_RADIO = 8
PRIM_RADIO_WAVES = 9
PRIM_MOON = 10
PRIM_DOUBLE_BOX = 11

PRIMITIVE_NAMES = {
    PRIM_TEXT: "Text",
    PRIM_TRIANGLE_OUTLINE: "Triangle Outline",
    PRIM_TRIANGLE_FILL: "Triangle Fill",
    PRIM_ARROW: "Arrow",
    PRIM_STAR: "Star",
    PRIM_ARC: "SemiCircle / Arc",
    PRIM_YAGI: "Yagi Antenna",
    PRIM_DISH: "Dish Antenna",
    PRIM_RADIO: "Radio Transceiver",
    PRIM_RADIO_WAVES: "Radio Waves",
    PRIM_MOON: "Moon",
    PRIM_DOUBLE_BOX: "DoubleBox",
}
PRIMITIVE_BY_NAME = {name: kind for kind, name in PRIMITIVE_NAMES.items()}

TEXT_ALPHABET = " 0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ-+./?:,()[]#_=@!&%"
TEXT_INDEX = {ch: i for i, ch in enumerate(TEXT_ALPHABET)}
MAX_TEXT_LEN = 63
assert len(TEXT_ALPHABET) <= 64

REC_NORMAL = 0
REC_SINGLE_REPEAT = 1
REC_GROUP_REPEAT = 2
REC_RESERVED = 3

MOON_CRATER_POINTS = [
    (1 / 5, 1 / 4, 3), (3 / 7, 5 / 8, 5), (1 / 4, 7 / 9, 4),
    (4 / 5, 2 / 7, 2), (7 / 12, 1 / 5, 6), (2 / 3, 2 / 5, 3),
    (5 / 8, 3 / 4, 4), (7 / 20, 3 / 7, 2), (3 / 20, 5 / 9, 5),
    (3 / 4, 3 / 5, 3),
]


def _clean_primitive_text(value: Any) -> str:
    text = str(value).upper()[:MAX_TEXT_LEN]
    return "".join(ch if ch in TEXT_INDEX else " " for ch in text).rstrip()


def _rotate_quarter(point: Tuple[float, float], origin: Tuple[float, float], turns: int) -> Tuple[float, float]:
    """Rotate in 90-degree clockwise screen-coordinate steps."""
    px, py = point
    ox, oy = origin
    dx, dy = px - ox, py - oy
    for _ in range(int(turns) % 4):
        dx, dy = -dy, dx
    return ox + dx, oy + dy


def _regular_polygon(cx: float, cy: float, radius: float, sides: int, rotation_deg: float) -> List[Tuple[float, float]]:
    return [
        (
            cx + math.cos(math.radians(rotation_deg + 360 * i / sides)) * radius,
            cy + math.sin(math.radians(rotation_deg + 360 * i / sides)) * radius,
        )
        for i in range(sides)
    ]


def _arc_points(cx: float, cy: float, radius: float, start_angle: int, arc_degrees: int) -> List[Tuple[float, float]]:
    radius = max(1.0, float(radius))
    start = int(start_angle) % 360
    sweep = max(0, min(360, int(arc_degrees)))
    if sweep <= 0:
        return []
    values = list(range(start, start + sweep + 1, 5))
    if not values or values[-1] != start + sweep:
        values.append(start + sweep)
    return [
        (
            cx + radius * math.cos(math.radians(angle % 360)),
            cy - radius * math.sin(math.radians(angle % 360)),
        )
        for angle in values
    ]


def _primitive_kind(cmd: VectorCommand) -> int:
    return int(cmd.geom.get("kind", -1))


def _primitive_anchor(cmd: VectorCommand) -> Tuple[float, float]:
    g = cmd.geom
    if _primitive_kind(cmd) == PRIM_DOUBLE_BOX:
        return float(g["x1"]), float(g["y1"])
    return float(g.get("x", 0)), float(g.get("y", 0))


def _line_style_from(cmd: VectorCommand, width: Optional[float] = None) -> PaintStyle:
    s = cmd.style.normalized()
    color = s.stroke or s.fill or "#000000"
    return PaintStyle(None, color, width if width is not None else max(1.0, s.stroke_width), "nonzero")


def _fill_style_from(cmd: VectorCommand) -> PaintStyle:
    s = cmd.style.normalized()
    color = s.fill or s.stroke or "#000000"
    return PaintStyle(color, None, 0, "nonzero")


def primitive_to_vectors(cmd: VectorCommand) -> Optional[List[VectorCommand]]:
    """Expand one old/manual primitive into transport-equivalent generic vectors.

    Text deliberately has no generic-vector alternative because converting a
    font into outlines would be much larger and platform-dependent. Every
    other primitive is expanded deterministically, so the optimizer can compare
    exact codec costs and the renderer can guarantee visual parity.
    """
    if cmd.opcode != OP_PRIMITIVE:
        return [cmd.clone()]
    validate_command(cmd)
    g = cmd.geom
    kind = _primitive_kind(cmd)
    label = cmd.label or PRIMITIVE_NAMES.get(kind, "Primitive")
    line_style = _line_style_from(cmd)
    fill_style = _fill_style_from(cmd)
    x = float(g.get("x", 0))
    y = float(g.get("y", 0))
    orientation = int(g.get("orientation", 0)) % 4
    scale = max(1, int(g.get("scale", 1)))

    def line(p1: Tuple[float, float], p2: Tuple[float, float], suffix: str = "") -> VectorCommand:
        return VectorCommand(OP_LINE, copy.deepcopy(line_style), {"p1": p1, "p2": p2}, f"{label}{suffix}")

    if kind == PRIM_TEXT:
        return None

    if kind in {PRIM_TRIANGLE_OUTLINE, PRIM_TRIANGLE_FILL}:
        points = _regular_polygon(x, y, 18 * scale, 3, -90 + orientation * 90)
        style = fill_style if kind == PRIM_TRIANGLE_FILL else line_style
        return [VectorCommand(OP_POLYGON, style, {"points": points}, label)]

    if kind == PRIM_ARROW:
        pts = [
            (x, y), (x - 8 * scale, y + 18 * scale), (x - 3 * scale, y + 18 * scale),
            (x - 3 * scale, y + 45 * scale), (x + 3 * scale, y + 45 * scale),
            (x + 3 * scale, y + 18 * scale), (x + 8 * scale, y + 18 * scale),
        ]
        pts = [_rotate_quarter(p, (x, y), orientation) for p in pts]
        return [VectorCommand(OP_POLYGON, fill_style, {"points": pts}, label)]

    if kind == PRIM_STAR:
        radius = max(1, int(g.get("radius", 35))) * scale
        diagonal = round(radius / math.sqrt(2))
        return [
            line((x, y - radius), (x, y + radius), " vertical"),
            line((x - radius, y), (x + radius, y), " horizontal"),
            line((x - diagonal, y - diagonal), (x + diagonal, y + diagonal), " diagonal 1"),
            line((x + diagonal, y - diagonal), (x - diagonal, y + diagonal), " diagonal 2"),
        ]

    if kind == PRIM_ARC:
        pts = _arc_points(x, y, max(1, int(g.get("radius", 35))) * scale,
                          int(g.get("start_angle", 0)), int(g.get("arc_degrees", 180)))
        if len(pts) < 2:
            pts = [(x, y), (x + 1, y)]
        return [VectorCommand(OP_POLYLINE, line_style, {"points": pts}, label)]

    if kind == PRIM_YAGI:
        axis_start = _rotate_quarter((x + 30 * scale, y), (x, y), orientation)
        axis_end = _rotate_quarter((x, y + 100 * scale), (x, y), orientation)
        style = PaintStyle(None, line_style.stroke, max(1, 2 * scale), "nonzero")
        result = [VectorCommand(OP_LINE, style, {"p1": axis_start, "p2": axis_end}, f"{label} boom")]
        for index, t in enumerate((0.15, 0.35, 0.55, 0.75), 1):
            ax = (1 - t) * (x + 30 * scale) + t * x
            ay = (1 - t) * y + t * (y + 100 * scale)
            p1 = _rotate_quarter((ax - 8 * scale, ay - 3 * scale), (x, y), orientation)
            p2 = _rotate_quarter((ax + 8 * scale, ay + 3 * scale), (x, y), orientation)
            result.append(VectorCommand(OP_LINE, copy.deepcopy(style), {"p1": p1, "p2": p2}, f"{label} element {index}"))
        return result

    if kind == PRIM_DISH:
        facing = orientation % 2
        radius = 40 * scale
        flip = -1 if facing == 0 else 1
        bowl = []
        for angle in range(90, 181, 5):
            theta = math.radians(angle)
            bowl.append((x + round(radius * math.cos(theta)) * flip, y + round(radius * math.sin(theta))))
        width_style = PaintStyle(None, line_style.stroke, 3, "nonzero")
        result: List[VectorCommand] = [VectorCommand(OP_POLYLINE, width_style, {"points": bowl}, f"{label} reflector")]
        result.append(VectorCommand(OP_LINE, copy.deepcopy(width_style), {"p1": (x, y), "p2": (x - radius * flip, y)}, f"{label} support 1"))
        result.append(VectorCommand(OP_LINE, copy.deepcopy(width_style), {"p1": (x, y), "p2": (x, y + radius)}, f"{label} support 2"))
        mid_x = round((-radius / math.sqrt(2)) * flip)
        mid_y = round(radius / math.sqrt(2))
        result.append(VectorCommand(OP_LINE, copy.deepcopy(width_style), {"p1": (x + mid_x, y + mid_y), "p2": (x + mid_x, y + mid_y + 30 * scale)}, f"{label} mast"))
        result.append(VectorCommand(OP_ELLIPSE, fill_style, {"cx": x, "cy": y, "rx": 5 * scale, "ry": 5 * scale}, f"{label} hub"))
        return result

    if kind == PRIM_RADIO:
        # Preserve the proven old geometry. Orientation was historically stored
        # but intentionally ignored by the renderer.
        width_style = PaintStyle(None, line_style.stroke, 3, "nonzero")
        return [
            VectorCommand(OP_RECT, copy.deepcopy(width_style), {"x": x, "y": y, "w": 50 * scale, "h": 20 * scale}, f"{label} body"),
            VectorCommand(OP_ELLIPSE, copy.deepcopy(width_style), {"cx": x + 10 * scale, "cy": y + 10 * scale, "rx": 5 * scale, "ry": 5 * scale}, f"{label} knob"),
            VectorCommand(OP_RECT, copy.deepcopy(width_style), {"x": x + 25 * scale, "y": y + 5 * scale, "w": 20 * scale, "h": 10 * scale}, f"{label} screen"),
        ]

    if kind == PRIM_RADIO_WAVES:
        radius = max(1, int(g.get("radius", 10)))
        base = radius * scale
        spacing = 2 * radius * scale
        result = []
        for index, offset in enumerate((0, spacing, 2 * spacing), 1):
            pts = _arc_points(x, y, base + offset, int(g.get("start_angle", 0)), int(g.get("arc_degrees", 180)))
            if len(pts) >= 2:
                result.append(VectorCommand(OP_POLYLINE, copy.deepcopy(line_style), {"points": pts}, f"{label} {index}"))
        return result or [line((x, y), (x + 1, y))]

    if kind == PRIM_MOON:
        radius = 36 * scale
        result = [VectorCommand(OP_ELLIPSE, fill_style, {"cx": x, "cy": y, "rx": radius, "ry": radius}, f"{label} body")]
        crater = quantize_palette_color(str(g.get("crater_color", "#808080")))
        crater_style = PaintStyle(None, crater, 3, "nonzero")
        left, top, diameter = x - radius, y - radius, radius * 2
        for index, (fx, fy, base_radius) in enumerate(MOON_CRATER_POINTS, 1):
            cx = left + diameter * fx
            cy = top + diameter * fy
            result.append(VectorCommand(OP_ELLIPSE, copy.deepcopy(crater_style), {"cx": cx, "cy": cy, "rx": base_radius * scale, "ry": base_radius * scale}, f"{label} crater {index}"))
        return result

    if kind == PRIM_DOUBLE_BOX:
        x1, y1 = float(g["x1"]), float(g["y1"])
        x2, y2 = float(g["x2"]), float(g["y2"])
        left, right = min(x1, x2), max(x1, x2)
        top, bottom = min(y1, y2), max(y1, y2)
        divider = top + (bottom - top) * max(0, min(100, int(g.get("percent", 50)))) / 100.0
        return [
            VectorCommand(OP_RECT, copy.deepcopy(line_style), {"x": left, "y": top, "w": max(1, right - left), "h": max(1, bottom - top)}, f"{label} box"),
            VectorCommand(OP_LINE, copy.deepcopy(line_style), {"p1": (left, divider), "p2": (right, divider)}, f"{label} divider"),
        ]

    raise MCIError(f"Unknown primitive kind {kind}.")


_v3_validate_command = validate_command
_v3_command_points = command_points
_v3_transform_command = transform_command
_v3_quantize_command = quantize_command
_v3_build_palette = build_palette
_v3_translate_command = translate_command
_v3_geom_translation = geom_translation
_v3_render_to_pillow = render_to_pillow
_BaseConstructorApp = ConstructorApp


def validate_command(cmd: VectorCommand) -> None:
    if cmd.opcode != OP_PRIMITIVE:
        _v3_validate_command(cmd)
        return
    kind = _primitive_kind(cmd)
    if kind not in PRIMITIVE_NAMES:
        raise MCIError(f"Unknown primitive kind {kind}.")
    s = cmd.style.normalized()
    if s.fill is None and s.stroke is None:
        raise MCIError("A primitive must have a fill or stroke color.")
    g = cmd.geom

    def require_number(name: str) -> float:
        if name not in g:
            raise MCIError(f"{PRIMITIVE_NAMES[kind]} requires {name}.")
        return float(g[name])

    if kind == PRIM_DOUBLE_BOX:
        for key in ("x1", "y1", "x2", "y2"):
            require_number(key)
        percent = int(g.get("percent", 50))
        if not 0 <= percent <= 100:
            raise MCIError("DoubleBox percent must be 0..100.")
    else:
        require_number("x"); require_number("y")
    if kind == PRIM_TEXT:
        if len(_clean_primitive_text(g.get("text", ""))) > MAX_TEXT_LEN:
            raise MCIError("Text is too long.")
    if kind in {PRIM_TRIANGLE_OUTLINE, PRIM_TRIANGLE_FILL, PRIM_ARROW, PRIM_YAGI, PRIM_DISH, PRIM_RADIO}:
        if not 0 <= int(g.get("orientation", 0)) <= 3:
            raise MCIError("Orientation must be 0..3.")
        if not 1 <= int(g.get("scale", 1)) <= 64:
            raise MCIError("Scale must be 1..64.")
    if kind in {PRIM_STAR, PRIM_ARC, PRIM_RADIO_WAVES}:
        if not 1 <= int(g.get("radius", 1)) <= 128:
            raise MCIError("Radius must be 1..128.")
        if not 1 <= int(g.get("scale", 1)) <= 64:
            raise MCIError("Scale must be 1..64.")
    if kind in {PRIM_ARC, PRIM_RADIO_WAVES}:
        if not 0 <= int(g.get("start_angle", 0)) <= 360:
            raise MCIError("Start angle must be 0..360.")
        if not 0 <= int(g.get("arc_degrees", 0)) <= 360:
            raise MCIError("Arc degrees must be 0..360.")
    if kind == PRIM_MOON:
        if not 1 <= int(g.get("scale", 1)) <= 64:
            raise MCIError("Scale must be 1..64.")
        normalize_hex(str(g.get("crater_color", "#808080")))


def command_points(cmd: VectorCommand) -> List[Tuple[float, float]]:
    if cmd.opcode != OP_PRIMITIVE:
        return _v3_command_points(cmd)
    kind = _primitive_kind(cmd)
    g = cmd.geom
    if kind == PRIM_TEXT:
        x, y = float(g["x"]), float(g["y"])
        text = _clean_primitive_text(g.get("text", ""))
        return [(x, y), (x + max(1, len(text)) * 13, y + 22)]
    expanded = primitive_to_vectors(cmd)
    return [p for vector in (expanded or []) for p in _v3_command_points(vector)]


def transform_command(cmd: VectorCommand, m: Matrix) -> VectorCommand:
    if cmd.opcode != OP_PRIMITIVE:
        return _v3_transform_command(cmd, m)
    out = cmd.clone()
    g = out.geom
    kind = _primitive_kind(out)
    a, b, c, d, _e, _f = m
    sx, sy = math.hypot(a, b), math.hypot(c, d)
    scale_factor = max(1e-9, (sx + sy) / 2.0)
    out.style.stroke_width *= scale_factor
    if kind == PRIM_DOUBLE_BOX:
        g["x1"], g["y1"] = apply_mat(m, (float(g["x1"]), float(g["y1"])))
        g["x2"], g["y2"] = apply_mat(m, (float(g["x2"]), float(g["y2"])))
    else:
        g["x"], g["y"] = apply_mat(m, (float(g["x"]), float(g["y"])))
    if "scale" in g:
        g["scale"] = max(1, min(64, int(round(float(g["scale"]) * scale_factor))))
    if "radius" in g and "scale" not in g:
        g["radius"] = max(1, min(128, int(round(float(g["radius"]) * scale_factor))))
    return out


def quantize_command(cmd: VectorCommand) -> VectorCommand:
    if cmd.opcode != OP_PRIMITIVE:
        return _v3_quantize_command(cmd)
    out = cmd.clone()
    out.style = out.style.normalized()
    if out.style.stroke:
        out.style.stroke_width = max(1, min(64, int(round(out.style.stroke_width))))
    g = out.geom
    kind = _primitive_kind(out)
    g["kind"] = kind
    if kind == PRIM_DOUBLE_BOX:
        g["x1"] = clamp_int(g["x1"], 0, CANVAS_W - 1)
        g["y1"] = clamp_int(g["y1"], 0, CANVAS_H - 1)
        g["x2"] = clamp_int(g["x2"], 0, CANVAS_W - 1)
        g["y2"] = clamp_int(g["y2"], 0, CANVAS_H - 1)
        g["percent"] = max(0, min(100, int(round(g.get("percent", 50)))))
    else:
        g["x"] = clamp_int(g["x"], 0, CANVAS_W - 1)
        g["y"] = clamp_int(g["y"], 0, CANVAS_H - 1)
    if "orientation" in g:
        g["orientation"] = int(g["orientation"]) % 4
    if "scale" in g:
        g["scale"] = max(1, min(64, int(round(g["scale"]))))
    if "radius" in g:
        g["radius"] = max(1, min(128, int(round(g["radius"]))))
    if "start_angle" in g:
        g["start_angle"] = max(0, min(360, int(round(g["start_angle"]))))
    if "arc_degrees" in g:
        g["arc_degrees"] = max(0, min(360, int(round(g["arc_degrees"]))))
    if kind == PRIM_TEXT:
        g["text"] = _clean_primitive_text(g.get("text", ""))
    if kind == PRIM_MOON:
        g["crater_color"] = quantize_palette_color(str(g.get("crater_color", "#808080")))
    validate_command(out)
    return out


def build_palette(commands: Sequence[VectorCommand]) -> List[str]:
    palette: List[str] = []
    for command in commands:
        for color in (command.style.fill, command.style.stroke):
            if color:
                quant = quantize_palette_color(color)
                if quant not in palette:
                    palette.append(quant)
        if command.opcode == OP_PRIMITIVE and _primitive_kind(command) == PRIM_MOON:
            crater = quantize_palette_color(str(command.geom.get("crater_color", "#808080")))
            if crater not in palette:
                palette.append(crater)
    if not palette:
        palette = ["#000000"]
    if len(palette) > MAX_PALETTE:
        raise MCIError(f"Image needs {len(palette)} colors; protocol v{PROTOCOL_VERSION} supports {MAX_PALETTE}.")
    return palette


def translate_command(cmd: VectorCommand, dx: int, dy: int) -> VectorCommand:
    return transform_command(cmd, mat_translate(dx, dy))


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
    if prev.opcode == OP_PRIMITIVE and _primitive_kind(prev) != _primitive_kind(cur):
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


def _load_manual_font(size: int = 20):
    try:
        from PIL import ImageFont
    except ImportError:
        return None
    candidates = [
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "DejaVuSans-Bold.ttf",
    ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def render_to_pillow(commands: Sequence[VectorCommand]):
    """Render generic vectors and old primitives through one alpha compositor."""
    if Image is None or ImageDraw is None:
        raise MCIError("Pillow is required for PNG export.")
    background = color_to_rgba(BACKGROUND) or (255, 255, 255, 255)
    image = Image.new("RGBA", (CANVAS_W, CANVAS_H), background)
    font = _load_manual_font(20)

    def composite_mask(draw_mask_fn, color: Tuple[int, int, int, int]) -> None:
        nonlocal image
        red, green, blue, alpha = color
        if alpha <= 0:
            return
        mask = Image.new("L", (CANVAS_W, CANVAS_H), 0)
        draw = ImageDraw.Draw(mask)
        draw_mask_fn(draw)
        if alpha < 255:
            mask = mask.point(lambda coverage, a=alpha: (coverage * a + 127) // 255)
        layer = Image.new("RGBA", (CANVAS_W, CANVAS_H), (red, green, blue, 255))
        layer.putalpha(mask)
        image = Image.alpha_composite(image, layer)

    def render_one(cmd: VectorCommand) -> None:
        if cmd.opcode == OP_PRIMITIVE:
            kind = _primitive_kind(cmd)
            if kind == PRIM_TEXT:
                color = color_to_rgba(cmd.style.fill or cmd.style.stroke or "#000000")
                if color:
                    x, y = int(cmd.geom["x"]), int(cmd.geom["y"])
                    text = _clean_primitive_text(cmd.geom.get("text", ""))
                    composite_mask(lambda d, x=x, y=y, text=text: d.text((x, y), text, font=font, fill=255), color)
                return
            for vector in primitive_to_vectors(cmd) or []:
                render_one(quantize_command(vector))
            return

        s = cmd.style.normalized()
        g = cmd.geom
        fill = color_to_rgba(s.fill) if s.fill else None
        stroke = color_to_rgba(s.stroke) if s.stroke else None
        width = max(1, int(round(s.stroke_width))) if stroke else 1
        if cmd.opcode == OP_RECT:
            box = [g["x"], g["y"], g["x"] + g["w"], g["y"] + g["h"]]
            if fill: composite_mask(lambda d, box=box: d.rectangle(box, fill=255), fill)
            if stroke: composite_mask(lambda d, box=box, width=width: d.rectangle(box, outline=255, width=width), stroke)
        elif cmd.opcode == OP_ELLIPSE:
            box = [g["cx"] - g["rx"], g["cy"] - g["ry"], g["cx"] + g["rx"], g["cy"] + g["ry"]]
            if fill: composite_mask(lambda d, box=box: d.ellipse(box, fill=255), fill)
            if stroke: composite_mask(lambda d, box=box, width=width: d.ellipse(box, outline=255, width=width), stroke)
        elif cmd.opcode == OP_LINE:
            color = stroke or fill
            if color:
                pts = [tuple(g["p1"]), tuple(g["p2"])]
                composite_mask(lambda d, pts=pts, width=width: d.line(pts, fill=255, width=width), color)
        elif cmd.opcode == OP_POLYLINE:
            color = stroke or fill
            if color:
                pts = [tuple(point) for point in g["points"]]
                composite_mask(lambda d, pts=pts, width=width: d.line(pts, fill=255, width=width), color)
        elif cmd.opcode == OP_POLYGON:
            pts = [tuple(point) for point in g["points"]]
            if fill: composite_mask(lambda d, pts=pts: d.polygon(pts, fill=255), fill)
            if stroke and pts:
                closed = pts + [pts[0]]
                composite_mask(lambda d, pts=closed, width=width: d.line(pts, fill=255, width=width), stroke)
        elif cmd.opcode == OP_PATH:
            for pts, closed in flatten_path(g["segments"], 16):
                if fill and len(pts) >= 3:
                    composite_mask(lambda d, pts=pts: d.polygon(pts, fill=255), fill)
                if stroke and len(pts) >= 2:
                    line_pts = pts + ([pts[0]] if closed else [])
                    composite_mask(lambda d, pts=line_pts, width=width: d.line(pts, fill=255, width=width), stroke)

    for command in commands:
        render_one(command)
    return image.convert("RGB")


# ---- v4 bitstream ---------------------------------------------------------


def _ue_length(value: int) -> int:
    number = int(value) + 1
    return number.bit_length() * 2 - 1


def _state_params(state: PointState) -> Dict[str, int]:
    params = getattr(state, "params", None)
    if params is None:
        params = {}
        setattr(state, "params", params)
    return params


def _write_stateful_fixed_v4(w: BitWriter, state: PointState, key: str, value: int, width: int) -> None:
    params = _state_params(state)
    same = params.get(key) == value
    w.bit(same)
    if not same:
        w.bits_n(value, width)
        params[key] = value


def _read_stateful_fixed_v4(r: BitReader, state: PointState, key: str, width: int) -> int:
    params = _state_params(state)
    if r.bit():
        if key not in params:
            raise MCIError(f"Primitive state {key!r} reused before initialization.")
        return params[key]
    value = r.bits_n(width)
    params[key] = value
    return value


def _write_stateful_ue_v4(w: BitWriter, state: PointState, key: str, value: int) -> None:
    params = _state_params(state)
    same = params.get(key) == value
    w.bit(same)
    if not same:
        w.ue(value)
        params[key] = value


def _read_stateful_ue_v4(r: BitReader, state: PointState, key: str, max_value: int) -> int:
    params = _state_params(state)
    if r.bit():
        if key not in params:
            raise MCIError(f"Primitive state {key!r} reused before initialization.")
        return params[key]
    value = r.ue(max_value)
    params[key] = value
    return value


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
    kind = _primitive_kind(cmd)
    _write_stateful_fixed_v4(w, state, "primitive_kind", kind, 4)
    if kind == PRIM_DOUBLE_BOX:
        write_point(w, state, (int(g["x1"]), int(g["y1"])))
        write_point(w, state, (int(g["x2"]), int(g["y2"])))
        _write_stateful_ue_v4(w, state, "percent", int(g.get("percent", 50)))
        return
    write_point(w, state, (int(g["x"]), int(g["y"])))
    if kind == PRIM_TEXT:
        text = _clean_primitive_text(g.get("text", ""))
        w.ue(len(text))
        for char in text: w.bits_n(TEXT_INDEX[char], 6)
    elif kind in {PRIM_TRIANGLE_OUTLINE, PRIM_TRIANGLE_FILL, PRIM_ARROW, PRIM_YAGI, PRIM_DISH, PRIM_RADIO}:
        _write_stateful_fixed_v4(w, state, f"orientation_{kind}", int(g.get("orientation", 0)) % 4, 2)
        _write_stateful_ue_v4(w, state, f"scale_{kind}", int(g.get("scale", 1)) - 1)
    elif kind == PRIM_STAR:
        _write_stateful_ue_v4(w, state, "star_radius", int(g.get("radius", 35)) - 1)
        _write_stateful_ue_v4(w, state, "star_scale", int(g.get("scale", 1)) - 1)
    elif kind in {PRIM_ARC, PRIM_RADIO_WAVES}:
        prefix = "arc" if kind == PRIM_ARC else "waves"
        _write_stateful_ue_v4(w, state, f"{prefix}_radius", int(g.get("radius", 35)) - 1)
        _write_stateful_ue_v4(w, state, f"{prefix}_scale", int(g.get("scale", 1)) - 1)
        _write_stateful_fixed_v4(w, state, f"{prefix}_start", int(g.get("start_angle", 0)), 9)
        _write_stateful_fixed_v4(w, state, f"{prefix}_degrees", int(g.get("arc_degrees", 180)), 9)
    elif kind == PRIM_MOON:
        _write_stateful_ue_v4(w, state, "moon_scale", int(g.get("scale", 1)) - 1)
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
    kind = _read_stateful_fixed_v4(r, state, "primitive_kind", 4)
    if kind not in PRIMITIVE_NAMES:
        raise MCIError(f"Invalid primitive kind {kind}.")
    g: Dict[str, Any] = {"kind": kind}
    if kind == PRIM_DOUBLE_BOX:
        g["x1"], g["y1"] = read_point(r, state)
        g["x2"], g["y2"] = read_point(r, state)
        g["percent"] = _read_stateful_ue_v4(r, state, "percent", 100)
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
        g["orientation"] = _read_stateful_fixed_v4(r, state, f"orientation_{kind}", 2)
        g["scale"] = _read_stateful_ue_v4(r, state, f"scale_{kind}", 63) + 1
    elif kind == PRIM_STAR:
        g["radius"] = _read_stateful_ue_v4(r, state, "star_radius", 127) + 1
        g["scale"] = _read_stateful_ue_v4(r, state, "star_scale", 63) + 1
    elif kind in {PRIM_ARC, PRIM_RADIO_WAVES}:
        prefix = "arc" if kind == PRIM_ARC else "waves"
        g["radius"] = _read_stateful_ue_v4(r, state, f"{prefix}_radius", 127) + 1
        g["scale"] = _read_stateful_ue_v4(r, state, f"{prefix}_scale", 63) + 1
        g["start_angle"] = _read_stateful_fixed_v4(r, state, f"{prefix}_start", 9)
        g["arc_degrees"] = _read_stateful_fixed_v4(r, state, f"{prefix}_degrees", 9)
    elif kind == PRIM_MOON:
        g["scale"] = _read_stateful_ue_v4(r, state, "moon_scale", 63) + 1
        width = max(1, (len(palette) - 1).bit_length())
        index = r.bits_n(width)
        if index >= len(palette): raise MCIError("Invalid moon crater palette index.")
        g["crater_color"] = palette[index]
    return g


@dataclass
class EncoderStateV4:
    point_states: Dict[int, PointState] = field(default_factory=lambda: {op: PointState() for op in OP_NAMES})
    style_states: Dict[int, Tuple[Any, ...]] = field(default_factory=dict)
    recent: Dict[int, VectorCommand] = field(default_factory=dict)
    previous_opcode: Optional[int] = None

    def clone(self) -> "EncoderStateV4":
        return copy.deepcopy(self)


def _update_history(state: EncoderStateV4, commands: Sequence[VectorCommand]) -> None:
    for command in commands:
        state.recent[command.opcode] = command.clone()
        state.previous_opcode = command.opcode


def _write_normal_record(w: BitWriter, state: EncoderStateV4, cmd: VectorCommand, palette: Sequence[str]) -> None:
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


def _write_single_repeat_record(w: BitWriter, state: EncoderStateV4, cmd: VectorCommand, delta: Tuple[int, int]) -> None:
    w.bits_n(REC_SINGLE_REPEAT, 2)
    w.bits_n(cmd.opcode, 3)
    w.rice_signed(delta[0], 2)
    w.rice_signed(delta[1], 2)
    _update_history(state, [cmd])


def _best_single_or_normal(cmd: VectorCommand, state: EncoderStateV4, palette: Sequence[str]) -> Tuple[BitWriter, EncoderStateV4, bool]:
    normal_writer = BitWriter(); normal_state = state.clone()
    _write_normal_record(normal_writer, normal_state, cmd, palette)
    repeat_writer: Optional[BitWriter] = None
    repeat_state: Optional[EncoderStateV4] = None
    reference = state.recent.get(cmd.opcode)
    delta = geom_translation(reference, cmd) if reference is not None else None
    if delta is not None:
        repeat_writer = BitWriter(); repeat_state = state.clone()
        _write_single_repeat_record(repeat_writer, repeat_state, cmd, delta)
    if repeat_writer is not None and len(repeat_writer.bits) < len(normal_writer.bits):
        assert repeat_state is not None
        return repeat_writer, repeat_state, True
    return normal_writer, normal_state, False


def _simulate_sequence(sequence: Sequence[VectorCommand], state: EncoderStateV4, palette: Sequence[str]) -> Tuple[int, EncoderStateV4, int]:
    current = state.clone(); bits = 0; repeats = 0
    for command in sequence:
        writer, current, used_repeat = _best_single_or_normal(command, current, palette)
        bits += len(writer.bits)
        repeats += int(used_repeat)
    return bits, current, repeats


def _plan_primitive_representations(commands: Sequence[VectorCommand], palette: Sequence[str]) -> Tuple[List[VectorCommand], int, int]:
    planned: List[VectorCommand] = []
    state = EncoderStateV4()
    primitive_selected = 0
    vectorized_selected = 0
    for source in commands:
        command = quantize_command(source)
        if command.opcode != OP_PRIMITIVE:
            planned.append(command)
            _bits, state, _repeats = _simulate_sequence([command], state, palette)
            continue
        expansion = primitive_to_vectors(command)
        if not expansion:
            planned.append(command)
            _bits, state, _repeats = _simulate_sequence([command], state, palette)
            primitive_selected += 1
            continue
        vectors = [quantize_command(item) for item in expansion]
        primitive_bits, primitive_state, _ = _simulate_sequence([command], state, palette)
        vector_bits, vector_state, _ = _simulate_sequence(vectors, state, palette)
        # A primitive is emitted only when it is strictly smaller. Equal-size
        # cases intentionally use generic vectors, matching the requested rule.
        if primitive_bits < vector_bits:
            planned.append(command)
            state = primitive_state
            primitive_selected += 1
        else:
            planned.extend(vectors)
            state = vector_state
            vectorized_selected += 1
    return planned, primitive_selected, vectorized_selected


def _command_invariant_signature(cmd: VectorCommand) -> str:
    anchor = _primitive_anchor(cmd) if cmd.opcode == OP_PRIMITIVE else (command_points(cmd)[0] if command_points(cmd) else (0, 0))
    shifted = quantize_command(translate_command(cmd, -int(round(anchor[0])), -int(round(anchor[1]))))
    return json.dumps({"op": shifted.opcode, "style": shifted.style.to_json(), "geom": shifted.geom}, sort_keys=True, separators=(",", ":"))


def _write_group_record(w: BitWriter, state: EncoderStateV4, source_distance: int, length: int,
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


def _best_group_repeat(commands: Sequence[VectorCommand], index: int, state: EncoderStateV4,
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

    @property
    def fits(self) -> bool:
        return self.frame_count <= MAX_MESSAGES


def encode_commands(commands: Sequence[VectorCommand]) -> Tuple[bytes, int, Dict[str, int], List[str]]:
    source = [quantize_command(command) for command in commands]
    palette = build_palette(source)
    planned, primitive_count, vectorized_count = _plan_primitive_representations(source, palette)
    if len(planned) > MAX_COMMANDS:
        raise MCIError(f"Optimized image expands to {len(planned)} commands; maximum is {MAX_COMMANDS}.")

    w = BitWriter()
    w.bits_n(PROTOCOL_VERSION, 4)
    w.ue(len(palette) - 1)
    for color in palette:
        w.bits_n(rgb565(color), 16)
        w.bits_n(alpha4(color), 4)
    w.ue(len(planned))

    state = EncoderStateV4()
    signatures: Dict[str, List[int]] = {}
    for position, command in enumerate(planned):
        signatures.setdefault(_command_invariant_signature(command), []).append(position)

    index = 0
    single_repeats = 0
    group_repeats = 0
    copied_commands = 0
    while index < len(planned):
        group = _best_group_repeat(planned, index, state, palette, signatures)
        if group is not None:
            source_start, length, dx, dy, _saved = group
            _write_group_record(w, state, index - source_start, length, (dx, dy), planned[index:index + length])
            group_repeats += 1
            copied_commands += length
            index += length
            continue
        record, state, used_repeat = _best_single_or_normal(planned[index], state, palette)
        w.bits.extend(record.bits)
        single_repeats += int(used_repeat)
        index += 1

    metrics = {
        "single_repeats": single_repeats,
        "group_repeats": group_repeats,
        "copied_commands": copied_commands,
        "primitive_count": primitive_count,
        "vectorized_primitive_count": vectorized_count,
        "transport_commands": len(planned),
        "source_commands": len(source),
    }
    return w.to_bytes(), len(w.bits), metrics, palette


def decode_commands(data: bytes) -> Tuple[List[VectorCommand], List[str]]:
    r = BitReader(data)
    version = r.bits_n(4)
    if version != PROTOCOL_VERSION:
        raise MCIError(f"Unsupported MCoreIMG protocol version {version}; expected {PROTOCOL_VERSION}.")
    palette = [from_rgb565_a4(r.bits_n(16), r.bits_n(4)) for _ in range(r.ue(MAX_PALETTE - 1) + 1)]
    output_count = r.ue(MAX_COMMANDS)
    point_states = {op: PointState() for op in OP_NAMES}
    style_states: Dict[int, PaintStyle] = {}
    recent: Dict[int, VectorCommand] = {}
    previous_opcode: Optional[int] = None
    result: List[VectorCommand] = []

    while len(result) < output_count:
        record_type = r.bits_n(2)
        if record_type == REC_NORMAL:
            same = bool(r.bit())
            if same:
                if previous_opcode is None: raise MCIError("Same opcode before initialization.")
                opcode = previous_opcode
            else:
                opcode = r.bits_n(3)
            if opcode not in OP_NAMES: raise MCIError("Invalid opcode.")
            same_style = bool(r.bit())
            if same_style:
                if opcode not in style_states: raise MCIError("Style reuse before initialization.")
                style = copy.deepcopy(style_states[opcode])
            else:
                style = read_style(r, palette); style_states[opcode] = copy.deepcopy(style)
            command = VectorCommand(opcode, style, read_geometry(r, opcode, point_states[opcode], palette))
            validate_command(command)
            generated = [command]
        elif record_type == REC_SINGLE_REPEAT:
            opcode = r.bits_n(3)
            if opcode not in recent: raise MCIError("Repeat references missing opcode history.")
            command = quantize_command(translate_command(recent[opcode], r.rice_signed(2, 719), r.rice_signed(2, 479)))
            generated = [command]
        elif record_type == REC_GROUP_REPEAT:
            length = r.ue(MAX_COMMANDS - 2) + 2
            adjacent = bool(r.bit())
            distance = length if adjacent else length + r.ue(MAX_COMMANDS - length)
            dx, dy = r.rice_signed(2, 719), r.rice_signed(2, 479)
            source_start = len(result) - distance
            if source_start < 0 or source_start + length > len(result):
                raise MCIError("Group-copy reference is outside decoded history.")
            generated = [quantize_command(translate_command(result[source_start + offset], dx, dy)) for offset in range(length)]
            if len(result) + len(generated) > output_count:
                raise MCIError("Group-copy expands beyond declared command count.")
        else:
            raise MCIError("Reserved record type encountered.")

        result.extend(generated)
        for command in generated:
            recent[command.opcode] = command.clone()
            previous_opcode = command.opcode
    return result, palette


def encode_image(commands: Sequence[VectorCommand]) -> EncodedImage:
    bit_bytes, bit_count, metrics, palette = encode_commands(commands)
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
    )
    return EncodedImage(raw, payload, frames, stats, image_id, palette)



# ---------------------------------------------------------------------------
# ACTIVE PROTOCOL-V5 OVERRIDE: local-space SVG groups
# ---------------------------------------------------------------------------
#
# The definitions in this section are the active transport implementation.
# In particular, the later CodecStats, encode_commands, decode_commands, and
# encode_image replace same-named hybrid-foundation versions above.
#
# Local-space grouping solves a subtle vector-format problem: if every SVG
# point is baked into canvas coordinates before compression, shrinking an SVG
# produces numerically smaller deltas and falsely appears to compress better.
# This layer instead normalizes geometry once and sends a fixed-width display
# box. Display scale therefore does not determine geometry cost.
# Imported SVGs are encoded once in a stable local coordinate space. Their
# displayed position and size are carried in a fixed-width transform box.
# Consequently, dragging or scaling an SVG does not make its path coordinates
# cheaper or more expensive, and repeated SVGs can reuse the same definition.
PROTOCOL_VERSION = 5
SOURCE_VERSION = 5
CONSTRUCTOR_BUILD = "2026.08.02-svg-v5.1-DOCUMENTED-LOCALSPACE-HYBRID-10MSG"

REC_TRANSFORM_GROUP = REC_RESERVED
LOCAL_GROUP_EXTENT = 255


@dataclass
class _TransformGroupPlan:
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
class _DecoderStateV5:
    """Mutable decode/encode history shared by normal and repeat records.

    Point and style state are opcode-local. ``recent`` stores the latest fully
    reconstructed command for each opcode. ``previous_opcode`` supports the
    one-bit same-opcode shortcut used by normal records.
    """

    point_states: Dict[int, PointState] = field(default_factory=lambda: {op: PointState() for op in OP_NAMES})
    style_states: Dict[int, PaintStyle] = field(default_factory=dict)
    recent: Dict[int, VectorCommand] = field(default_factory=dict)
    previous_opcode: Optional[int] = None


def _all_transport_points_v5(command: VectorCommand) -> List[Tuple[float, float]]:
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


def _geometry_bbox_v5(commands: Sequence[VectorCommand]) -> Tuple[float, float, float, float]:
    """Bounds for normalization, including path control points.

    This differs from visual bounds. Control points must be included because
    they are encoded and must remain inside the advertised local coordinate
    box even when a Bézier curve never reaches the control point itself.
    """
    points = [point for command in commands for point in _all_transport_points_v5(command)]
    if not points:
        return 0.0, 0.0, 0.0, 0.0
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def _group_transport_signature_v5(commands: Sequence[VectorCommand], local_width: int, local_height: int) -> str:
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


def _make_transform_group_v5(commands: Sequence[VectorCommand], start: int, end: int) -> Optional[_TransformGroupPlan]:
    """Normalize one eligible contiguous command run into local coordinates.

    Returns ``None`` for runs that are too short, contain compact primitives,
    or have degenerate width/height. The caller may then encode those commands
    using normal/repeat records. This function performs lossy integer
    quantization at the selected local extent; preview parity depends on using
    ``display_commands`` reconstructed from the same quantized definition.
    """
    group = [command.clone() for command in commands[start:end]]
    if len(group) < 2 or any(command.opcode == OP_PRIMITIVE for command in group):
        return None
    x1, y1, x2, y2 = _geometry_bbox_v5(group)
    raw_width = x2 - x1
    raw_height = y2 - y1
    if raw_width <= 1e-6 or raw_height <= 1e-6:
        return None
    maximum = max(raw_width, raw_height)
    local_width = max(1, min(LOCAL_GROUP_EXTENT, int(round(raw_width / maximum * LOCAL_GROUP_EXTENT))))
    local_height = max(1, min(LOCAL_GROUP_EXTENT, int(round(raw_height / maximum * LOCAL_GROUP_EXTENT))))
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
    local_points = [point for command in local_commands for point in _all_transport_points_v5(command)]
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

    display_commands = _apply_transform_group_v5(
        local_commands, x, y, width, height, local_width, local_height,
    )
    signature = _group_transport_signature_v5(local_commands, local_width, local_height)
    return _TransformGroupPlan(
        start, end, local_commands, display_commands,
        x, y, width, height, local_width, local_height, signature,
    )


def _apply_transform_group_v5(
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


def _write_transform_box_v5(w: BitWriter, plan: _TransformGroupPlan) -> None:
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


def _read_transform_box_v5(r: BitReader) -> Tuple[int, int, int, int, int, int]:
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


def _local_bits_v5(maximum: int) -> int:
    return max(1, int(maximum).bit_length())


def _write_local_point_v5(
    w: BitWriter, state: PointState, point: Tuple[int, int], local_width: int, local_height: int,
) -> None:
    """Write one local point using the cheaper absolute or Rice-delta form.

    Unlike canvas points, the absolute bit width derives from the group's local
    dimensions. Delta state remains opcode-local through the nested decoder
    state used for the definition.
    """
    x, y = int(point[0]), int(point[1])
    x_bits = _local_bits_v5(local_width)
    y_bits = _local_bits_v5(local_height)
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


def _read_local_point_v5(
    r: BitReader, state: PointState, local_width: int, local_height: int,
) -> Tuple[int, int]:
    """Inverse of _write_local_point_v5 with strict local-box validation."""
    if r.bit():
        if not state.initialized:
            raise MCIError("Local delta point before initialization.")
        x = state.x + r.rice_signed(3, 1024)
        y = state.y + r.rice_signed(3, 1024)
    else:
        x = r.bits_n(_local_bits_v5(local_width))
        y = r.bits_n(_local_bits_v5(local_height))
    if not (0 <= x <= local_width and 0 <= y <= local_height):
        raise MCIError(f"Point outside local SVG group: {x},{y}.")
    state.initialized = True
    state.x, state.y = x, y
    return x, y


def _write_local_geometry_v5(
    w: BitWriter, command: VectorCommand, state: PointState, local_width: int, local_height: int,
) -> None:
    """Write generic vector geometry inside a local SVG definition.

    Compact primitives are excluded before this function. Keeping local
    definitions generic makes their signatures deterministic and lets the same
    geometry be instantiated at several display sizes.
    """
    g = command.geom
    point = lambda value: _write_local_point_v5(w, state, tuple(value), local_width, local_height)
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


def _read_local_geometry_v5(
    r: BitReader, opcode: int, state: PointState, local_width: int, local_height: int,
) -> Dict[str, Any]:
    point = lambda: _read_local_point_v5(r, state, local_width, local_height)
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


def _write_local_normal_record_v5(
    w: BitWriter,
    state: EncoderStateV4,
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
    _write_local_geometry_v5(
        w, command, state.point_states[command.opcode], local_width, local_height,
    )
    _update_history(state, [command])


def _best_local_record_v5(
    command: VectorCommand,
    state: EncoderStateV4,
    palette: Sequence[str],
    local_width: int,
    local_height: int,
) -> Tuple[BitWriter, EncoderStateV4]:
    normal = BitWriter(); normal_state = state.clone()
    _write_local_normal_record_v5(
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


def _write_local_definition_v5(
    w: BitWriter,
    commands: Sequence[VectorCommand],
    palette: Sequence[str],
    local_width: int,
    local_height: int,
) -> None:
    state = EncoderStateV4()
    for command in commands:
        record, state = _best_local_record_v5(
            command, state, palette, local_width, local_height,
        )
        w.bits.extend(record.bits)

def _decode_regular_record_v5(r: BitReader, palette: Sequence[str], state: _DecoderStateV5, local_width: int, local_height: int) -> VectorCommand:
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
        command = VectorCommand(opcode, style, _read_local_geometry_v5(r, opcode, state.point_states[opcode], local_width, local_height))
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


def _plan_primitive_representations_v5(
    commands: Sequence[VectorCommand], palette: Sequence[str],
) -> Tuple[List[VectorCommand], int, int]:
    """Keep generic SVG geometry unquantized until local-space normalization."""
    planned: List[VectorCommand] = []
    comparison_state = EncoderStateV4()
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


# ACTIVE V5 COMMAND ENCODER
# -------------------------
# This is the final encode_commands definition. It first resolves primitive
# representations, then identifies local SVG groups, then chooses repeat/normal
# records for everything else. Metrics returned here feed both the status bar
# and regression tests; add new metrics in CodecStats and preview_frames too.

def encode_commands(commands: Sequence[VectorCommand]) -> Tuple[bytes, int, Dict[str, int], List[str]]:
    raw_source = [command.clone() for command in commands]
    palette = build_palette(raw_source)
    planned, primitive_count, vectorized_count = _plan_primitive_representations_v5(raw_source, palette)
    if len(planned) > MAX_COMMANDS:
        raise MCIError(f"Optimized image expands to {len(planned)} commands; maximum is {MAX_COMMANDS}.")

    display_commands = [quantize_command(command) for command in planned]
    transform_plans: Dict[int, _TransformGroupPlan] = {}
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
        plan = _make_transform_group_v5(planned, index, end)
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

    state = EncoderStateV4()
    signatures: Dict[str, List[int]] = {}
    for position, command in enumerate(display_commands):
        signatures.setdefault(_command_invariant_signature(command), []).append(position)

    definition_indices: Dict[str, int] = {}
    definitions: List[_TransformGroupPlan] = []
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
            _write_transform_box_v5(w, plan)
            if is_reference:
                assert definition_index is not None
                w.ue(definition_index)
                transformed_references += 1
                copied_commands += len(plan.local_commands)
            else:
                w.ue(len(plan.local_commands) - 2)
                _write_local_definition_v5(w, plan.local_commands, palette, plan.local_width, plan.local_height)
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


# ACTIVE V5 COMMAND DECODER
# -------------------------
# Decoder record expansion must update history exactly as if the expanded
# commands had arrived as normal records. Repeat/reference bugs often appear
# only in a later command because stale opcode/style/point history survives.

def decode_commands(data: bytes) -> Tuple[List[VectorCommand], List[str]]:
    r = BitReader(data)
    version = r.bits_n(4)
    if version != PROTOCOL_VERSION:
        raise MCIError(f"Unsupported MCoreIMG protocol version {version}; expected {PROTOCOL_VERSION}.")
    palette = [from_rgb565_a4(r.bits_n(16), r.bits_n(4)) for _ in range(r.ue(MAX_PALETTE - 1) + 1)]
    output_count = r.ue(MAX_COMMANDS)
    state = _DecoderStateV5()
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
            x, y, width, height, local_width, local_height = _read_transform_box_v5(r)
            if is_reference:
                definition_index = r.ue(MAX_COMMANDS)
                if definition_index >= len(definitions):
                    raise MCIError("SVG group references an undefined local definition.")
                local_commands, saved_local_width, saved_local_height = definitions[definition_index]
                if local_width != saved_local_width or local_height != saved_local_height:
                    raise MCIError("SVG group reference has mismatched local dimensions.")
            else:
                length = r.ue(MAX_COMMANDS - 2) + 2
                nested_state = _DecoderStateV5()
                local_commands = [
                    _decode_regular_record_v5(r, palette, nested_state, local_width, local_height)
                    for _ in range(length)
                ]
                definitions.append((copy.deepcopy(local_commands), local_width, local_height))
            generated = _apply_transform_group_v5(
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


def _encode_image_once_v5(commands: Sequence[VectorCommand], local_extent: int) -> EncodedImage:
    """Encode once using a particular local-coordinate precision.

    TODO(DEBT): this temporarily mutates LOCAL_GROUP_EXTENT. The application is
    currently single-threaded, but an explicit CodecConfig should replace this
    global before the codec is reused concurrently or as a library service.
    """
    global LOCAL_GROUP_EXTENT
    previous_extent = LOCAL_GROUP_EXTENT
    LOCAL_GROUP_EXTENT = int(local_extent)
    try:
        bit_bytes, bit_count, metrics, palette = encode_commands(commands)
    finally:
        LOCAL_GROUP_EXTENT = previous_extent
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
        return _encode_image_once_v5(commands, LOCAL_GROUP_EXTENT)

    precision_ladder = (255, 224, 192, 160, 144, 128, 112, 96, 80, 72, 64, 56, 48, 40, 32)
    smallest: Optional[EncodedImage] = None
    for extent in precision_ladder:
        candidate = _encode_image_once_v5(commands, extent)
        smallest = candidate
        if candidate.stats.fits:
            return candidate
    assert smallest is not None
    return smallest

# ---- Multi-SVG composition helpers ----------------------------------------


def next_editor_group(doc: VectorDocument) -> int:
    groups = [int(command.editor_group) for command in doc.commands if command.editor_group is not None]
    return max(groups, default=0) + 1


def assign_editor_group(commands: Sequence[VectorCommand], group_id: int) -> None:
    for command in commands:
        command.editor_group = int(group_id)


def preserve_alpha_with_rgb(old_color: Optional[str], new_rgb: str) -> str:
    rgb = normalize_hex(new_rgb)[:7]
    if old_color:
        normalized = normalize_hex(old_color)
        if len(normalized) == 9:
            return rgb + normalized[7:9]
    return rgb


def append_source_files(
    target: VectorDocument,
    paths: Sequence[str | Path],
    base_dx: int = 0,
    base_dy: int = 0,
    step_dx: int = 40,
    step_dy: int = 40,
) -> Tuple[int, int]:
    """Append one or more SVG/source documents without replacing current art.

    Existing document transforms are baked first so newly appended artwork and
    click-drawn primitives share the same canvas coordinate system.  Each file
    is imported using its own SVG fit-to-canvas transform, then translated by
    ``base + index * step``. Re-importing the same SVG therefore produces an
    exact translated command group that protocol-v5 local-definition reuse can
    reference instead of transmitting twice.
    """
    normalized_paths = [Path(item) for item in paths]
    if not normalized_paths:
        return 0, 0

    if target.commands and (
        abs(target.scale - 1.0) > 1e-9
        or abs(target.offset_x) > 1e-9
        or abs(target.offset_y) > 1e-9
    ):
        target.bake_transform()

    appended_commands = 0
    appended_files = 0
    for index, path in enumerate(normalized_paths):
        incoming = load_source(path)
        incoming_commands = incoming.transformed_commands()
        dx = int(base_dx) + index * int(step_dx)
        dy = int(base_dy) + index * int(step_dy)
        prefix = path.stem
        group_id = next_editor_group(target)
        for command in incoming_commands:
            copied = translate_command(command, dx, dy)
            copied.label = f"{prefix} #{index + 1} | {copied.label}"
            copied.editor_group = group_id
            target.commands.append(copied)
        for warning in incoming.warnings:
            target.warnings.append(f"{path.name}: {warning}")
        appended_commands += len(incoming_commands)
        appended_files += 1
    return appended_files, appended_commands


class MultiSVGPlacementDialog(tk.Toplevel):
    """One compact placement dialog for a multi-file SVG append operation."""

    def __init__(self, parent: tk.Misc, file_count: int):
        super().__init__(parent)
        self.title(f"Add {file_count} SVG file{'s' if file_count != 1 else ''}")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.result: Optional[Tuple[int, int, int, int]] = None

        self.base_x = tk.IntVar(value=0)
        self.base_y = tk.IntVar(value=0)
        self.step_x = tk.IntVar(value=40 if file_count > 1 else 0)
        self.step_y = tk.IntVar(value=40 if file_count > 1 else 0)

        frame = ttk.Frame(self, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame,
            text=("The first SVG keeps its fitted canvas position. Each later "
                  "SVG receives the additional step offset. Selecting the same "
                  "SVG repeatedly enables translated group-copy compression."),
            wraplength=390,
        ).grid(row=0, column=0, columnspan=4, sticky="ew", pady=(0, 10))

        fields = [
            ("Base X", self.base_x, "Base Y", self.base_y),
            ("Per-file step X", self.step_x, "Per-file step Y", self.step_y),
        ]
        for row, (left_label, left_var, right_label, right_var) in enumerate(fields, start=1):
            ttk.Label(frame, text=left_label).grid(row=row, column=0, sticky="w", padx=(0, 4), pady=3)
            ttk.Spinbox(frame, from_=-1440, to=1440, textvariable=left_var, width=9).grid(row=row, column=1, sticky="w", pady=3)
            ttk.Label(frame, text=right_label).grid(row=row, column=2, sticky="w", padx=(12, 4), pady=3)
            ttk.Spinbox(frame, from_=-960, to=960, textvariable=right_var, width=9).grid(row=row, column=3, sticky="w", pady=3)

        buttons = ttk.Frame(frame)
        buttons.grid(row=3, column=0, columnspan=4, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Cancel", command=self._cancel).pack(side="right", padx=(6, 0))
        ttk.Button(buttons, text="Add SVG(s)", command=self._accept).pack(side="right")
        self.bind("<Return>", lambda _event: self._accept())
        self.bind("<Escape>", lambda _event: self._cancel())
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.update_idletasks()
        x = parent.winfo_rootx() + max(0, (parent.winfo_width() - self.winfo_reqwidth()) // 2)
        y = parent.winfo_rooty() + max(0, (parent.winfo_height() - self.winfo_reqheight()) // 2)
        self.geometry(f"+{x}+{y}")
        self.wait_visibility()
        self.focus_set()
        self.wait_window(self)

    def _accept(self) -> None:
        try:
            self.result = (
                int(self.base_x.get()), int(self.base_y.get()),
                int(self.step_x.get()), int(self.step_y.get()),
            )
        except (ValueError, tk.TclError):
            messagebox.showerror("Invalid placement", "Placement values must be whole numbers.", parent=self)
            return
        self.grab_release()
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()


# ---- HYBRID GUI LAYER ------------------------------------------------------
# This first GUI subclass adds multi-SVG import and manual primitives. It is
# not the final class instantiated by main(); the direct-editing/undo subclass
# later in the file extends it. Keep this inheritance chain in mind when
# changing _build_ui or methods also overridden later.


class ConstructorApp(_BaseConstructorApp):
    def __init__(self):
        self._manual_window: Optional[tk.Toplevel] = None
        self._manual_first_point: Optional[Tuple[int, int]] = None
        super().__init__()
        self.title(f"MCoreIMG Hybrid Constructor — {CONSTRUCTOR_BUILD} — {Path(__file__).name}")

    def _build_ui(self):
        super()._build_ui()

        # Creation controls have their own full-width row.  They previously
        # lived at the far end of an already crowded toolbar and could be
        # clipped entirely on narrower desktops, making the features appear
        # absent even though codec support existed.
        toolbar = self.winfo_children()[0]
        creation_bar = ttk.LabelFrame(self, text="Create / Combine Artwork", padding=(6, 4))
        creation_bar.pack(fill="x", after=toolbar)
        ttk.Button(
            creation_bar, text="Draw Primitive / Old Drawing Mode",
            command=self.open_drawing_mode,
        ).pack(side="left", padx=3)
        ttk.Button(
            creation_bar, text="Add SVG(s) Without Replacing",
            command=self.append_svg,
        ).pack(side="left", padx=3)
        ttk.Button(
            creation_bar, text="Duplicate All Artwork",
            command=self.duplicate_all,
        ).pack(side="left", padx=3)
        ttk.Label(
            creation_bar,
            text="Primitive-vs-vector and repeated-SVG comparison happen automatically at encode time.",
        ).pack(side="left", padx=12)

        # A menu provides a second permanent access path independent of window
        # width, desktop theme, or toolbar layout.
        menu = tk.Menu(self)
        create_menu = tk.Menu(menu, tearoff=False)
        create_menu.add_command(label="Draw Primitive / Old Drawing Mode", command=self.open_drawing_mode)
        create_menu.add_command(label="Add SVG(s) Without Replacing…", command=self.append_svg)
        create_menu.add_command(label="Duplicate All Artwork…", command=self.duplicate_all)
        menu.add_cascade(label="Create", menu=create_menu)
        self.configure(menu=menu)
        self.creation_bar = creation_bar

    def _refresh(self):
        self.var_scale.set(self.doc.scale * 100); self.var_offset_x.set(self.doc.offset_x); self.var_offset_y.set(self.doc.offset_y)
        self.layer_list.delete(0, "end")
        for index, command in enumerate(self.doc.commands):
            mark = "✓" if command.visible else "×"
            name = PRIMITIVE_NAMES.get(_primitive_kind(command), "Primitive") if command.opcode == OP_PRIMITIVE else OP_NAMES[command.opcode]
            self.layer_list.insert("end", f"{index:03d} {mark} {name:18s} {command.label}")
        self.warning_text.configure(state="normal"); self.warning_text.delete("1.0", "end")
        self.warning_text.insert("end", "\n".join(self.doc.warnings) if self.doc.warnings else "No import warnings.")
        self.warning_text.configure(state="disabled")
        self._refresh_preview_and_stats()

    def _refresh_preview_and_stats(self):
        source_commands = self.doc.transformed_commands()
        if not source_commands:
            render_to_tk(self.canvas, [])
            self.var_stats.set("No image loaded")
            return
        bbox = commands_bbox(source_commands)
        outside = bbox[0] < 0 or bbox[1] < 0 or bbox[2] > CANVAS_W or bbox[3] > CANVAS_H
        try:
            encoded = encode_image(source_commands)
            fallback = [quantize_command(command) for command in source_commands]
            preview = decode_frames(encoded.frames) if encoded.stats.fits else fallback
            render_to_tk(self.canvas, preview)
            fit = "fits" if encoded.stats.fits else "TOO LARGE"
            prefix = "OVER CANVAS | " if outside else ""
            optimizer = f" | P:{encoded.stats.primitive_count} V:{encoded.stats.vectorized_primitive_count} G:{encoded.stats.group_repeat_count} L:{encoded.stats.local_group_extent if encoded.stats.transformed_group_count else "-"}"
            self.var_stats.set(f"{prefix}{len(source_commands)} src / {encoded.stats.command_count} tx | {encoded.stats.base91_chars} chars | {encoded.stats.frame_count}/{MAX_MESSAGES} frames {fit}{optimizer}")
        except Exception as exc:
            render_to_tk(self.canvas, source_commands)
            self.var_stats.set(f"Cannot encode: {exc}")

    def preview_frames(self):
        try: encoded = encode_image(self._export_commands())
        except Exception as exc:
            messagebox.showerror("Preview failed", str(exc)); return
        window = tk.Toplevel(self); window.title(f"MCoreIMG Hybrid Frames — {encoded.image_id}"); window.geometry("1000x520")
        text = tk.Text(window, wrap="none", font=("TkFixedFont", 10)); text.pack(fill="both", expand=True, padx=8, pady=8)
        text.insert("end", (
            f"Build: {CONSTRUCTOR_BUILD}\nProtocol: {PROTOCOL_VERSION}\nTransport limit: {MAX_MESSAGES} messages / {MAX_PAYLOAD_CHARS} payload characters\n"
            f"Palette: {', '.join(encoded.palette)}\nSource commands: {encoded.stats.source_command_count}\nTransport commands: {encoded.stats.command_count}\n"
            f"Compact primitives selected: {encoded.stats.primitive_count}\nPrimitives vectorized because equal/smaller: {encoded.stats.vectorized_primitive_count}\n"
            f"Single translated repeats: {encoded.stats.repeat_count - encoded.stats.group_repeat_count}\nRepeated/copied groups: {encoded.stats.group_repeat_count}\nLocal-space SVG groups: {encoded.stats.transformed_group_count}\nLocal coordinate extent: {encoded.stats.local_group_extent}\n"
            f"Payload: {encoded.stats.base91_chars} chars\nFrames: {encoded.stats.frame_count}/{MAX_MESSAGES}\n\n"
        ))
        for index, frame in enumerate(encoded.frames):
            text.insert("end", f"Part {index + 1}/{len(encoded.frames)} — {len(frame)} chars\n{frame}\n\n")
        text.configure(state="disabled")
        ttk.Button(window, text="Copy All Frames", command=lambda: (self.clipboard_clear(), self.clipboard_append("\n".join(encoded.frames)))).pack(pady=(0, 8))

    def append_svg(self):
        paths = filedialog.askopenfilenames(
            title="Add one or more SVG/source files",
            filetypes=[
                ("SVG / MCoreIMG source", "*.svg *.svgz *.mci.json *.json"),
                ("SVG files", "*.svg *.svgz"),
                ("All files", "*.*"),
            ],
        )
        if not paths:
            return
        placement = MultiSVGPlacementDialog(self, len(paths)).result
        if placement is None:
            return
        base_dx, base_dy, step_dx, step_dy = placement
        try:
            file_count, command_count = append_source_files(
                self.doc, paths, base_dx, base_dy, step_dx, step_dy,
            )
            self._refresh()
            self.var_status.set(
                f"Added {file_count} SVG/source file(s), {command_count} commands. "
                "Repeated translated groups are compared automatically."
            )
        except Exception as exc:
            messagebox.showerror("Add SVG(s) failed", str(exc))

    def duplicate_all(self):
        from tkinter import simpledialog
        if not self.doc.commands:
            messagebox.showinfo("Duplicate artwork", "There is no artwork to duplicate.", parent=self)
            return
        dx = simpledialog.askinteger("Duplicate all", "Horizontal translation in pixels:", parent=self, initialvalue=40, minvalue=-719, maxvalue=719)
        if dx is None:
            return
        dy = simpledialog.askinteger("Duplicate all", "Vertical translation in pixels:", parent=self, initialvalue=40, minvalue=-479, maxvalue=479)
        if dy is None:
            return
        if abs(self.doc.scale - 1.0) > 1e-9 or abs(self.doc.offset_x) > 1e-9 or abs(self.doc.offset_y) > 1e-9:
            self.doc.bake_transform()
        original = [command.clone() for command in self.doc.commands]
        for command in original:
            copied = translate_command(command, dx, dy)
            copied.label = f"Copy | {copied.label}"
            self.doc.commands.append(copied)
        self._refresh()
        self.var_status.set(
            f"Duplicated {len(original)} layers by ({dx},{dy}); "
            "normal vectors and one translated group-copy record are compared automatically."
        )

    def open_drawing_mode(self):
        if self._manual_window is not None and self._manual_window.winfo_exists():
            self._manual_window.lift(); return
        window = tk.Toplevel(self); self._manual_window = window
        window.title("MCoreIMG Old Drawing Mode — click canvas or enter coordinates")
        window.geometry("560x720")
        window.protocol("WM_DELETE_WINDOW", self._close_drawing_mode)
        frame = ttk.Frame(window, padding=10); frame.pack(fill="both", expand=True)

        self.manual_shape = tk.StringVar(value="Line")
        self.manual_color = tk.StringVar(value="#000000")
        self.manual_crater = tk.StringVar(value="#808080")
        self.manual_fill = tk.BooleanVar(value=False)
        self.manual_width = tk.IntVar(value=2)
        self.manual_x1 = tk.IntVar(value=100); self.manual_y1 = tk.IntVar(value=100)
        self.manual_x2 = tk.IntVar(value=200); self.manual_y2 = tk.IntVar(value=200)
        self.manual_radius_x = tk.IntVar(value=35); self.manual_radius_y = tk.IntVar(value=35)
        self.manual_scale = tk.IntVar(value=1); self.manual_orientation = tk.IntVar(value=0)
        self.manual_start = tk.IntVar(value=0); self.manual_degrees = tk.IntVar(value=180)
        self.manual_percent = tk.IntVar(value=50); self.manual_text = tk.StringVar(value="KE9ETA")
        self.manual_status = tk.StringVar(value="Choose a shape, then click the main canvas. Two-point shapes use two clicks.")

        row = 0
        ttk.Label(frame, text="Shape").grid(row=row, column=0, sticky="w")
        shape_values = ["Text", "Line", "Rectangle", "Ellipse"] + [PRIMITIVE_NAMES[k] for k in range(1, 12)]
        ttk.Combobox(frame, textvariable=self.manual_shape, values=shape_values, state="readonly", width=28).grid(row=row, column=1, columnspan=3, sticky="ew"); row += 1
        for label, variable in [("Color (#RRGGBB or #RRGGBBAA)", self.manual_color), ("Moon crater color", self.manual_crater)]:
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w"); ttk.Entry(frame, textvariable=variable).grid(row=row, column=1, columnspan=3, sticky="ew"); row += 1
        ttk.Checkbutton(frame, text="Filled rectangle/ellipse", variable=self.manual_fill).grid(row=row, column=0, columnspan=2, sticky="w")
        ttk.Label(frame, text="Stroke width").grid(row=row, column=2, sticky="e"); ttk.Spinbox(frame, from_=1, to=64, textvariable=self.manual_width, width=7).grid(row=row, column=3); row += 1

        fields = [
            ("X1", self.manual_x1, "Y1", self.manual_y1), ("X2", self.manual_x2, "Y2", self.manual_y2),
            ("Radius X / radius", self.manual_radius_x, "Radius Y", self.manual_radius_y),
            ("Scale", self.manual_scale, "Orientation 0-3", self.manual_orientation),
            ("Start angle", self.manual_start, "Arc degrees", self.manual_degrees),
            ("Divider %", self.manual_percent, "", None),
        ]
        for left_label, left_var, right_label, right_var in fields:
            ttk.Label(frame, text=left_label).grid(row=row, column=0, sticky="w")
            ttk.Spinbox(frame, from_=-1440 if left_label.startswith("X") else 0, to=1440 if left_label.startswith("X") else 1000, textvariable=left_var, width=9).grid(row=row, column=1, sticky="w")
            if right_var is not None:
                ttk.Label(frame, text=right_label).grid(row=row, column=2, sticky="e")
                ttk.Spinbox(frame, from_=-960 if right_label.startswith("Y") else 0, to=960 if right_label.startswith("Y") else 1000, textvariable=right_var, width=9).grid(row=row, column=3, sticky="w")
            row += 1
        ttk.Label(frame, text="Text").grid(row=row, column=0, sticky="w"); ttk.Entry(frame, textvariable=self.manual_text).grid(row=row, column=1, columnspan=3, sticky="ew"); row += 1
        buttons = ttk.Frame(frame); buttons.grid(row=row, column=0, columnspan=4, sticky="ew", pady=8)
        ttk.Button(buttons, text="Add From Fields", command=self._manual_add_from_fields).pack(side="left", padx=3)
        ttk.Button(buttons, text="Clear First Click", command=self._manual_clear_first).pack(side="left", padx=3)
        ttk.Button(buttons, text="Close", command=self._close_drawing_mode).pack(side="right", padx=3); row += 1
        ttk.Label(frame, text=("Compression is automatic: each old primitive is measured against its generic vector expansion. "
                               "The primitive is transmitted only when it is strictly smaller."), wraplength=470).grid(row=row, column=0, columnspan=4, sticky="ew", pady=(4, 8)); row += 1
        ttk.Label(frame, textvariable=self.manual_status, wraplength=470).grid(row=row, column=0, columnspan=4, sticky="ew")
        for column in range(4): frame.columnconfigure(column, weight=1 if column in {1, 3} else 0)
        self.canvas.bind("<Button-1>", self._manual_canvas_click)

    def _close_drawing_mode(self):
        self._manual_first_point = None
        self.canvas.unbind("<Button-1>")
        if self._manual_window is not None and self._manual_window.winfo_exists(): self._manual_window.destroy()
        self._manual_window = None

    def _manual_clear_first(self):
        self._manual_first_point = None
        if hasattr(self, "manual_status"): self.manual_status.set("First click cleared.")

    def _manual_canvas_click(self, event):
        if self._manual_window is None or not self._manual_window.winfo_exists(): return
        x = max(0, min(CANVAS_W - 1, int(event.x))); y = max(0, min(CANVAS_H - 1, int(event.y)))
        shape = self.manual_shape.get()
        if shape in {"Line", "Rectangle", "DoubleBox"}:
            if self._manual_first_point is None:
                self._manual_first_point = (x, y); self.manual_x1.set(x); self.manual_y1.set(y)
                self.manual_status.set("First point stored. Click the second point."); return
            x1, y1 = self._manual_first_point; self._manual_first_point = None
            self.manual_x1.set(x1); self.manual_y1.set(y1); self.manual_x2.set(x); self.manual_y2.set(y)
        else:
            self.manual_x1.set(x); self.manual_y1.set(y)
        self._manual_add_from_fields()

    def _manual_make_command(self) -> VectorCommand:
        shape = self.manual_shape.get()
        color = normalize_hex(self.manual_color.get())
        crater = normalize_hex(self.manual_crater.get())
        width = max(1, min(64, int(self.manual_width.get())))
        x1, y1 = int(self.manual_x1.get()), int(self.manual_y1.get())
        x2, y2 = int(self.manual_x2.get()), int(self.manual_y2.get())
        rx, ry = max(1, int(self.manual_radius_x.get())), max(1, int(self.manual_radius_y.get()))
        scale = max(1, min(64, int(self.manual_scale.get())))
        orientation = int(self.manual_orientation.get()) % 4
        if shape == "Line":
            return VectorCommand(OP_LINE, PaintStyle(None, color, width), {"p1": (x1, y1), "p2": (x2, y2)}, "Manual Line")
        if shape == "Rectangle":
            left, right = min(x1, x2), max(x1, x2); top, bottom = min(y1, y2), max(y1, y2)
            style = PaintStyle(color, None, 0) if self.manual_fill.get() else PaintStyle(None, color, width)
            return VectorCommand(OP_RECT, style, {"x": left, "y": top, "w": max(1, right - left), "h": max(1, bottom - top)}, "Manual Rectangle")
        if shape == "Ellipse":
            style = PaintStyle(color, None, 0) if self.manual_fill.get() else PaintStyle(None, color, width)
            return VectorCommand(OP_ELLIPSE, style, {"cx": x1, "cy": y1, "rx": rx, "ry": ry}, "Manual Ellipse")
        kind = PRIMITIVE_BY_NAME[shape]
        if kind in {PRIM_TRIANGLE_FILL, PRIM_ARROW, PRIM_MOON, PRIM_TEXT}:
            style = PaintStyle(color, None, 0)
        else:
            style = PaintStyle(None, color, width)
        geom: Dict[str, Any] = {"kind": kind}
        if kind == PRIM_DOUBLE_BOX:
            geom.update(x1=x1, y1=y1, x2=x2, y2=y2, percent=max(0, min(100, int(self.manual_percent.get()))))
        else:
            geom.update(x=x1, y=y1)
        if kind == PRIM_TEXT: geom["text"] = _clean_primitive_text(self.manual_text.get())
        if kind in {PRIM_TRIANGLE_OUTLINE, PRIM_TRIANGLE_FILL, PRIM_ARROW, PRIM_YAGI, PRIM_DISH, PRIM_RADIO}:
            geom.update(orientation=orientation, scale=scale)
        if kind == PRIM_STAR: geom.update(radius=rx, scale=scale)
        if kind in {PRIM_ARC, PRIM_RADIO_WAVES}:
            geom.update(radius=rx, scale=scale, start_angle=max(0, min(360, int(self.manual_start.get()))), arc_degrees=max(0, min(360, int(self.manual_degrees.get()))))
        if kind == PRIM_MOON: geom.update(scale=scale, crater_color=crater)
        return VectorCommand(OP_PRIMITIVE, style, geom, f"Manual {shape}")

    def _manual_add_from_fields(self):
        try:
            command = quantize_command(self._manual_make_command())
            command.editor_group = next_editor_group(self.doc)
            self.doc.commands.append(command)
            self._refresh()
            encoded = encode_image([command])
            chosen = "compact primitive" if encoded.stats.primitive_count else "generic vectors"
            self.manual_status.set(f"Added {command.label}. Auto-comparison chose {chosen} for this isolated command.")
            self.var_status.set(f"Added {command.label}; final whole-image optimization is recalculated during preview/export.")
        except Exception as exc:
            messagebox.showerror("Drawing command failed", str(exc), parent=self._manual_window)


# ---- FINAL ACTIVE GUI LAYER: direct manipulation and undo -----------------
# The ConstructorApp defined below is the one main() instantiates. It extends
# the hybrid GUI rather than replacing its implementation wholesale. Method
# lookup therefore flows: final editor -> hybrid GUI -> original base GUI.
#
# TODO(DEBT): collapse these layers into one class after behavior is covered by
# UI tests. Until then, always check super() before assuming a method is local.


_HybridConstructorApp = ConstructorApp


class ConstructorApp(_HybridConstructorApp):
    """Protocol-v5 hybrid Constructor with direct canvas manipulation.

    Editor-only grouping makes an imported SVG move and scale as one object,
    while the transport encoder still receives its original contiguous command
    sequence. Therefore this UI metadata does not cost transmission bytes or
    interfere with primitive-vs-vector and translated-group comparisons.
    """

    HANDLE_SIZE = 8

    def __init__(self):
        self._editor_drag_mode: Optional[str] = None
        self._editor_last_point: Optional[Tuple[float, float]] = None
        self._editor_resize_start: Optional[Tuple[float, float]] = None
        self._editor_resize_bbox: Optional[Tuple[float, float, float, float]] = None
        self._editor_resize_originals: List[Tuple[int, VectorCommand]] = []
        self._editor_selected_index: Optional[int] = None
        self._editor_fill_label: Optional[tk.StringVar] = None
        self._editor_stroke_label: Optional[tk.StringVar] = None
        self._undo_stack: List[Tuple[str, VectorDocument, Optional[int], Optional[int]]] = []
        self._undo_text: Optional[tk.StringVar] = None
        self._undo_button: Optional[ttk.Button] = None
        super().__init__()
        self.title(f"MCoreIMG Hybrid Constructor — {CONSTRUCTOR_BUILD} — {Path(__file__).name}")
        self._bind_editor_canvas()
        self.bind_all("<Control-z>", self._undo_keypress)
        self.bind_all("<Control-Z>", self._undo_keypress)
        self._update_undo_controls()

    def _build_ui(self):
        super()._build_ui()
        editing_bar = ttk.LabelFrame(self, text="Direct Editing", padding=(6, 4))
        editing_bar.pack(fill="x", after=self.creation_bar)
        ttk.Label(
            editing_bar,
            text="Click artwork to select • drag to move • drag blue corner to scale",
        ).pack(side="left", padx=(2, 10))
        ttk.Button(editing_bar, text="Fill Color…", command=self.choose_selected_fill).pack(side="left", padx=3)
        ttk.Button(editing_bar, text="Stroke Color…", command=self.choose_selected_stroke).pack(side="left", padx=3)
        ttk.Button(editing_bar, text="Moon Crater Color…", command=self.choose_selected_crater).pack(side="left", padx=3)
        ttk.Button(editing_bar, text="No Fill", command=lambda: self._set_selected_style_color("fill", None)).pack(side="left", padx=3)
        ttk.Button(editing_bar, text="No Stroke", command=lambda: self._set_selected_style_color("stroke", None)).pack(side="left", padx=3)
        ttk.Button(editing_bar, text="Duplicate Selected", command=self.duplicate_selected).pack(side="left", padx=3)
        self._undo_text = tk.StringVar(value="Undo")
        self._undo_button = ttk.Button(editing_bar, textvariable=self._undo_text, command=self.undo_last_action)
        self._undo_button.pack(side="left", padx=(10, 3))
        self._editor_fill_label = tk.StringVar(value="Fill: —")
        self._editor_stroke_label = tk.StringVar(value="Stroke: —")
        ttk.Label(editing_bar, textvariable=self._editor_fill_label).pack(side="left", padx=(12, 4))
        ttk.Label(editing_bar, textvariable=self._editor_stroke_label).pack(side="left", padx=4)
        self.editing_bar = editing_bar

    def _document_changed(self, before: VectorDocument) -> bool:
        return self.doc.to_json() != before.to_json()

    # Undo stores complete document snapshots rather than inverse operations.
    # This costs memory but is robust while imports, groups, primitive creation,
    # and transforms are still evolving. A command-pattern undo system is a
    # future optimization, not a prerequisite for correctness.
    def _record_undo_snapshot(
        self,
        label: str,
        before: VectorDocument,
        editor_selection: Optional[int],
        layer_selection: Optional[int],
    ) -> None:
        if not self._document_changed(before):
            return
        self._undo_stack.append((label, before, editor_selection, layer_selection))
        # A modest cap prevents repeated large SVG imports from consuming
        # unbounded memory while still allowing several corrections.
        if len(self._undo_stack) > 20:
            del self._undo_stack[0]
        self._update_undo_controls()

    def _update_undo_controls(self) -> None:
        if self._undo_text is not None:
            self._undo_text.set(
                f"Undo {self._undo_stack[-1][0]}" if self._undo_stack else "Undo"
            )
        if self._undo_button is not None:
            self._undo_button.configure(state="normal" if self._undo_stack else "disabled")

    def undo_last_action(self) -> None:
        if not self._undo_stack:
            self.bell()
            return
        label, document, editor_selection, layer_selection = self._undo_stack.pop()
        self.doc = copy.deepcopy(document)
        self._editor_selected_index = editor_selection
        self.selected_index = layer_selection
        self._manual_first_point = None
        self._refresh()
        self._update_undo_controls()
        self.var_status.set(f"Undid {label}.")

    def _undo_keypress(self, _event=None):
        self.undo_last_action()
        return "break"

    def append_svg(self):
        before = copy.deepcopy(self.doc)
        editor_selection = self._editor_selected_index
        layer_selection = self.selected_index
        super().append_svg()
        self._record_undo_snapshot(
            "SVG import", before, editor_selection, layer_selection,
        )

    def _manual_add_from_fields(self):
        before = copy.deepcopy(self.doc)
        editor_selection = self._editor_selected_index
        layer_selection = self.selected_index
        super()._manual_add_from_fields()
        label = "draw action"
        if len(self.doc.commands) > len(before.commands):
            label = self.doc.commands[-1].label or label
        self._record_undo_snapshot(
            label, before, editor_selection, layer_selection,
        )

    def _bind_editor_canvas(self) -> None:
        self.canvas.bind("<Button-1>", self._editor_press)
        self.canvas.bind("<B1-Motion>", self._editor_drag)
        self.canvas.bind("<ButtonRelease-1>", self._editor_release)

    def open_drawing_mode(self):
        # Manual drawing temporarily owns the primary canvas click. Movement
        # bindings are disabled so a drawing gesture cannot also move artwork.
        self.canvas.unbind("<B1-Motion>")
        self.canvas.unbind("<ButtonRelease-1>")
        super().open_drawing_mode()
        if self._manual_window is not None and self._manual_window.winfo_exists():
            self._manual_window.geometry("560x780")
            chooser_bar = ttk.Frame(self._manual_window, padding=(10, 0, 10, 8))
            chooser_bar.pack(fill="x")
            ttk.Button(chooser_bar, text="Choose Main Color…", command=self._choose_manual_main_color).pack(side="left", padx=3)
            ttk.Button(chooser_bar, text="Choose Moon Crater Color…", command=self._choose_manual_crater_color).pack(side="left", padx=3)

    def _close_drawing_mode(self):
        super()._close_drawing_mode()
        self._bind_editor_canvas()

    def _choose_manual_main_color(self):
        current = normalize_hex(self.manual_color.get())
        result = colorchooser.askcolor(color=current[:7], parent=self._manual_window, title="Choose drawing color")
        if result and result[1]:
            self.manual_color.set(preserve_alpha_with_rgb(current, result[1]))

    def _choose_manual_crater_color(self):
        current = normalize_hex(self.manual_crater.get())
        result = colorchooser.askcolor(color=current[:7], parent=self._manual_window, title="Choose moon crater color")
        if result and result[1]:
            self.manual_crater.set(preserve_alpha_with_rgb(current, result[1]))

    def open_file(self):
        path = filedialog.askopenfilename(
            filetypes=[("SVG / MCoreIMG source", "*.svg *.svgz *.mci.json *.json"), ("All files", "*.*")]
        )
        if not path:
            return
        try:
            self.doc = load_source(path)
            # A newly opened SVG is one directly manipulable object. Editable
            # source files retain their previously saved groups.
            if Path(path).suffix.lower() in {".svg", ".svgz"} and self.doc.commands:
                assign_editor_group(self.doc.commands, next_editor_group(self.doc))
        except Exception as exc:
            messagebox.showerror("Open failed", str(exc))
            return
        self.selected_index = None
        self._editor_selected_index = None
        self._refresh()
        self.var_status.set(f"Loaded {len(self.doc.commands)} commands from {path}")

    def _selected_group_indices(self) -> List[int]:
        index = self._editor_selected_index
        if index is None or not (0 <= index < len(self.doc.commands)):
            return []
        command = self.doc.commands[index]
        if command.editor_group is None:
            return [index]
        return [
            i for i, item in enumerate(self.doc.commands)
            if item.editor_group == command.editor_group
        ]

    def _selected_layer_indices(self) -> List[int]:
        index = self._editor_selected_index
        return [index] if index is not None and 0 <= index < len(self.doc.commands) else []

    def _bake_for_direct_edit(self) -> None:
        if not self.doc.commands:
            return
        if abs(self.doc.scale - 1.0) > 1e-9 or abs(self.doc.offset_x) > 1e-9 or abs(self.doc.offset_y) > 1e-9:
            self.doc.bake_transform()
            self.var_scale.set(100.0)
            self.var_offset_x.set(0.0)
            self.var_offset_y.set(0.0)
            self.var_status.set("Baked document transform for direct canvas editing.")

    def _document_editor_matrix(self) -> Matrix:
        if not self.doc.commands:
            return mat_translate(0, 0)
        bbox = commands_bbox(self.doc.commands)
        cx = (bbox[0] + bbox[2]) / 2.0
        cy = (bbox[1] + bbox[3]) / 2.0
        return mat_mul(
            mat_translate(self.doc.offset_x, self.doc.offset_y),
            mat_mul(
                mat_translate(cx, cy),
                mat_mul(mat_scale(self.doc.scale, self.doc.scale), mat_translate(-cx, -cy)),
            ),
        )

    def _selection_bbox(self) -> Optional[Tuple[float, float, float, float]]:
        indices = self._selected_group_indices()
        matrix = self._document_editor_matrix()
        commands = [
            transform_command(self.doc.commands[i], matrix)
            for i in indices if self.doc.commands[i].visible
        ]
        return commands_bbox(commands) if commands else None

    def _hit_command(self, x: float, y: float) -> Optional[int]:
        for index in range(len(self.doc.commands) - 1, -1, -1):
            command = self.doc.commands[index]
            if not command.visible:
                continue
            x1, y1, x2, y2 = commands_bbox([command])
            if x1 - 5 <= x <= x2 + 5 and y1 - 5 <= y <= y2 + 5:
                return index
        return None

    def _draw_editor_selection(self) -> None:
        bbox = self._selection_bbox()
        if bbox is None:
            return
        x1, y1, x2, y2 = bbox
        self.canvas.create_rectangle(x1, y1, x2, y2, outline="#008CFF", width=2, dash=(5, 3), tags="editor_overlay")
        h = self.HANDLE_SIZE
        self.canvas.create_rectangle(
            x2 - h, y2 - h, x2 + h, y2 + h,
            fill="#008CFF", outline="#FFFFFF", width=1,
            tags=("editor_overlay", "editor_resize_handle"),
        )

    def _refresh(self):
        super()._refresh()
        self._sync_editor_color_labels()
        self._restore_layer_selection()

    def _refresh_preview_and_stats(self):
        super()._refresh_preview_and_stats()
        self._draw_editor_selection()

    def _restore_layer_selection(self):
        if self._editor_selected_index is None:
            return
        if 0 <= self._editor_selected_index < self.layer_list.size():
            self.layer_list.selection_clear(0, "end")
            self.layer_list.selection_set(self._editor_selected_index)
            self.layer_list.see(self._editor_selected_index)

    def _select_layer(self, _event=None):
        selection = self.layer_list.curselection()
        self.selected_index = selection[0] if selection else None
        self._editor_selected_index = self.selected_index
        self._sync_editor_color_labels()
        self._refresh_preview_and_stats()

    def _sync_editor_color_labels(self) -> None:
        if self._editor_fill_label is None or self._editor_stroke_label is None:
            return
        indices = self._selected_layer_indices()
        if not indices:
            self._editor_fill_label.set("Fill: —")
            self._editor_stroke_label.set("Stroke: —")
            return
        style = self.doc.commands[indices[0]].style.normalized()
        self._editor_fill_label.set(f"Fill: {style.fill or 'none'}")
        self._editor_stroke_label.set(f"Stroke: {style.stroke or 'none'}")

    def choose_selected_fill(self):
        indices = self._selected_layer_indices()
        if not indices:
            messagebox.showinfo("No selection", "Select a layer or click artwork first.", parent=self)
            return
        old = self.doc.commands[indices[0]].style.fill or "#000000"
        result = colorchooser.askcolor(color=normalize_hex(old)[:7], parent=self, title="Choose fill color")
        if result and result[1]:
            self._set_selected_style_color("fill", preserve_alpha_with_rgb(old, result[1]))

    def choose_selected_stroke(self):
        indices = self._selected_layer_indices()
        if not indices:
            messagebox.showinfo("No selection", "Select a layer or click artwork first.", parent=self)
            return
        old = self.doc.commands[indices[0]].style.stroke or "#000000"
        result = colorchooser.askcolor(color=normalize_hex(old)[:7], parent=self, title="Choose stroke color")
        if result and result[1]:
            self._set_selected_style_color("stroke", preserve_alpha_with_rgb(old, result[1]))

    def choose_selected_crater(self):
        indices = self._selected_layer_indices()
        if not indices:
            messagebox.showinfo("No selection", "Select a moon primitive first.", parent=self)
            return
        command = self.doc.commands[indices[0]]
        if command.opcode != OP_PRIMITIVE or _primitive_kind(command) != PRIM_MOON:
            messagebox.showinfo("Not a moon", "Moon crater color applies only to the Moon primitive.", parent=self)
            return
        old = str(command.geom.get("crater_color", "#808080"))
        result = colorchooser.askcolor(color=normalize_hex(old)[:7], parent=self, title="Choose moon crater color")
        if result and result[1]:
            command.geom["crater_color"] = preserve_alpha_with_rgb(old, result[1])
            self._refresh()

    def _set_selected_style_color(self, field: str, value: Optional[str]) -> None:
        for index in self._selected_layer_indices():
            setattr(self.doc.commands[index].style, field, value)
        self._refresh()

    def _editor_press(self, event):
        if self._manual_window is not None and self._manual_window.winfo_exists():
            return
        self._bake_for_direct_edit()
        bbox = self._selection_bbox()
        if bbox is not None:
            _x1, _y1, x2, y2 = bbox
            if abs(event.x - x2) <= self.HANDLE_SIZE + 4 and abs(event.y - y2) <= self.HANDLE_SIZE + 4:
                self._editor_drag_mode = "resize"
                self._editor_resize_start = (event.x, event.y)
                self._editor_resize_bbox = bbox
                self._editor_resize_originals = [
                    (index, self.doc.commands[index].clone())
                    for index in self._selected_group_indices()
                ]
                return
        hit = self._hit_command(event.x, event.y)
        self._editor_selected_index = hit
        self.selected_index = hit
        if hit is not None:
            self._editor_drag_mode = "move"
            self._editor_last_point = (event.x, event.y)
        else:
            self._editor_drag_mode = None
        self._sync_editor_color_labels()
        self._refresh_preview_and_stats()
        self._restore_layer_selection()

    def _editor_drag(self, event):
        if self._editor_drag_mode == "move" and self._editor_last_point is not None:
            dx = event.x - self._editor_last_point[0]
            dy = event.y - self._editor_last_point[1]
            for index in self._selected_group_indices():
                self.doc.commands[index] = translate_command(self.doc.commands[index], int(round(dx)), int(round(dy)))
            self._editor_last_point = (event.x, event.y)
            self._refresh_preview_and_stats()
            return
        if (
            self._editor_drag_mode == "resize"
            and self._editor_resize_start is not None
            and self._editor_resize_bbox is not None
            and self._editor_resize_originals
        ):
            x1, y1, x2, y2 = self._editor_resize_bbox
            cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
            initial = math.hypot(self._editor_resize_start[0] - cx, self._editor_resize_start[1] - cy)
            current = math.hypot(event.x - cx, event.y - cy)
            factor = max(0.05, min(20.0, current / max(initial, 1e-6)))
            matrix = mat_mul(
                mat_translate(cx, cy),
                mat_mul(mat_scale(factor, factor), mat_translate(-cx, -cy)),
            )
            for index, original in self._editor_resize_originals:
                self.doc.commands[index] = transform_command(original, matrix)
            self._refresh_preview_and_stats()

    def _editor_release(self, _event):
        self._editor_drag_mode = None
        self._editor_last_point = None
        self._editor_resize_start = None
        self._editor_resize_bbox = None
        self._editor_resize_originals = []
        self._refresh()

    def duplicate_selected(self):
        indices = self._selected_group_indices()
        if not indices:
            messagebox.showinfo("Duplicate selected", "Select artwork first.", parent=self)
            return
        self._bake_for_direct_edit()
        new_group = next_editor_group(self.doc)
        copies: List[VectorCommand] = []
        for index in indices:
            copied = translate_command(self.doc.commands[index], 24, 24)
            copied.label = f"Copy | {copied.label}"
            copied.editor_group = new_group
            copies.append(copied)
        self.doc.commands.extend(copies)
        self._editor_selected_index = len(self.doc.commands) - 1
        self.selected_index = self._editor_selected_index
        self._refresh()
        self.var_status.set(
            f"Duplicated {len(copies)} command(s). Protocol-v5 local-definition reuse remains automatic."
        )

    def duplicate_all(self):
        if not self.doc.commands:
            messagebox.showinfo("Duplicate artwork", "There is no artwork to duplicate.", parent=self)
            return
        dx = simpledialog.askinteger("Duplicate all", "Horizontal translation in pixels:", parent=self, initialvalue=40, minvalue=-719, maxvalue=719)
        if dx is None:
            return
        dy = simpledialog.askinteger("Duplicate all", "Vertical translation in pixels:", parent=self, initialvalue=40, minvalue=-479, maxvalue=479)
        if dy is None:
            return
        self._bake_for_direct_edit()
        original = [command.clone() for command in self.doc.commands]
        group_map: Dict[Optional[int], int] = {}
        for command in original:
            key = command.editor_group
            if key not in group_map:
                group_map[key] = next_editor_group(self.doc) + len(group_map)
            copied = translate_command(command, dx, dy)
            copied.label = f"Copy | {copied.label}"
            copied.editor_group = group_map[key]
            self.doc.commands.append(copied)
        self._editor_selected_index = len(self.doc.commands) - 1
        self.selected_index = self._editor_selected_index
        self._refresh()
        self.var_status.set(
            f"Duplicated {len(original)} layers by ({dx},{dy}); primitive/vector and translated-group comparisons remain active."
        )

    def delete_layer(self):
        indices = self._selected_group_indices()
        if not indices:
            return
        for index in sorted(indices, reverse=True):
            del self.doc.commands[index]
        self._editor_selected_index = None
        self.selected_index = None
        self._refresh()



# ---- v4 regression tests --------------------------------------------------


def _transport_signature_v4(command: VectorCommand) -> Dict[str, Any]:
    style = command.style.normalized()
    return {
        "opcode": command.opcode,
        "fill": quantize_palette_color(style.fill) if style.fill else None,
        "stroke": quantize_palette_color(style.stroke) if style.stroke else None,
        "stroke_width": int(style.stroke_width),
        "fill_rule": style.fill_rule,
        "geom": command.geom,
    }


# FINAL ACTIVE REGRESSION SUITE
# -----------------------------
# Earlier run_self_test definitions belong to superseded layers. This final
# function is the one main() invokes. Keep protocol fixtures here until the
# code is split into modules and a conventional test package can replace it.

def run_self_test():
    # Generic v5 round trip and alpha.
    base = sample_document()
    base_commands = [quantize_command(command) for command in base.commands]
    encoded = encode_image(base_commands)
    decoded = decode_frames(encoded.frames)
    assert len(decoded) == len(base_commands)
    assert MAX_MESSAGES == 10 and MAX_PAYLOAD_CHARS == 1350

    alpha_svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><circle r="32" cx="35" cy="65" fill="#F00" opacity="0.5"/><circle r="32" cx="65" cy="65" fill="#0F0" opacity="0.5"/><circle r="32" cx="50" cy="35" fill="#00F" opacity="0.5"/></svg>'
    alpha_path = Path("/tmp/mcoreimg-v4-alpha.svg"); alpha_path.write_text(alpha_svg)
    alpha_doc = import_svg(alpha_path); alpha_path.unlink(missing_ok=True)
    alpha_encoded = encode_image(alpha_doc.commands); alpha_decoded = decode_frames(alpha_encoded.frames)
    assert all(alpha4(command.style.fill or "#000000") == 8 for command in alpha_decoded)
    if Image is not None:
        alpha_image = render_to_pillow(alpha_decoded)
        assert alpha_image.getpixel((360, 330)) != alpha_image.getpixel((220, 330))
        alpha_image.close()

    # Old primitive chooses compact encoding when strictly better, and renders
    # identically to its generic-vector expansion.
    yagi = quantize_command(VectorCommand(OP_PRIMITIVE, PaintStyle(None, "#112233", 2), {
        "kind": PRIM_YAGI, "x": 180, "y": 100, "orientation": 1, "scale": 2,
    }, "test yagi"))
    yagi_encoded = encode_image([yagi])
    assert yagi_encoded.stats.primitive_count == 1
    yagi_decoded = decode_frames(yagi_encoded.frames)
    assert len(yagi_decoded) == 1 and yagi_decoded[0].opcode == OP_PRIMITIVE

    # The comparison is genuine rather than a primitive-first preference: when
    # a DoubleBox follows the exact rectangle/line state its vector expansion
    # is smaller, so protocol v5 deliberately transmits generic vectors.
    double_box = quantize_command(VectorCommand(OP_PRIMITIVE, PaintStyle(None, "#FF0000", 2), {
        "kind": PRIM_DOUBLE_BOX, "x1": 100, "y1": 100, "x2": 200, "y2": 200, "percent": 50,
    }, "test doublebox"))
    double_vectors = [quantize_command(item) for item in primitive_to_vectors(double_box) or []]
    comparison_encoded = encode_image(double_vectors + [double_box])
    assert comparison_encoded.stats.vectorized_primitive_count == 1
    if Image is not None:
        primitive_image = render_to_pillow([yagi])
        vector_image = render_to_pillow([quantize_command(item) for item in primitive_to_vectors(yagi) or []])
        assert primitive_image.tobytes() == vector_image.tobytes()
        primitive_image.close(); vector_image.close()

    # A translated copy of a multi-command SVG-style group must become one
    # group-copy record rather than independent command copies.
    group = [
        VectorCommand(OP_RECT, PaintStyle("#3366CC", None, 0), {"x": 30, "y": 40, "w": 80, "h": 40}, "g rect"),
        VectorCommand(OP_ELLIPSE, PaintStyle("#FFCC00", "#000000", 2), {"cx": 70, "cy": 100, "rx": 25, "ry": 15}, "g ellipse"),
        VectorCommand(OP_PATH, PaintStyle(None, "#990000", 2), {"segments": [
            {"op": SEG_M, "points": [(30, 130)]}, {"op": SEG_C, "points": [(50, 110), (90, 150), (110, 130)]},
        ]}, "g path"),
    ]
    copied = [translate_command(command, 240, 90) for command in group]
    group_encoded = encode_image(group + copied)
    assert group_encoded.stats.group_repeat_count >= 1
    group_decoded = decode_frames(group_encoded.frames)
    assert len(group_decoded) == 6
    for source, target in zip(group_decoded[:3], group_decoded[3:]):
        assert geom_translation(source, target) == (240, 90)

    # Multiple SVG files append rather than replace the document, and an exact
    # translated second import is eligible for one group-copy record.
    multi_svg_path = Path("/tmp/mcoreimg-v4-multisvg.svg")
    multi_svg_path.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><rect x="10" y="10" width="20" height="20" fill="#123456"/><circle cx="60" cy="60" r="15" fill="#654321"/></svg>')
    multi_doc = VectorDocument(source_name="multi-svg-test")
    files_added, commands_added = append_source_files(multi_doc, [multi_svg_path, multi_svg_path], 0, 0, 30, 20)
    multi_svg_path.unlink(missing_ok=True)
    assert files_added == 2 and commands_added == 4 and len(multi_doc.commands) == 4
    multi_encoded = encode_image([quantize_command(command) for command in multi_doc.commands])
    assert multi_encoded.stats.group_repeat_count >= 1

    # Source JSON accepts primitive commands.
    primitive_doc = VectorDocument([yagi], source_name="primitive-test")
    restored = VectorDocument.from_json(primitive_doc.to_json())
    assert restored.commands[0].opcode == OP_PRIMITIVE

    # Editor grouping survives source JSON but remains transport-free.
    grouped = yagi.clone(); grouped.editor_group = 77
    grouped_doc = VectorDocument([grouped], source_name="editor-group-test")
    grouped_restored = VectorDocument.from_json(grouped_doc.to_json())
    assert grouped_restored.commands[0].editor_group == 77
    assert encode_image([grouped]).payload == encode_image([yagi]).payload

    # Corruption detection remains active.
    broken = encoded.frames.copy(); position = FRAME_HEADER_LEN
    broken[0] = broken[0][:position] + BASE91[(BASE91_INDEX[broken[0][position]] + 1) % 91] + broken[0][position + 1:]
    try: decode_frames(broken)
    except FrameError: pass
    else: raise AssertionError("Corrupted frame was accepted")

    # Local-space SVG groups must have scale-independent payload size. The
    # displayed transform is fixed-width while the local path definition stays
    # unchanged. A repeated copy also reuses that definition.
    scaling_path = Path("/tmp/mcoreimg-v5-scaling.svg")
    scaling_path.write_text("""<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 1000 600'><path d='M10 10 C150 590 850 10 990 590 L500 300 Z' fill='#445566'/><path d='M80 500 Q500 40 920 500' fill='none' stroke='#ffffff' stroke-width='8'/></svg>""")
    scaling_doc = import_svg(scaling_path); scaling_path.unlink(missing_ok=True)
    assign_editor_group(scaling_doc.commands, 1)
    full = encode_image(scaling_doc.transformed_commands())
    scaling_doc.scale = 0.1
    small = encode_image(scaling_doc.transformed_commands())
    assert full.stats.base91_chars == small.stats.base91_chars
    assert full.stats.transformed_group_count == 1
    assert small.stats.transformed_group_count == 1
    copied = [command.clone() for command in scaling_doc.commands]
    assign_editor_group(copied, 2)
    scaling_doc.commands.extend([translate_command(command, 20, 20) for command in copied])
    repeated = encode_image(scaling_doc.transformed_commands())
    assert repeated.stats.transformed_group_reference_count >= 1

    print("MCoreIMG Hybrid SVG Constructor self-test: PASS")
    print(f"protocol={PROTOCOL_VERSION} payload={encoded.stats.base91_chars} frames={encoded.stats.frame_count} primitive={yagi_encoded.stats.primitive_count} group_copies={group_encoded.stats.group_repeat_count}")


def verify_build_integrity() -> None:
    """Fail at startup if a stale/superseded layer became active by accident.

    This guard exists because the file contains intentional redefinitions.
    It does not prove protocol correctness, but it catches the recurring class
    of packaging mistakes where an older Constructor was distributed under a
    newer filename.
    """
    required = {
        "protocol": PROTOCOL_VERSION == 5,
        "messages": MAX_MESSAGES == 10,
        "primitive opcode": OP_PRIMITIVE in OP_NAMES,
        "local-space record": "REC_TRANSFORM_GROUP" in globals(),
        "group-copy record": "REC_GROUP_REPEAT" in globals(),
        "undo": hasattr(ConstructorApp, "undo_last_action"),
        "multi-SVG": hasattr(ConstructorApp, "append_svg"),
        "drawing mode": hasattr(ConstructorApp, "open_drawing_mode"),
    }
    failed = [name for name, ok in required.items() if not ok]
    if failed:
        raise RuntimeError("Build-integrity failure: " + ", ".join(failed))


def main(argv:Optional[Sequence[str]]=None)->int:
    """CLI entry point for version reporting, regression tests, or the Tk GUI."""
    parser=argparse.ArgumentParser(description="MCoreIMG protocol-v5 local-space hybrid SVG and old drawing Constructor")
    parser.add_argument("--self-test",action="store_true")
    parser.add_argument("--version",action="store_true")
    args=parser.parse_args(argv)
    verify_build_integrity()
    if args.version:
        print(CONSTRUCTOR_BUILD)
        print(FEATURE_SIGNATURE)
        print(f"protocol={PROTOCOL_VERSION} messages={MAX_MESSAGES} source_version={SOURCE_VERSION}")
        return 0
    if args.self_test:run_self_test();return 0
    app=ConstructorApp();app.mainloop();return 0


if __name__=="__main__":
    raise SystemExit(main())
