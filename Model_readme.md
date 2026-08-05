# MCoreIMG Model

The shared vocabulary every other MCoreIMG module speaks.

`MCoreIMG-model.py` defines what a drawing command *is*: the opcodes, the
geometry schema, the paint model, the colour quantization, and the compact
primitives. It is the bottom of the dependency chain and imports nothing but
the Python standard library.

> **Build:** `2026.08.05-model-v6.0-MODULAR`
> **Depends on:** `copy`, `math`, `re`, `dataclasses`, `typing`

---

## Why this module exists

The codec, the editor, and the receiver all need to agree on the same
definitions. If any of them carried its own copy of "what a rectangle is" or
"how a colour rounds to RGB565", they would drift.

Everything whose behaviour the transport depends on lives here — including the
rounding rules. `quantize_command` is a model function rather than a codec
function because the encoder, the preview renderer, and the receiver must all
round identically or the preview stops matching the transmission.

What is deliberately **not** here: bit writing, record selection, palette
assembly, framing, SVG parsing, rendering, and anything with a user interface.

---

## Contents

### Canvas and vocabulary

Canvas bounds are in the model because command validation and coordinate
clamping depend on them.

```python
CANVAS_W = 720
CANVAS_H = 480
```

### Opcodes

| Constant | Value | Geometry keys |
|---|---|---|
| `OP_RECT` | 0 | `x`, `y`, `w`, `h` |
| `OP_ELLIPSE` | 1 | `cx`, `cy`, `rx`, `ry` |
| `OP_LINE` | 2 | `p1`, `p2` |
| `OP_POLYLINE` | 3 | `points` (≥ 2) |
| `OP_POLYGON` | 4 | `points` (≥ 3) |
| `OP_PATH` | 5 | `segments` |
| `OP_PRIMITIVE` | 6 | `kind` plus per-kind fields |

`OP_NAMES` maps each opcode to a display name.

### Path segments

| Constant | Value | Points | SVG |
|---|---|---|---|
| `SEG_M` | 0 | 1 | MoveTo |
| `SEG_L` | 1 | 1 | LineTo |
| `SEG_Q` | 2 | 2 | Quadratic Bézier |
| `SEG_C` | 3 | 3 | Cubic Bézier |
| `SEG_Z` | 4 | 0 | ClosePath |

`SEG_POINT_COUNTS` gives the point count per segment type. A path must begin
with `SEG_M`.

Segments are dictionaries with an `op` key and a `points` list:

```python
{"op": SEG_C, "points": [(660, 280), (680, 340), (700, 300)]}
```

### Compact primitives

Twelve radio-oriented shapes that cost far less than their vector expansion
when the encoder decides they are cheaper.

| Constant | Value | Name |
|---|---|---|
| `PRIM_TEXT` | 0 | Text |
| `PRIM_TRIANGLE_OUTLINE` | 1 | Triangle Outline |
| `PRIM_TRIANGLE_FILL` | 2 | Triangle Fill |
| `PRIM_ARROW` | 3 | Arrow |
| `PRIM_STAR` | 4 | Star |
| `PRIM_ARC` | 5 | SemiCircle / Arc |
| `PRIM_YAGI` | 6 | Yagi Antenna |
| `PRIM_DISH` | 7 | Dish Antenna |
| `PRIM_RADIO` | 8 | Radio Transceiver |
| `PRIM_RADIO_WAVES` | 9 | Radio Waves |
| `PRIM_MOON` | 10 | Moon |
| `PRIM_DOUBLE_BOX` | 11 | DoubleBox |

`PRIMITIVE_NAMES` and `PRIMITIVE_BY_NAME` map between kinds and labels.

`TEXT_ALPHABET` is the 6-bit character set for `PRIM_TEXT` (55 characters,
capped at 64 by assertion). `MAX_TEXT_LEN` is 63.

`MOON_CRATER_POINTS` is a fixed crater layout as `(x fraction, y fraction,
radius)`. Both ends reproduce craters from this table rather than transmitting
them.

---

## Data model

### `PaintStyle`

```python
@dataclass
class PaintStyle:
    fill: Optional[str] = "#000000"
    stroke: Optional[str] = None
    stroke_width: float = 1.0
    fill_rule: str = "nonzero"
```

`fill` and `stroke` are normalized `#RRGGBB` or `#RRGGBBAA` strings, or `None`.
`fill_rule` is retained because compound SVG paths can need even-odd filling to
preserve holes. A command must have a fill or a stroke.

`normalized()` canonicalizes colours and clamps stroke width.
`to_json()` / `from_json()` handle editable-source serialization.

### `VectorCommand`

```python
@dataclass
class VectorCommand:
    opcode: int
    style: PaintStyle
    geom: Dict[str, Any]
    label: str = ""
    visible: bool = True
    editor_group: Optional[int] = None
```

`geom` is opcode-specific, per the tables above.

`editor_group` is the one piece of purely editorial metadata. It lets a GUI move
an imported SVG as a single object, and it is the hint the encoder uses to
propose a local-space group. It is never transmitted directly and costs no bits.

`clone()` deep-copies. `to_json()` / `from_json()` serialize, with validation on
the way in.

---

## Geometry operations

| Function | Purpose |
|---|---|
| `command_points(cmd)` | Every coordinate the command occupies, curves flattened |
| `commands_bbox(cmds)` | Bounding box across a sequence |
| `transform_command(cmd, m)` | Apply an affine matrix |
| `translate_command(cmd, dx, dy)` | Convenience translation |
| `quantize_command(cmd)` | Round to integer transport precision and clamp to canvas |
| `validate_command(cmd)` | Raise `MCIError` on malformed geometry or missing paint |

Generic behaviour lives in `_generic_*` functions; the public wrappers dispatch
so primitives can apply their own anchor and expansion rules while everything
else falls through unchanged.

### Curve flattening

`cubic_point`, `quad_point`, and `flatten_path` convert Bézier segments into
polylines. `flatten_path` is here rather than in the renderer because
`command_points` needs it to compute path bounding boxes, which drive
local-space normalization — making it a transport dependency.

### Affine matrices

`Matrix` is a 6-tuple `(a, b, c, d, e, f)`. `IDENTITY` is the identity matrix.

`mat_mul`, `mat_translate`, `mat_scale`, `mat_rotate`, `apply_mat`,
`is_axis_aligned`, `clamp_int`.

---

## Colour

### Normalization

`normalize_hex`, `color_to_rgba`, `rgba_to_hex` convert between string and
tuple forms. Alpha is carried in the string as a trailing `AA` pair when it is
not fully opaque.

### Quantization

This is where the model and the wire format touch.

| Function | Purpose |
|---|---|
| `rgb565(color)` | Pack to 16-bit RGB565 |
| `alpha4(color)` | Quantize alpha to 4 bits (16 levels) |
| `from_rgb565(v)` | Unpack to `#RRGGBB` |
| `from_rgb565_a4(v, a4)` | Unpack to `#RRGGBB` or `#RRGGBBAA` |
| `quantize_palette_color(c)` | Round-trip a colour through RGB565 + A4 |

Assembling the ordered palette and enforcing the 32-entry limit are codec
concerns and live in `MCoreIMG-compression.py`.

---

## Primitive expansion

`primitive_to_vectors(cmd)` expands a compact primitive into the generic vector
commands that draw it, returning `None` when the primitive has no expansion.

This function has two callers with different motives. The renderer uses it to
draw. The encoder uses it to *price* the primitive against its expansion in
context, choosing the primitive only when it is strictly smaller.

Support functions: `primitive_kind`, `primitive_anchor`, `clean_primitive_text`.

---

## Errors

`MCIError` is the base exception for invalid model, codec, or stream data. It
subclasses `ValueError`.

Transport-specific failures raise `FrameError`, defined in the compression
module.

---

## Using the model directly

The filename is hyphenated and therefore not importable by name. Load it by
path:

```python
import importlib.util, sys

spec = importlib.util.spec_from_file_location(
    "mcoreimg_model", "MCoreIMG-model.py")
model = importlib.util.module_from_spec(spec)
sys.modules["mcoreimg_model"] = spec.name and model
spec.loader.exec_module(model)

cmd = model.VectorCommand(
    model.OP_RECT,
    model.PaintStyle("#FF0000", None, 1.0),
    {"x": 10, "y": 10, "w": 100, "h": 50},
)
model.validate_command(cmd)
```

In practice you rarely need this: importing `MCoreIMG-compression.py`
re-exports the entire model vocabulary.

---

## Maintenance notes

- The module has no shadowed definitions. Every name is defined once.
- `__all__` marks the public surface. Anything not listed is an implementation
  detail.
- Adding an opcode means updating `OP_NAMES`, the geometry schema in
  `validate_command`, `command_points`, `transform_command`, and
  `quantize_command` — then teaching the codec to carry it. That second half is
  a protocol change.
- Changing a rounding rule changes emitted bits. Treat it as a protocol change.
