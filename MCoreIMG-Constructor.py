#!/usr/bin/env python3
"""
MCoreIMG Constructor — SVG import, editing, preview, and export
==============================================================

The Constructor is the authoring half of MCoreIMG.  It imports a practical SVG
subset, lets an operator combine and edit artwork alongside compact radio
primitives, previews the transport-visible result, and writes MeshCore frames.

It does **not** implement the model or the codec.  The drawing vocabulary
lives in ``MCoreIMG-model.py``, and every bit of encoding, decoding, palette
construction, record selection, and framing lives in
``MCoreIMG-compression.py``.  If you are changing how images are compressed,
you are in the wrong file.

    MCoreIMG-model.py           <- opcodes, model, geometry
    MCoreIMG-compression.py     <- bitstream codec and MeshCore framing
    MCoreIMG-Constructor.py     <- you are here: SVG, rendering, editor GUI
    MCoreIMG-Reconstructor.py   <- frame decoding and export

WHAT LIVES HERE
---------------

1.  :class:`VectorDocument` — the editable document, its non-destructive
    document transform, and editable-JSON serialization.
2.  The SVG importer: path parsing, transform and style resolution, a small
    CSS subset, ``<use>`` expansion, and shape normalization.
3.  Rendering to Tk (interactive canvas) and to Pillow (preview and PNG).
4.  Multi-SVG composition helpers and editor-only grouping.
5.  The Tk GUI.
6.  The regression suite, build-integrity guard, and command-line entry point.

RELATIONSHIP TO THE CODEC
-------------------------

The editor speaks the same model the codec does: ``VectorCommand`` objects
carrying a ``PaintStyle`` and an opcode-specific ``geom`` dictionary.  Those
types are defined by the model module, which both this file and the codec
import, so the editor and the transport can never disagree about them.

``editor_group`` is the one piece of purely editorial metadata.  It lets the
GUI move an imported SVG as a single object, and it is also the hint the
encoder uses to propose a local-space group.  It is never transmitted directly.

Preview rendering deliberately draws the *decoded* command stream whenever the
image fits the ten-message budget, so the screen shows palette quantization,
alpha quantization, and coordinate precision exactly as a receiver would.

GUI STRUCTURE
-------------

The GUI is three cooperating classes:

``_DocumentAppBase``
    Document lifecycle, layer list, file open/save, preview and statistics.
``_DrawingAppBase``
    Adds multi-SVG import, editor grouping, and Old Drawing Mode.
``ConstructorApp``
    Adds direct canvas editing — select, drag, scale, recolour — plus undo.

These were previously three top-level definitions all named ``ConstructorApp``,
chained together by alias assignments and resolved only by Python's
last-definition-wins rule.  They are now named for what they do and inherit
explicitly.  Collapsing them into a single class is still worthwhile but
requires interactive GUI testing, so it is deliberately left as a separate
change.

Run:
    python MCoreIMG-Constructor.py

Version and self-test:
    python MCoreIMG-Constructor.py --version
    python MCoreIMG-Constructor.py --self-test

Arch Linux dependencies:
    sudo pacman -Syu python tk python-pillow
"""

from __future__ import annotations

import argparse
import copy
import gzip
import importlib.util
import json
import math
import re
import subprocess
import sys
import tkinter as tk
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox, simpledialog, ttk
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple
from xml.etree import ElementTree as ET

try:
    from PIL import Image, ImageColor, ImageDraw, ImageFont, ImageTk
except ImportError:
    Image = None
    ImageColor = None
    ImageDraw = None
    ImageFont = None
    ImageTk = None


# ===========================================================================
# Codec import
# ===========================================================================
#
# The model and the codec ship as hyphenated filenames, which are not legal
# Python identifiers, so they are loaded by path rather than by ``import``.
# Loading the codec pulls in the model, so only the codec is loaded here.
# Both files are expected to sit beside this one; --compression overrides that.

COMPRESSION_FILENAME = "MCoreIMG-compression.py"


def load_compression(explicit: Optional[str] = None):
    """Import the compression module from an explicit path or from beside us.

    Kept as a function so tooling and tests can load an alternate codec build
    without editing this file.
    """
    candidates = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.append(Path(__file__).resolve().parent / COMPRESSION_FILENAME)
    candidates.append(Path.cwd() / COMPRESSION_FILENAME)

    tried = []
    for path in candidates:
        tried.append(str(path))
        if not path.is_file():
            continue

        # Reuse an already-imported codec rather than creating a second module
        # object. Two copies would define two distinct VectorCommand classes,
        # and objects decoded by one would not be recognised by the other. The
        # Reconstructor imports the codec before importing this file, so this
        # path is taken in normal use.
        existing = sys.modules.get("mcoreimg_compression")
        if existing is not None:
            existing_file = getattr(existing, "__file__", None)
            if existing_file and Path(existing_file).resolve() == path.resolve():
                return existing

        spec = importlib.util.spec_from_file_location("mcoreimg_compression", path)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules["mcoreimg_compression"] = module
        spec.loader.exec_module(module)
        return module

    searched = "\n".join(f"  - {item}" for item in tried)
    raise SystemExit(
        f"Cannot find {COMPRESSION_FILENAME}.\n\n"
        f"Place it beside this file, or pass --compression /path/to/{COMPRESSION_FILENAME}.\n\n"
        f"Searched:\n{searched}"
    )


def _early_compression_arg(argv: Optional[Sequence[str]] = None) -> Optional[str]:
    """Read --compression before argparse runs.

    The codec must be imported at module import time because it defines the
    model classes used by everything below, which is earlier than argparse can
    run. This scan keeps the flag usable without duplicating the parser.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    for index, item in enumerate(args):
        if item == "--compression" and index + 1 < len(args):
            return args[index + 1]
        if item.startswith("--compression="):
            return item.split("=", 1)[1]
    return None


mci = load_compression(_early_compression_arg())
model = mci.model

# Names used throughout this file. Everything here is defined by the codec so
# that the editor and the transport can never disagree about the model.
MCIError = mci.MCIError
FrameError = mci.FrameError
PaintStyle = mci.PaintStyle
VectorCommand = mci.VectorCommand
Matrix = mci.Matrix
IDENTITY = mci.IDENTITY

PROTOCOL_VERSION = mci.PROTOCOL_VERSION
CANVAS_W = mci.CANVAS_W
CANVAS_H = mci.CANVAS_H
MAX_MESSAGES = mci.MAX_MESSAGES
MESSAGE_LEN = mci.MESSAGE_LEN
FRAME_HEADER_LEN = mci.FRAME_HEADER_LEN
FRAME_PAYLOAD_LEN = mci.FRAME_PAYLOAD_LEN
MAX_PAYLOAD_CHARS = mci.MAX_PAYLOAD_CHARS
MAX_COMMANDS = mci.MAX_COMMANDS
MAX_PALETTE = mci.MAX_PALETTE

OP_RECT = mci.OP_RECT
OP_ELLIPSE = mci.OP_ELLIPSE
OP_LINE = mci.OP_LINE
OP_POLYLINE = mci.OP_POLYLINE
OP_POLYGON = mci.OP_POLYGON
OP_PATH = mci.OP_PATH
OP_PRIMITIVE = mci.OP_PRIMITIVE
OP_NAMES = mci.OP_NAMES

SEG_M = mci.SEG_M
SEG_L = mci.SEG_L
SEG_Q = mci.SEG_Q
SEG_C = mci.SEG_C
SEG_Z = mci.SEG_Z
SEG_NAMES = mci.SEG_NAMES

PRIM_TEXT = mci.PRIM_TEXT
PRIM_TRIANGLE_OUTLINE = mci.PRIM_TRIANGLE_OUTLINE
PRIM_TRIANGLE_FILL = mci.PRIM_TRIANGLE_FILL
PRIM_ARROW = mci.PRIM_ARROW
PRIM_STAR = mci.PRIM_STAR
PRIM_ARC = mci.PRIM_ARC
PRIM_YAGI = mci.PRIM_YAGI
PRIM_DISH = mci.PRIM_DISH
PRIM_RADIO = mci.PRIM_RADIO
PRIM_RADIO_WAVES = mci.PRIM_RADIO_WAVES
PRIM_MOON = mci.PRIM_MOON
PRIM_DOUBLE_BOX = mci.PRIM_DOUBLE_BOX
PRIMITIVE_NAMES = mci.PRIMITIVE_NAMES
PRIMITIVE_BY_NAME = mci.PRIMITIVE_BY_NAME
TEXT_ALPHABET = mci.TEXT_ALPHABET
MAX_TEXT_LEN = mci.MAX_TEXT_LEN
MOON_CRATER_POINTS = mci.MOON_CRATER_POINTS
primitive_to_vectors = mci.primitive_to_vectors
primitive_kind = mci.primitive_kind
primitive_anchor = mci.primitive_anchor
clean_primitive_text = mci.clean_primitive_text

mat_mul = mci.mat_mul
mat_translate = mci.mat_translate
mat_scale = mci.mat_scale
mat_rotate = mci.mat_rotate
apply_mat = mci.apply_mat
is_axis_aligned = mci.is_axis_aligned
clamp_int = mci.clamp_int
normalize_hex = mci.normalize_hex
color_to_rgba = mci.color_to_rgba
rgba_to_hex = mci.rgba_to_hex
cubic_point = mci.cubic_point
quad_point = mci.quad_point
flatten_path = mci.flatten_path
command_points = mci.command_points
commands_bbox = mci.commands_bbox
transform_command = mci.transform_command
translate_command = mci.translate_command
quantize_command = mci.quantize_command
validate_command = mci.validate_command
geom_translation = mci.geom_translation

build_palette = mci.build_palette
quantize_palette_color = mci.quantize_palette_color
rgb565 = mci.rgb565
alpha4 = mci.alpha4
from_rgb565 = mci.from_rgb565
from_rgb565_a4 = mci.from_rgb565_a4

BASE62 = mci.BASE62
BASE91 = mci.BASE91
BASE91_INDEX = mci.BASE91_INDEX

CodecStats = mci.CodecStats
EncodedImage = mci.EncodedImage
encode_image = mci.encode_image
decode_frames = mci.decode_frames
encode_commands = mci.encode_commands
decode_commands = mci.decode_commands


# ===========================================================================
# Editor constants
# ===========================================================================

# BACKGROUND affects preview/export compositing but is not transmitted as an
# explicit command. A configurable background would need either a protocol
# field or an agreed Reconstructor default.
BACKGROUND = "#FFFFFF"
DEFAULT_MARGIN = 8

# The editable JSON format is an editor concern and may evolve independently
# of the transport protocol.
SOURCE_FORMAT = "MCoreIMG-SVG-source"
SOURCE_VERSION = 6

CONSTRUCTOR_BUILD = "2026.08.05-svg-v6.0-MODULAR-SLIMHEADER-10MSG"
FEATURE_SIGNATURE = "PROTO6|LOCALSPACE|HYBRID|PRIMITIVES|GROUPCOPY|ALPHA|UNDO|10MSG"

CSS_NAMED_FALLBACK = {
    "black": "#000000", "white": "#FFFFFF", "red": "#FF0000",
    "green": "#008000", "blue": "#0000FF", "yellow": "#FFFF00",
    "gray": "#808080", "grey": "#808080", "silver": "#C0C0C0",
    "maroon": "#800000", "purple": "#800080", "fuchsia": "#FF00FF",
    "lime": "#00FF00", "olive": "#808000", "navy": "#000080",
    "teal": "#008080", "aqua": "#00FFFF", "orange": "#FFA500",
    "transparent": "#FFFFFF",
}


# ==========================================================================
# Editor errors
# ==========================================================================

class SVGImportError(MCIError):
    """Raised when SVG/XML cannot be normalized into the supported model."""

    pass

# ==========================================================================
# Editable document
# ==========================================================================

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

# ==========================================================================
# SVG value parsing
# ==========================================================================

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

# ==========================================================================
# SVG path parsing
# ==========================================================================

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

# ==========================================================================
# SVG importer
# ==========================================================================

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

# ==========================================================================
# Rendering
# ==========================================================================

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
            kind = primitive_kind(cmd)
            if kind == PRIM_TEXT:
                color = color_to_rgba(cmd.style.fill or cmd.style.stroke or "#000000")
                if color:
                    x, y = int(cmd.geom["x"]), int(cmd.geom["y"])
                    text = clean_primitive_text(cmd.geom.get("text", ""))
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

# ==========================================================================
# Source file helpers
# ==========================================================================

def save_source(path:str|Path,doc:VectorDocument)->None:
    Path(path).write_text(json.dumps(doc.to_json(),indent=2)+"\n",encoding="utf-8")


def load_source(path:str|Path)->VectorDocument:
    path=Path(path)
    if path.suffix.lower() in {".svg",".svgz"}: return import_svg(path)
    obj=json.loads(path.read_text(encoding="utf-8"))
    return VectorDocument.from_json(obj)

# ==========================================================================
# Multi-SVG composition helpers
# ==========================================================================

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

# ==========================================================================
# GUI
# ==========================================================================

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


class _DocumentAppBase(tk.Tk):
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


class _DrawingAppBase(_DocumentAppBase):
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
            name = PRIMITIVE_NAMES.get(primitive_kind(command), "Primitive") if command.opcode == OP_PRIMITIVE else OP_NAMES[command.opcode]
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
        if kind == PRIM_TEXT: geom["text"] = clean_primitive_text(self.manual_text.get())
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


class ConstructorApp(_DrawingAppBase):
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
        if command.opcode != OP_PRIMITIVE or primitive_kind(command) != PRIM_MOON:
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

# ==========================================================================
# Self-test and entry point
# ==========================================================================

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


def _transport_signature(command: VectorCommand) -> Dict[str, Any]:
    style = command.style.normalized()
    return {
        "opcode": command.opcode,
        "fill": quantize_palette_color(style.fill) if style.fill else None,
        "stroke": quantize_palette_color(style.stroke) if style.stroke else None,
        "stroke_width": int(style.stroke_width),
        "fill_rule": style.fill_rule,
        "geom": command.geom,
    }


def run_self_test():
    # Generic v5 round trip and alpha.
    base = sample_document()
    base_commands = [quantize_command(command) for command in base.commands]
    encoded = encode_image(base_commands)
    decoded = decode_frames(encoded.frames)
    assert len(decoded) == len(base_commands)
    assert MAX_MESSAGES == 10 and MAX_PAYLOAD_CHARS == 1420

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
    """Fail fast if the Constructor and the codec module are mismatched.

    The single-file build used this to catch a superseded layer becoming
    active by accident. With the codec extracted, the equivalent packaging
    mistake is shipping this file beside an older or newer
    MCoreIMG-compression.py, so the guard now verifies the pairing and the
    feature set both halves are expected to provide.
    """
    required = {
        # Codec identity and transport profile.
        "model loaded": getattr(mci, "model", None) is not None,
        "model canvas": getattr(mci, "CANVAS_W", None) == 720
                        and getattr(mci, "CANVAS_H", None) == 480,
        "codec protocol 6": getattr(mci, "PROTOCOL_VERSION", None) == 6,
        "codec 10-message envelope": getattr(mci, "MAX_MESSAGES", None) == 10,
        "codec 150-char messages": getattr(mci, "MESSAGE_LEN", None) == 150,
        # Record types the v5 transport contract requires.
        "local-space record": hasattr(mci, "REC_TRANSFORM_GROUP"),
        "group-copy record": hasattr(mci, "REC_GROUP_REPEAT"),
        "single-repeat record": hasattr(mci, "REC_SINGLE_REPEAT"),
        # Codec entry points this editor calls.
        "codec encode_image": callable(getattr(mci, "encode_image", None)),
        "codec decode_frames": callable(getattr(mci, "decode_frames", None)),
        # Model and primitive support.
        "primitive opcode": OP_PRIMITIVE in OP_NAMES,
        "primitive expansion": callable(getattr(mci, "primitive_to_vectors", None)),
        "alpha palette": callable(getattr(mci, "alpha4", None)),
        # Editor features named in FEATURE_SIGNATURE.
        "undo": hasattr(ConstructorApp, "undo_last_action"),
        "multi-SVG": hasattr(ConstructorApp, "append_svg"),
        "drawing mode": hasattr(ConstructorApp, "open_drawing_mode"),
    }
    failed = [name for name, ok in required.items() if not ok]
    if failed:
        raise RuntimeError(
            "Build-integrity failure: " + ", ".join(failed)
            + f"\n  Constructor build: {CONSTRUCTOR_BUILD}"
            + f"\n  Codec build:       {getattr(mci, 'COMPRESSION_BUILD', 'unknown')}"
            + f"\n  Model build:       {getattr(getattr(mci, 'model', None), 'MODEL_BUILD', 'unknown')}"
        )


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point for version reporting, regression tests, or the Tk GUI."""
    parser = argparse.ArgumentParser(
        description="MCoreIMG protocol-v5 local-space hybrid SVG and old drawing Constructor")
    parser.add_argument("--self-test", action="store_true",
                        help="run the regression suite and exit")
    parser.add_argument("--version", action="store_true",
                        help="print build, feature signature, and codec identity")
    parser.add_argument("--compression", metavar="PATH",
                        help=f"path to {COMPRESSION_FILENAME} (default: beside this file)")
    args = parser.parse_args(argv)

    verify_build_integrity()

    if args.version:
        print(CONSTRUCTOR_BUILD)
        print(FEATURE_SIGNATURE)
        print(f"protocol={PROTOCOL_VERSION} messages={MAX_MESSAGES} source_version={SOURCE_VERSION}")
        print(f"codec={getattr(mci, 'COMPRESSION_BUILD', 'unknown')}")
        print(f"codec_path={getattr(mci, '__file__', 'unknown')}")
        print(f"model={getattr(model, 'MODEL_BUILD', 'unknown')}")
        print(f"model_path={getattr(model, '__file__', 'unknown')}")
        return 0

    if args.self_test:
        run_self_test()
        return 0

    app = ConstructorApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
