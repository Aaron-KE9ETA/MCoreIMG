#!/usr/bin/env python3
"""
MCoreIMG model — the shared drawing model
=========================================

This module defines *what an MCoreIMG image is*, independent of how it is
compressed, rendered, or edited.  It is the bottom of the dependency stack and
imports nothing from the rest of the project.

    MCoreIMG-model.py           <- you are here: opcodes, model, geometry
    MCoreIMG-compression.py     <- bitstream codec and MeshCore framing
    MCoreIMG-Constructor.py     <- SVG import, rendering, editor GUI
    MCoreIMG-Reconstructor.py   <- frame decoding and export

WHAT LIVES HERE
---------------

1.  The canvas bounds and the opcode, path-segment, and primitive vocabularies.
2.  :class:`PaintStyle` and :class:`VectorCommand` — the objects every other
    module passes around.
3.  Affine matrix helpers and colour normalization.
4.  The geometry operations whose behaviour both the editor and the codec must
    agree on: ``transform_command``, ``quantize_command``, ``validate_command``,
    ``command_points``, ``commands_bbox``, and curve flattening.
5.  Expansion of compact primitives into generic vector commands.

WHAT DOES NOT LIVE HERE
-----------------------

Anything that knows about bits, palettes, records, or frames.  There is no
RGB565 quantization here, no Exp-Golomb, no CRC.  If a function's behaviour
would change when the wire format changes, it belongs in the compression
module instead.

The dividing line is drawn at *encoding*, not at *quantization*.  Rounding —
of coordinates and of colours — lives here, because ``quantize_command`` has to
round a Moon's crater colour and the editor has to preview exactly what will be
transmitted.  Turning those rounded values into bits does not.

So ``quantize_palette_color`` is here, while ``build_palette`` is in the codec:
assembling a palette and enforcing the 32-entry ceiling is a wire-format
decision, but knowing what a colour becomes on the air is a model one.

WHY THE GEOMETRY OPERATIONS ARE SHARED
--------------------------------------

The encoder prices a primitive against its vector expansion, the editor draws
what the operator will actually transmit, and the decoder reconstructs from the
same rules.  If any of the three rounded or validated differently, previews
would stop matching transmissions.  Keeping one implementation here is what
makes preview parity possible.

GENERIC VERSUS PRIMITIVE COMMANDS
---------------------------------

``OP_PRIMITIVE`` commands carry a compact radio-oriented shape (a Yagi, a dish,
a moon) rather than explicit geometry.  The public geometry functions dispatch:
primitives get their own anchor and expansion rules via
:func:`primitive_to_vectors`, and everything else falls through to the
``_generic_*`` implementations.
"""

from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

MODEL_BUILD = "2026.08.05-model-v6.0-MODULAR"


# ==========================================================================
# Canvas and vocabulary
# ==========================================================================


# Canvas bounds belong to the model because command validation and coordinate
# clamping depend on them. The codec sizes its absolute point fields from the
# same numbers, so changing these is both a model and a protocol change.
CANVAS_W = 720
CANVAS_H = 480

# Generic vector opcodes.
OP_RECT = 0
OP_ELLIPSE = 1
OP_LINE = 2
OP_POLYLINE = 3
OP_POLYGON = 4
OP_PATH = 5
OP_PRIMITIVE = 6
OP_NAMES = {
    OP_RECT: "Rectangle",
    OP_ELLIPSE: "Ellipse",
    OP_LINE: "Line",
    OP_POLYLINE: "Polyline",
    OP_POLYGON: "Polygon",
    OP_PATH: "Path",
    OP_PRIMITIVE: "Primitive",
}

# Path segment opcodes and how many points each one carries.
SEG_M = 0
SEG_L = 1
SEG_Q = 2
SEG_C = 3
SEG_Z = 4
SEG_NAMES = {SEG_M: "M", SEG_L: "L", SEG_Q: "Q", SEG_C: "C", SEG_Z: "Z"}
SEG_POINT_COUNTS = {SEG_M: 1, SEG_L: 1, SEG_Q: 2, SEG_C: 3, SEG_Z: 0}

# Compact radio-oriented primitives.
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

# Six-bit text alphabet for PRIM_TEXT. The codec packs indices into this table,
# so the ordering is part of the transport contract and must not be reordered.
TEXT_ALPHABET = " 0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ-+./?:,()[]#_=@!&%"
TEXT_INDEX = {ch: i for i, ch in enumerate(TEXT_ALPHABET)}
MAX_TEXT_LEN = 63
assert len(TEXT_ALPHABET) <= 64

# Fixed crater layout for PRIM_MOON, as (x fraction, y fraction, radius).
# Both ends reproduce craters from this table rather than transmitting them.
MOON_CRATER_POINTS = [
    (1 / 5, 1 / 4, 3), (3 / 7, 5 / 8, 5), (1 / 4, 7 / 9, 4),
    (4 / 5, 2 / 7, 2), (7 / 12, 1 / 5, 6), (2 / 3, 2 / 5, 3),
    (5 / 8, 3 / 4, 4), (7 / 20, 3 / 7, 2), (3 / 20, 5 / 9, 5),
    (3 / 4, 3 / 5, 3),
]

Matrix = Tuple[float, float, float, float, float, float]
IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

# ==========================================================================
# Errors
# ==========================================================================


class MCIError(ValueError):
    """Base exception for invalid MCoreIMG model, codec, or stream data.

    The codec subclasses this for transport failures, so callers can catch
    ``MCIError`` to cover both malformed geometry and malformed frames.
    """

    pass

# ==========================================================================
# Transport model
# ==========================================================================

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

# ==========================================================================
# Affine matrices and colour primitives
# ==========================================================================

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

# ==========================================================================
# Geometry operations
# ==========================================================================

# Generic (non-primitive) behaviour lives in the ``_generic_*`` functions. The
# public wrappers dispatch: primitives need their own anchor and expansion
# rules, everything else falls through unchanged. In the original single-file
# build these pairs were two top-level definitions of the same name, the
# earlier one kept alive only by an alias; they are now explicit.


# Curve flattening. ``command_points`` needs it to compute path bounding
# boxes, which drive both editor selection and the codec's local-space
# normalization, so it belongs to the shared model.


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


def _generic_command_points(cmd: VectorCommand) -> List[Tuple[float, float]]:
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


def _generic_transform_command(cmd: VectorCommand, m: Matrix) -> VectorCommand:
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


def _generic_quantize_command(cmd: VectorCommand) -> VectorCommand:
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


def _generic_validate_command(cmd: VectorCommand) -> None:
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


def command_points(cmd: VectorCommand) -> List[Tuple[float, float]]:
    if cmd.opcode != OP_PRIMITIVE:
        return _generic_command_points(cmd)
    kind = primitive_kind(cmd)
    g = cmd.geom
    if kind == PRIM_TEXT:
        x, y = float(g["x"]), float(g["y"])
        text = clean_primitive_text(g.get("text", ""))
        return [(x, y), (x + max(1, len(text)) * 13, y + 22)]
    expanded = primitive_to_vectors(cmd)
    return [p for vector in (expanded or []) for p in _generic_command_points(vector)]


def transform_command(cmd: VectorCommand, m: Matrix) -> VectorCommand:
    if cmd.opcode != OP_PRIMITIVE:
        return _generic_transform_command(cmd, m)
    out = cmd.clone()
    g = out.geom
    kind = primitive_kind(out)
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
        return _generic_quantize_command(cmd)
    out = cmd.clone()
    out.style = out.style.normalized()
    if out.style.stroke:
        out.style.stroke_width = max(1, min(64, int(round(out.style.stroke_width))))
    g = out.geom
    kind = primitive_kind(out)
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
        g["text"] = clean_primitive_text(g.get("text", ""))
    if kind == PRIM_MOON:
        g["crater_color"] = quantize_palette_color(str(g.get("crater_color", "#808080")))
    validate_command(out)
    return out


def validate_command(cmd: VectorCommand) -> None:
    if cmd.opcode != OP_PRIMITIVE:
        _generic_validate_command(cmd)
        return
    kind = primitive_kind(cmd)
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
        if len(clean_primitive_text(g.get("text", ""))) > MAX_TEXT_LEN:
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


def commands_bbox(commands: Sequence[VectorCommand]) -> Tuple[float, float, float, float]:
    points = [p for c in commands for p in command_points(c)]
    if not points:
        return 0.0, 0.0, 0.0, 0.0
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    # Include half stroke width in the bounds.
    pad = max((c.style.stroke_width/2 for c in commands if c.style.stroke), default=0.0)
    return min(xs)-pad, min(ys)-pad, max(xs)+pad, max(ys)+pad


def translate_command(cmd: VectorCommand, dx: int, dy: int) -> VectorCommand:
    return transform_command(cmd, mat_translate(dx, dy))

# ==========================================================================
# Colour quantization
# ==========================================================================


# RGB565 with 4-bit alpha is where the model and the wire format touch.
# ``quantize_command`` rounds a Moon's crater colour through these functions,
# and the editor renders previews from the same values, so they live with the
# geometry quantization they are part of rather than in the codec.
#
# ``build_palette`` is *not* here: assembling a palette and enforcing the
# 32-entry limit is a codec concern, and it lives in MCoreIMG-compression.py.
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

# ==========================================================================
# Compact primitive expansion
# ==========================================================================

def clean_primitive_text(value: Any) -> str:
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


def primitive_kind(cmd: VectorCommand) -> int:
    return int(cmd.geom.get("kind", -1))


def primitive_anchor(cmd: VectorCommand) -> Tuple[float, float]:
    g = cmd.geom
    if primitive_kind(cmd) == PRIM_DOUBLE_BOX:
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
    kind = primitive_kind(cmd)
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


# ===========================================================================
# Public API
# ===========================================================================

__all__ = [
    "MODEL_BUILD",
    # canvas
    "CANVAS_W", "CANVAS_H",
    # opcodes and segments
    "OP_RECT", "OP_ELLIPSE", "OP_LINE", "OP_POLYLINE", "OP_POLYGON", "OP_PATH",
    "OP_PRIMITIVE", "OP_NAMES",
    "SEG_M", "SEG_L", "SEG_Q", "SEG_C", "SEG_Z", "SEG_NAMES", "SEG_POINT_COUNTS",
    # primitives
    "PRIM_TEXT", "PRIM_TRIANGLE_OUTLINE", "PRIM_TRIANGLE_FILL", "PRIM_ARROW",
    "PRIM_STAR", "PRIM_ARC", "PRIM_YAGI", "PRIM_DISH", "PRIM_RADIO",
    "PRIM_RADIO_WAVES", "PRIM_MOON", "PRIM_DOUBLE_BOX",
    "PRIMITIVE_NAMES", "PRIMITIVE_BY_NAME",
    "TEXT_ALPHABET", "TEXT_INDEX", "MAX_TEXT_LEN", "MOON_CRATER_POINTS",
    "primitive_to_vectors", "primitive_kind", "clean_primitive_text",
    "primitive_anchor",
    # errors
    "MCIError",
    # model
    "PaintStyle", "VectorCommand", "Matrix", "IDENTITY",
    # matrices and colour
    "mat_mul", "mat_translate", "mat_scale", "mat_rotate", "apply_mat",
    "is_axis_aligned", "clamp_int",
    "normalize_hex", "color_to_rgba", "rgba_to_hex",
    # colour quantization
    "rgb565", "alpha4", "from_rgb565", "from_rgb565_a4", "quantize_palette_color",
    # geometry
    "cubic_point", "quad_point", "flatten_path",
    "command_points", "commands_bbox", "transform_command", "translate_command",
    "quantize_command", "validate_command",
]
