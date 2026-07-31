#!/usr/bin/env python3
from __future__ import annotations
"""
MCoreIMG SVG Constructor — protocol-v2 SVG/vector branch
=========================================================

A standalone Constructor for importing, scaling, editing, previewing, saving,
and encoding a practical SVG subset for MCoreIMG over MeshCore.

Highlights
----------
* Imports SVG, SVGZ, and MCoreIMG v2 JSON sources.
* Fits imported SVG artwork inside the fixed 720x480 canvas by default.
* Keeps document scale and offset non-destructive until explicitly baked.
* Supports rect, circle, ellipse, line, polyline, polygon, and SVG path.
* Supports nested transforms and inherited flat fill/stroke styles.
* Normalizes path H/V/S/T commands and approximates SVG arcs with line nodes.
* Uses a per-image RGB565+A4 palette, opcode-local style/point state, predictive
  coordinates, Exp-Golomb/Rice coding, and nonadjacent translated repeats.
* Uses a ten-by-150-character MeshCore envelope with Base91,
  per-frame CRC-16, and stream CRC-32.

This branch intentionally uses compressed protocol version 2. A protocol-v1
Reconstructor will not decode its new vector/path opcodes.

Run:
    python MCoreIMG-SVG-Constructor.py

Self-test:
    python MCoreIMG-SVG-Constructor.py --self-test

Arch Linux:
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
from tkinter import filedialog, messagebox, ttk
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

CANVAS_W = 720
CANVAS_H = 480
BACKGROUND = "#FFFFFF"
DEFAULT_MARGIN = 8
PROTOCOL_VERSION = 3
SOURCE_FORMAT = "MCoreIMG-SVG-source"
SOURCE_VERSION = 2
CONSTRUCTOR_BUILD = "2026.07.31-svg-v3.3-ALPHA-ROUNDTRIP-10MSG"

MAX_MESSAGES = 10
MESSAGE_LEN = 150
FRAME_HEADER_LEN = 15
FRAME_PAYLOAD_LEN = MESSAGE_LEN - FRAME_HEADER_LEN
MAX_PAYLOAD_CHARS = MAX_MESSAGES * FRAME_PAYLOAD_LEN
FRAME_MAGIC = "MCI"
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
    pass


class SVGImportError(MCIError):
    pass


class FrameError(MCIError):
    pass


# ---------------------------------------------------------------------------
# Vector model
# ---------------------------------------------------------------------------


@dataclass
class PaintStyle:
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
    opcode: int
    style: PaintStyle
    geom: Dict[str, Any]
    label: str = ""
    visible: bool = True

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
        )
        validate_command(cmd)
        return cmd


@dataclass
class VectorDocument:
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
        return [quantize_command(c) for c in cmds]

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


def main(argv:Optional[Sequence[str]]=None)->int:
    parser=argparse.ArgumentParser(description="MCoreIMG protocol-v3 SVG Constructor")
    parser.add_argument("--self-test",action="store_true")
    args=parser.parse_args(argv)
    if args.self_test:run_self_test();return 0
    app=ConstructorApp();app.mainloop();return 0


if __name__=="__main__":
    raise SystemExit(main())
