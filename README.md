# MCoreIMG

**MCoreIMG is an experimental vector-image editor and transport codec for fitting practical SVG artwork and compact hand-drawn graphics into severely constrained MeshCore text messages.**

Instead of transmitting a PNG or the original SVG document, MCoreIMG sends a compact drawing program containing a small palette, vector geometry, reusable primitives, predictive state, repeated-object references, and local-space SVG definitions. A matching reconstructor validates the frames, decodes the drawing program, and renders the image locally.

> [!WARNING]
> [!WARNING]
> MCoreIMG is currently pre-alpha. The SVG/hybrid implementation has been merged into the main pre-alpha codebase. Protocol, source-format, and editor behavior may still change, so keep compatible constructor and reconstructor builds together.

## Current Pre-Alpha Implementation

| Item | Current implementation |
|---|---|
| Constructor generation | v5.1 local-space hybrid |
| Compressed protocol | Protocol 5 |
| Editable source version | Version 5 |
| Feature signature | `PROTO5|LOCALSPACE|HYBRID|PRIMITIVES|GROUPCOPY|ALPHA|UNDO|10MSG` |
| Canvas | 720 × 480 pixels |
| Background | White |
| Transport limit | 10 messages |
| Message limit | 150 ASCII characters |
| Header | 15 characters |
| Payload per frame | Up to 135 Base91 characters |
| Total text payload | Up to 1,350 Base91 characters |
| Palette | Up to 32 RGB565+A4 entries |
| Output-command limit | 2,048 |
| Integrity | Per-frame CRC-16 and stream CRC-32 |

Current development filenames:

```text
MCoreIMG-SVG-Constructor-v5.1-LOCALSPACE-HYBRID-VERIFIED.py
MCoreIMG-Reconstructor-v5.1.py
```

Canonical repository filenames may be:

```text
MCoreIMG-SVG-Constructor.py
MCoreIMG-Reconstructor.py
```


## Pre-Alpha Merge Status

The former SVG integration branch has been merged into the main pre-alpha MCoreIMG codebase. SVG import, local-space definitions, hybrid primitive comparison, alpha support, group copying, direct canvas manipulation, undo, and the ten-message transport profile are now part of the unified implementation rather than maintained as a separate branch.

Documentation in this file therefore describes the complete current pre-alpha implementation.

## Implemented Features

### SVG and Source Import

- Opens `.svg`, `.svgz`, `.mci.json`, and compatible `.json` files
- Appends multiple SVG or MCoreIMG source files without replacing existing artwork
- Fits newly imported SVG artwork inside the 720 × 480 canvas
- Preserves imported artwork as editor groups
- Supports repeated imports and translated copies
- Records warnings for unsupported or approximated SVG features

### Direct Canvas Editing

- Select imported groups or individual layers
- Click and drag artwork across the canvas
- Resize selected artwork with a corner handle
- Edit fill and stroke colors
- Edit Moon crater color independently
- Preserve existing alpha when changing RGB through a color chooser
- Fit, center, and bake document transforms
- Show live command, payload, frame, and optimizer statistics

### Old Drawing Mode

The earlier MCoreIMG/EMEIMG-style drawing workflow remains available alongside SVG import.

It supports:

- Canvas-click placement
- Direct coordinate entry
- Shape-specific fields
- Main and Moon-crater color choosers
- Alpha-aware colors
- Compact legacy-style primitives
- Automatic comparison against equivalent generic vectors

### Undo

- Undo the latest SVG import
- Undo the latest manual drawing action
- Undo direct manipulation changes
- `Ctrl+Z` support
- Up to 20 snapshots to prevent unbounded memory use with large SVGs

### Export and Reconstruction

- Save editable `.mci.json`
- Export MeshCore-ready `.mci`
- Preview and copy every frame
- Export a locally rendered PNG
- Reconstruct transport frames to PNG
- Dump best-effort editable source
- List decoded commands and scene content

## Requirements

Python 3.10 or newer is recommended.

### Arch Linux

```bash
sudo pacman -Syu python tk python-pillow
```

### Other Python Environments

```bash
python -m pip install pillow
```

Tkinter may need to be installed through the operating system package manager.

Pillow is required for PNG export, RGBA compositing, authoritative rendering, and the complete self-test suite.

## Quick Start

### Constructor

```bash
python MCoreIMG-SVG-Constructor.py
```

Versioned build:

```bash
python MCoreIMG-SVG-Constructor-v5.1-LOCALSPACE-HYBRID-VERIFIED.py
```

Show build information:

```bash
python MCoreIMG-SVG-Constructor.py --version
```

Run the constructor self-test:

```bash
python MCoreIMG-SVG-Constructor.py --self-test
```

### Reconstructor

```bash
python MCoreIMG-Reconstructor.py image.mci
```

Choose the output:

```bash
python MCoreIMG-Reconstructor.py image.mci --output reconstructed.png
```

Use an exact constructor core:

```bash
python MCoreIMG-Reconstructor.py image.mci \
  --core ./MCoreIMG-SVG-Constructor.py
```

Versioned files:

```bash
python MCoreIMG-Reconstructor-v5.1.py image.mci \
  --core ./MCoreIMG-SVG-Constructor-v5.1-LOCALSPACE-HYBRID-VERIFIED.py
```

When no input is supplied, the reconstructor opens a file chooser.

## Recommended Workflow

1. Open an SVG or editable source.
2. Append additional SVGs or source files.
3. Click an imported object to select its group.
4. Drag it to move it.
5. Drag its handle to resize it.
6. Adjust fill, stroke, alpha, or Moon crater color.
7. Add manual shapes through Old Drawing Mode.
8. Watch the live frame and optimizer statistics.
9. Undo an import or drawing action when necessary.
10. Save editable work as `.mci.json`.
11. Export transport as `.mci`.
12. Preview or copy the generated frames.
13. Reconstruct the `.mci` as a final round-trip check.

## Editing Model

### Open Versus Append

**Open SVG / Source** replaces the document.

Appending adds one or more source files. The placement dialog supports an initial X/Y offset and a per-file X/Y step, allowing repeated files to be placed automatically.

### Editor Groups

A newly imported SVG receives an editor-group identifier. Clicking any member selects and moves the full imported object.

Editor groups:

- Survive editable JSON saves
- Are used by the GUI
- Do not directly consume transport space
- Help recognize local-space definitions and repeated copies

### Moving and Resizing

Dragging translates a selected layer or group. The resize handle scales the selection around its bounding-box center. Extreme resize factors are clamped to avoid accidental zero-sized or unreasonable geometry.

The constructor immediately recalculates payload size, frame count, repeat usage, and canvas bounds.

### Document Transform

The document has non-destructive scale, X offset, and Y offset.

Available operations:

- **Fit** — fit artwork inside the canvas
- **Center** — center transformed bounds
- **Bake Transform** — permanently apply scale and offset

Direct editing bakes a pending document transform when necessary so pointer position matches stored geometry.

### Duplicate All

The constructor can duplicate every layer by a selected X/Y translation. The encoder then compares independent commands, a translated group-copy record, and reusable local definitions.

## Live Statistics

A status line may resemble:

```text
24 src / 16 tx | 472 chars | 4/10 frames fits | P:3 V:1 G:2 L:255
```

| Field | Meaning |
|---|---|
| `src` | Visible source commands before optimization |
| `tx` | Declared reconstructed commands |
| `chars` | Complete Base91 payload length |
| `frames` | Frames used out of ten |
| `P` | Compact primitives selected |
| `V` | Primitive requests vectorized because vectors were equal or smaller |
| `G` | Translated group references |
| `L` | Local SVG coordinate extent |

The GUI also warns about canvas overflow, encoding failures, and images that exceed ten frames.

## Generic Vector Commands

| Opcode | Command | Geometry |
|---:|---|---|
| `0` | Rectangle | X, Y, width, height |
| `1` | Ellipse | Center and X/Y radii |
| `2` | Line | Two endpoints |
| `3` | Polyline | Open point list |
| `4` | Polygon | Closed point list |
| `5` | Path | Move, line, quadratic, cubic, close |
| `6` | Primitive | Compact manual/macro shape |

SVG circles become ellipses. Rounded rectangles become paths.

## Path Segments

| Segment | Meaning |
|---|---|
| `M` | MoveTo |
| `L` | LineTo |
| `Q` | Quadratic Bezier |
| `C` | Cubic Bezier |
| `Z` | ClosePath |

During import:

- `H` and `V` are normalized
- Smooth cubic `S` is expanded
- Smooth quadratic `T` is expanded
- Relative and absolute coordinates are resolved
- SVG arcs are approximated with line nodes
- Beziers remain Q/C commands but are flattened for raster rendering

## SVG Element Support

| SVG element | Behavior |
|---|---|
| `<svg>` | Root/container and viewport |
| `<g>` | Nested container |
| `<a>` | Container |
| `<switch>` | Container |
| `<rect>` | Rectangle or rounded path |
| `<circle>` | Ellipse |
| `<ellipse>` | Ellipse |
| `<line>` | Line |
| `<polyline>` | Polyline |
| `<polygon>` | Polygon |
| `<path>` | Normalized path |
| `<use>` | Resolves referenced ID |
| `<symbol>` | Usable through `<use>` |

Unresolved and recursive `<use>` references produce warnings instead of infinite recursion.

Explicitly unsupported content includes SVG text, raster images, and `foreignObject`.

## SVG Geometry

### Viewport

The importer uses:

1. A valid `viewBox`
2. Explicit width and height
3. A 720 × 480 fallback viewport

The viewport is centered and fitted with an eight-pixel margin.

### Length Units

Parsed units include unitless values, pixels, points, picas, inches, centimeters, millimeters, quarter-millimeters, and percentages.

### Transforms

Supported affine transforms:

- `matrix(...)`
- `translate(...)`
- `scale(...)`
- `rotate(...)`
- Rotation around a center
- `skewX(...)`
- `skewY(...)`

Nested transforms are combined before transport encoding.

## SVG Style Support

The importer supports a practical flat-paint subset:

- Fill
- Stroke
- Stroke width
- `nonzero` and `evenodd` fill rules
- Presentation attributes
- Inline style declarations
- Inherited style
- Basic stylesheet rules
- Element, fill, and stroke opacity
- Hex and common named colors
- Transparent colors

Open subpaths follow SVG fill semantics: filling may implicitly close a subpath even when its stroke remains open.

Not represented:

- Gradients
- Patterns
- Filters
- Masks
- Clip paths
- Full browser CSS behavior

Paint references such as `url(#gradient)` cannot become flat palette entries.

## Alpha Support

Protocol 5 transports palette entries as **RGB565+A4**:

- 16-bit RGB565 color
- 4-bit alpha
- 16 opacity levels

SVG opacity is combined before quantization. Transparency survives parsing, source save, palette creation, bitstream encoding, Base91 framing, decoding, source-over rendering, and PNG export.

Quantized output can differ slightly from the original SVG.

The constructor and reconstructor share the constructor's Pillow renderer so preview, export, and reconstruction remain synchronized.

## Old Drawing Primitives

| Primitive | Adjustable data |
|---|---|
| Text | Position and text |
| Triangle Outline | Anchor, orientation, scale |
| Triangle Fill | Anchor, orientation, scale |
| Arrow | Anchor, orientation, scale |
| Star | Center, radius, scale |
| SemiCircle / Arc | Center, radius, scale, start, sweep |
| Yagi Antenna | Anchor, orientation, scale |
| Dish Antenna | Anchor, orientation, scale |
| Radio Transceiver | Anchor, orientation, scale |
| Radio Waves | Center, radius, scale, start, sweep |
| Moon | Center, scale, body and crater colors |
| DoubleBox | Opposite corners and divider percentage |

Current limits include orientation `0..3`, scale `1..64`, star/arc/radio-wave radius `1..128`, angles `0..360`, and text up to 63 characters.

Compact text alphabet:

```text
 0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ-+./?:,()[]#_=@!&%
```

## Primitive-versus-Vector Optimizer

The encoder does not automatically prefer primitives.

For each supported manual shape it compares:

1. The compact primitive record
2. Its equivalent generic vector expansion

The primitive is used only when its actual encoded form is smaller. Existing opcode-local vector state can occasionally make ordinary vectors cheaper, so a shape such as DoubleBox may be sent as a rectangle and line.

Self-tests verify pixel parity between primitive and vector rendering.

## Compression Architecture

### Draw Order

Source order is draw order. No extra layer index is transmitted.

### Palette

Only used colors are sent. The shared per-image palette has up to 32 RGB565+A4 entries.

### Opcode-Local Style State

Each opcode keeps its own fill, stroke, width, and fill-rule history. Alternating between object types does not discard useful state.

### Opcode-Local Point State

Coordinates are predicted within relevant local contexts. Nearby points can use small deltas instead of repeated absolutes.

### Exp-Golomb and Rice Coding

Unsigned Exp-Golomb handles small counts and dimensions. Signed translations use ZigZag ordering and Golomb-Rice coding, making small movement cheap.

### Single Translated Repeat

A command can reference the latest compatible command of the same opcode and send only X/Y translation. It need not be adjacent.

### Translated Group Copy

A repeated contiguous group can send source length, history distance, and X/Y translation. Adjacent and nonadjacent copies are supported.

### Protocol-5 Local-Space SVG Groups

Imported SVG groups are encoded once in stable local coordinates. Displayed position and size are carried separately in a fixed-width transform box.

This means shrinking an SVG on the canvas no longer makes its path coordinates cheaper. A complex imported SVG costs approximately the same at 10% and 100% display size unless other stream context changes.

To reduce payload, simplify the SVG definition itself rather than merely shrinking it.

### Reused Definitions

An identical later SVG group can reference an earlier local definition and send only its placement transform.

### Base91

The packed stream is converted to a custom printable Base91 alphabet that excludes:

```text
"  '  \
```

This avoids common quoting and escaping problems.

## Protocol-5 Record Types

| Record | Purpose |
|---:|---|
| `0` | Normal vector or primitive |
| `1` | Single translated repeat |
| `2` | Translated command-group repeat |
| `3` | Local-space SVG group or definition reference |

The decoder expands records back into the declared output command sequence.

## MeshCore Transport

Each frame is:

```text
[15-character header][0 to 135 Base91 payload characters]
```

An image uses one through ten frames.

### Header

```text
MCI V III P T LL CCC F
```

Spaces are explanatory only.

| Field | Width | Meaning |
|---|---:|---|
| `MCI` | 3 | Magic |
| `V` | 1 | Base62 protocol |
| `III` | 3 | Image ID |
| `P` | 1 | Zero-based part |
| `T` | 1 | Total parts |
| `LL` | 2 | Payload length |
| `CCC` | 3 | Frame payload CRC |
| `F` | 1 | Reserved flags |

Current flags are zero.

The image ID is derived from the encoded stream CRC and groups related frames; it is not a cryptographic signature.

Each frame has a CRC-16. The assembled binary stream has a CRC-32. Acknowledgement, retransmission, and pacing remain application responsibilities.

## File Formats

### `.svg` / `.svgz`

Original SVG input.

### `.mci.json`

Editable source containing:

- Format, source version, and protocol
- Canvas dimensions
- Source name
- Document transform
- Ordered commands
- Style and geometry
- Labels and visibility
- Editor grouping
- Import warnings

Example:

```json
{
  "format": "MCoreIMG-SVG-source",
  "version": 5,
  "protocol_version": 5,
  "canvas": {"width": 720, "height": 480},
  "source_name": "example.svg",
  "transform": {
    "scale": 1.0,
    "offset_x": 0.0,
    "offset_y": 0.0
  },
  "commands": [
    {
      "opcode": 0,
      "type": "Rectangle",
      "style": {
        "fill": "#3366CC",
        "stroke": null,
        "stroke_width": 0,
        "fill_rule": "nonzero"
      },
      "geom": {"x": 40, "y": 40, "w": 160, "h": 80},
      "label": "background",
      "visible": true,
      "editor_group": 1
    }
  ],
  "warnings": []
}
```

### `.mci`

One complete transport frame per line. Editing payload characters normally invalidates the CRC.

### `.png`

Raster preview or reconstructed output.

## PNG Export

When the image fits:

```text
source
  -> protocol quantization
  -> encode frames
  -> decode frames
  -> authoritative renderer
  -> PNG
```

The PNG therefore represents receiver-visible output.

An oversized image can still be locally previewed after quantization, but its frame set is not a valid current-profile transmission.

## Reconstructor Design

The v5.1 reconstructor is a compatibility adapter. It loads a matching constructor as the authority for decoding, primitive expansion, local-space groups, repeated groups, palette/alpha behavior, and rendering.

This prevents a second decoder implementation from drifting away from the sender.

### Core Selection

Without `--core`, it searches its own directory and the current working directory for canonical and versioned constructor filenames. Candidates are ranked by protocol, current feature signature, constructor version, filename preference, and modification ordering.

With `--core`, the supplied file is an explicit override.

### Protocol Compatibility

Transport protocol is detected from the `MCI` frame header. The adapter prefers protocol 5 and can use older protocol-2, protocol-3, and protocol-4 constructor cores when they expose compatible APIs.

A protocol number alone does not guarantee current v5.1 behavior, so the feature signature is also checked.

## Reconstructor Inputs

- `.mci`
- Plain text or logs containing MCI frames
- `.mci.json`
- Compatible `.json`
- `.svg`
- `.svgz`

## Reconstructor Options

```text
input                    Optional input file
-o, --output PATH        Output PNG
--core PATH              Exact constructor core
--protocol N             Force protocol
--dump-json               Write best-effort source JSON
--list-commands           Print decoded content
--no-open                 Do not open the PNG
--self-test               Run round-trip tests
--show-core               Show selected core and APIs
```

Examples:

```bash
python MCoreIMG-Reconstructor.py --show-core
```

```bash
python MCoreIMG-Reconstructor.py image.mci --list-commands
```

```bash
python MCoreIMG-Reconstructor.py image.mci --dump-json --no-open
```

```bash
python MCoreIMG-Reconstructor.py --protocol 5 --self-test
```

The reconstructor reports the selected core, build, feature signature, protocol, envelope, decoder/renderer APIs, decoded representation, PNG mode and dimensions, and output path.

## Self-Test Coverage

Constructor and reconstructor tests cover:

- Protocol-5 encode/decode round trips
- Alpha import and compositing
- RGB565+A4 quantization
- Ten-message envelope and exact 150-character frames
- Corruption rejection
- SVG fitting
- Open-subpath fill behavior
- Compact primitive selection
- Primitive vector fallback
- Primitive/vector pixel parity
- Single translated repeats
- Multi-command group copies
- Multiple appended SVGs
- Source JSON round trips
- Editor-group persistence without payload changes
- Scale-independent local-space SVG encoding
- Reused local definitions
- Constructor/reconstructor render parity

## Development Checks

Syntax:

```bash
python -m py_compile \
  MCoreIMG-SVG-Constructor.py \
  MCoreIMG-Reconstructor.py
```

Tests:

```bash
python MCoreIMG-SVG-Constructor.py --self-test
python MCoreIMG-Reconstructor.py --protocol 5 --self-test
python MCoreIMG-Reconstructor.py --show-core
```

## Known Limitations

- Practical SVG subset, not the complete SVG specification
- SVG `<text>` skipped; primitive text is separate
- No embedded raster images or `foreignObject`
- No gradients, patterns, filters, masks, or clip paths
- No complete browser CSS cascade
- SVG arcs approximated with line nodes
- Beziers flattened for raster rendering
- RGB565 color and four-bit alpha quantization
- Maximum 32 palette entries
- Maximum 2,048 output commands
- Fixed 720 × 480 white canvas
- Maximum ten 150-character messages
- Oversized images cannot be exported as valid current-profile transport
- Original SVG IDs, CSS structure, and authoring metadata are not fully transmitted
- Dumped source cannot recover information absent from transport
- Earlier experimental protocol-5 cores may not match the current feature set
- No built-in MeshCore send/receive client

## Compression Advice

- Simplify paths before import.
- Remove invisible and off-canvas geometry.
- Reduce unnecessary curve nodes.
- Reuse colors and alpha values.
- Duplicate existing objects instead of rebuilding them.
- Reuse identical SVG groups.
- Use compact radio, antenna, star, arc, Moon, and similar primitives.
- Let the optimizer choose primitive or vectors.
- Replace gradients with a few flat colors.
- Keep text short.
- Watch `src`, `tx`, `P`, `V`, `G`, payload, and frame statistics.

Displayed scale is no longer a compression control. Simplify the local definition to reduce size.

## Security

CRC values detect accidental corruption. They do not provide encryption, authentication, sender verification, or intentional-tamper protection. Those properties depend on the surrounding MeshCore system.

## Development Rules

1. Update constructor and reconstructor compatibility together.
2. Increase protocol version when old streams would decode differently.
3. Increase source version when editable JSON meaning changes.
4. Update the feature signature for material protocol-5 behavior changes.
5. Keep constructor-authoritative rendering.
6. Add a self-test for every fixed regression.
7. Test exact frame lengths and CRC failures.
8. Test alpha round trips.
9. Test local-space scale invariance.
10. Test repeated definitions and group copies.
11. Test primitive/vector render parity.
12. Update this README.

## Pre-Commit Checklist

- [ ] Constructor reports protocol 5 and source version 5.
- [ ] Feature signature includes local space, hybrid, primitives, group copy, alpha, undo, and ten-message support.
- [ ] Both self-tests pass.
- [ ] `py_compile` succeeds.
- [ ] Representative SVGs import and fit.
- [ ] Multiple SVGs append rather than replace.
- [ ] Groups drag and resize correctly.
- [ ] Resizing an SVG does not change payload solely because of display scale.
- [ ] Repeated SVGs use definition or group references.
- [ ] Old Drawing Mode works.
- [ ] Color choosers preserve alpha.
- [ ] Undo reverses imports and drawing actions.
- [ ] Primitive and vector choices both occur in tests.
- [ ] Preview, PNG export, and reconstruction match.
- [ ] Alpha survives SVG-to-MCI-to-PNG.
- [ ] Corrupted frames are rejected.
- [ ] Ten-frame output remains within 150 characters per frame.
- [ ] Oversized images are clearly rejected for transport.
- [ ] Constructor and reconstructor are committed together.

## Project Direction

The current pre-alpha release is a unified hybrid, stateful vector transport combining:

- SVG import
- Direct graphical editing
- Compact symbolic primitives
- Generic paths and curves
- Transported transparency
- Repeated-object references
- Scale-independent local definitions
- Integrity-checked frames
- A strict ten-message MeshCore envelope

The goal is not arbitrary image transfer. The goal is to make a useful class of small vector illustrations practical over a channel where every character matters.
