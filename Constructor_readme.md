# MCoreIMG Constructor

The authoring half of MCoreIMG: a graphical editor that imports SVG artwork,
combines it with compact radio primitives, previews the transport-visible
result, and exports MeshCore frames.

> **Build:** `2026.08.05-svg-v6.0-MODULAR-SLIMHEADER-10MSG`
> **Feature signature:** `PROTO6|LOCALSPACE|HYBRID|PRIMITIVES|GROUPCOPY|ALPHA|UNDO|10MSG`
> **Depends on:** `MCoreIMG-compression.py`, which in turn loads `MCoreIMG-model.py`

The Constructor implements **no compression**. Every bit of encoding, decoding,
palette construction, record selection, and framing comes from the codec. If
you are changing how images are packed, edit `MCoreIMG-compression.py` instead.

---

## Highlights

- Imports `.svg`, `.svgz`, and editable `.mci.json` source files.
- Adds several SVG files to one canvas without replacing existing artwork.
- Keeps imported SVG geometry in **local coordinate space**, so display scaling
  does not change the transmission size.
- Direct canvas editing: click to select, drag to move, drag the blue corner
  handle to scale, and recolour fills, strokes, and Moon crater colours.
- Includes the legacy **Old Drawing Mode** with compact MCoreIMG primitives.
- Previews the encoded-and-decoded image rather than an idealized source render.
- Exports editable source JSON, MeshCore frame text, and decoded PNG previews.
- Undo for imports and drawing actions, including `Ctrl+Z`.
- Build-integrity guard that verifies the editor and codec are correctly paired.

---

## Requirements

- Python 3.10 or newer
- Tkinter/Tk for the graphical interface
- Pillow for accurate preview rendering and PNG export

### Arch Linux

```bash
sudo pacman -S --needed python tk python-pillow
```

### Python package installation

Tkinter is normally supplied by the operating system. Pillow installs with:

```bash
python -m pip install Pillow
```

---

## Running

```bash
python MCoreIMG-Constructor.py
```

`MCoreIMG-compression.py` and `MCoreIMG-model.py` must be in the same
directory, or the codec path must be given explicitly.

### Command-line options

| Option | Purpose |
|---|---|
| `--version` | Print build, feature signature, and the paired codec and model |
| `--self-test` | Run the regression suite and exit |
| `--compression PATH` | Use a codec from somewhere other than beside this file |

### Verifying a build

```bash
python MCoreIMG-Constructor.py --version
```

Expected output includes the full chain, so a mismatched set is obvious:

```text
2026.08.05-svg-v6.0-MODULAR-SLIMHEADER-10MSG
PROTO6|LOCALSPACE|HYBRID|PRIMITIVES|GROUPCOPY|ALPHA|UNDO|10MSG
protocol=6 messages=10 source_version=6
codec=2026.08.05-compression-v6.0-SLIMHEADER
codec_path=/path/to/MCoreIMG-compression.py
model=2026.08.05-model-v6.0-MODULAR
model_path=/path/to/MCoreIMG-model.py
```

Run the regression suite before distributing a modified build:

```bash
python MCoreIMG-Constructor.py --self-test
```

A successful run ends with:

```text
MCoreIMG Hybrid SVG Constructor self-test: PASS
protocol=6 payload=70 frames=1 primitive=1 group_copies=1
```

---

## Quick start

1. Launch the Constructor.
2. Use **Open SVG / Source** to start a new document, or **Add SVG(s)** to
   append one or more SVG files.
3. Click an imported object on the canvas.
4. Drag it to reposition the complete imported SVG group.
5. Drag the blue lower-right handle to scale the selected group.
6. Use the selected-artwork colour controls to change fill, stroke, or Moon
   crater colours.
7. Open **Old Drawing Mode** to add text, lines, geometric shapes, or compact
   radio-oriented primitives.
8. Watch the status bar for payload characters and the current `n/10` message
   count.
9. Use **Preview Frames** to inspect or copy the final MeshCore messages.
10. Export `.mci`, editable `.mci.json`, or a decoded PNG preview.

When an import or drawing action pushes the image over the ten-message limit,
press **Undo** or `Ctrl+Z` to restore the previous document.

---

## Editing model

### Open versus append

**Open SVG / Source** replaces the document. **Add SVG(s)** appends, so several
files can share one canvas.

### Editor groups

Imported commands receive an `editor_group` identifier so an entire SVG can be
selected, moved, scaled, duplicated, or deleted as one object.

Grouping is editor-only metadata. It is never transmitted and costs no direct
bits. What it *does* do is tell the encoder which contiguous runs are candidates
for local-space groups, so keeping an imported group contiguous in draw order
gives the encoder its best opportunity to reuse geometry.

### Moving and resizing

Dragging a selected object moves the complete group. The blue lower-right
handle scales it. Both operations change only the group's placement transform,
not its encoded geometry.

### Document transform

The document carries one non-destructive scale and offset, convenient for Fit
and Center operations. `bake_transform()` folds it into the commands.

---

## Old Drawing Mode

The legacy drawing window supports normal vector shapes and compact MCoreIMG
primitives:

Text · Line · Rectangle · Ellipse · Filled and outlined triangles · Arrow ·
Star · Arc · Radio waves · Yagi antenna · Dish antenna · Radio · Moon ·
DoubleBox

Some shapes take one canvas click. Line, Rectangle, and DoubleBox take two.
Exact coordinates and shape parameters can also be entered manually.

**The interface does not decide the transmitted representation.** During
encoding, the codec measures the contextual bit cost of the compact primitive
record against its generic vector expansion, and uses the primitive only when it
is strictly smaller. The same primitive can win in one image and lose in
another.

---

## Why SVG scale no longer changes the message count

Earlier branches flattened displayed size into every path coordinate. Smaller
artwork produced smaller coordinate deltas and therefore fewer bits, even though
the SVG contained identical geometry.

Protocol v5 separates:

```text
local SVG geometry
        +
canvas placement and scale
```

Geometry is encoded in a stable local coordinate box. Moving or resizing changes
only a fixed-width transform record. An imported SVG has approximately the same
encoded bit count at 100%, 50%, or 10% display scale.

Minor Base91 character-count differences can still occur from byte padding, but
geometry cost and frame count stay stable.

---

## SVG support

The importer handles a practical, deliberately limited subset intended for
compact radio transmission.

**Supported or normalized:**

- `<path>`, `<line>`, `<polyline>`, `<polygon>`
- `<rect>`, including rounded-corner conversion
- `<circle>` and `<ellipse>`
- Nested groups and basic transforms
- Solid fill and stroke colours, stroke width
- `opacity`, `fill-opacity`, `stroke-opacity`
- Inherited styles
- Basic CSS tag, class, and ID selectors
- `<use>` references
- Path operations: move, line, quadratic Bézier, cubic Bézier, close-path, and
  normalized SVG shorthand

SVG arcs and unsupported geometry may be approximated during import. The
**Import Notes** panel reports skipped or approximated features.

**Not preserved by this branch:** animation, scripting, filters, masks, complex
clipping, embedded raster images, advanced text layout, gradients, and patterns.

---

## Colour and alpha

Palette entries use RGB565 colour with 4-bit alpha, giving 16 opacity levels.

The same Pillow source-over compositor drives the GUI preview and PNG export.
Preview rendering is performed from the **transport-decoded commands** whenever
the image fits, so the screen reflects palette quantization, alpha quantization,
coordinate precision, geometry simplification, and protocol reconstruction
exactly as a receiver would see them.

Note that protocol 6 removes vertices that cannot survive integer rounding, so
a dense imported outline will show slightly fewer points in the preview than the
source SVG contained. The deviation is under half a pixel by construction.

---

## Message envelope

The MeshCore profile is fixed at:

- 10 messages maximum
- 150 characters per message
- 142 payload characters per framed message
- 1,420 Base91 payload characters maximum

Each frame includes protocol and image identification plus a single part
descriptor covering the frame index, the final-frame flag, and the coding mode.
Protocol 6 removed the per-frame CRC (MeshCore already guarantees message
integrity) and the length field (chunking fills every frame but the last). The
reassembled stream still carries a CRC-32.

The Constructor rejects malformed, mixed, incomplete, corrupt, and over-limit
frame sets.

---

## File types

### SVG and SVGZ

Source artwork, imported and normalized into editable MCoreIMG commands.

### `.mci.json`

Editable Constructor source. Preserves command order, styles, primitive
parameters, document transforms, warnings, and editor grouping.

This is not the radio transport format and may evolve independently of the
protocol version.

### `.mci`

A text file containing one complete MeshCore frame per line.

### PNG

A raster preview of the transport-visible decoded image. Useful for visual
verification; not transmitted.

---

## Module structure

| Section | Contents |
|---|---|
| Codec import | Loads the codec by path and re-exports the model vocabulary |
| Editor constants | Background, margins, source format, build strings, CSS colour fallbacks |
| Editable document | `VectorDocument` and its transform |
| SVG value parsing | Transforms, colours, lengths |
| SVG path parsing | Tokenizer, arc approximation, segment normalization |
| SVG importer | Groups, styles, CSS, `<use>`, shape conversion |
| Rendering | Tk canvas and Pillow renderers |
| Source file helpers | `save_source`, `load_source` |
| Multi-SVG composition | Editor grouping and append logic |
| GUI | Three cooperating application classes |
| Self-test and entry point | Regression suite, integrity guard, CLI |

### GUI classes

| Class | Adds |
|---|---|
| `_DocumentAppBase` | Document lifecycle, layer list, file open/save, preview and statistics |
| `_DrawingAppBase` | Multi-SVG import, editor grouping, Old Drawing Mode |
| `ConstructorApp` | Direct canvas editing — select, drag, scale, recolour — plus undo |

These were previously three top-level definitions all named `ConstructorApp`,
chained by alias assignments and resolved only by Python's last-definition-wins
rule. They now have distinct names and inherit explicitly.

Collapsing them into a single class remains worthwhile but requires interactive
GUI testing, so it is deliberately left as a separate change.

---

## Build integrity

`verify_build_integrity()` runs before the GUI starts. It checks that the codec
and editor are correctly paired:

- Codec reports protocol 6, a 10-message envelope, and 150-character messages
- All four record types are present
- `encode_image` and `decode_frames` are callable
- Primitive opcode, primitive expansion, and alpha quantization are available
- The editor provides undo, multi-SVG import, and drawing mode

Failures name both build strings, because the realistic packaging mistake is
shipping this file beside a stale or mismatched codec.

---

## Development rules

Before committing editor changes:

1. Run `--version` and verify protocol 6, the expected feature signature, and
   matching codec and model builds.
2. Run `--self-test`.
3. Import a normal SVG and a multi-SVG document.
4. Verify dragging and scaling operate on the intended complete group.
5. Verify the payload stays stable when only display scale changes.
6. Test a translucent overlap image and compare preview against exported PNG.
7. Draw at least one compact primitive and check the primitive/vector
   statistics.
8. Duplicate an imported SVG and confirm local-definition reuse.
9. Import an oversized SVG and confirm Undo restores the previous document.
10. Decode the exported frames with the paired Reconstructor.

**Do not add compression logic here.** If a change requires new bits on the
wire, it belongs in the codec and is a protocol change.
