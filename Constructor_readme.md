# MCoreIMG SVG Constructor

A graphical editor and reference encoder for building compact vector images for transmission over MeshCore.

The Constructor imports and combines SVG artwork, restores the legacy MCoreIMG drawing primitives, previews the exact transport-visible result, and packages an image into no more than **ten 150-character MeshCore messages**.

> **Current documented build:** `2026.08.02-svg-v5.1-DOCUMENTED-LOCALSPACE-HYBRID-10MSG`  
> **Protocol:** MCoreIMG v5  
> **Feature signature:** `PROTO5|LOCALSPACE|HYBRID|PRIMITIVES|GROUPCOPY|ALPHA|UNDO|10MSG`

## Highlights

- Imports `.svg`, `.svgz`, and editable `.mci.json` source files.
- Adds several SVG files to one canvas without replacing existing artwork.
- Keeps imported SVG geometry in **local coordinate space**, so display scaling does not make the transmission substantially smaller or larger.
- Supports direct canvas editing:
  - click artwork to select it;
  - drag selected artwork to move it;
  - drag the blue corner handle to scale it;
  - recolor fills, strokes, and Moon crater colors.
- Includes the legacy **Old Drawing Mode** and compact MCoreIMG primitives.
- Automatically compares each compact primitive with its generic vector expansion and transmits whichever is actually smaller in context.
- Detects translated copies of commands and SVG groups and uses compact repeat references when beneficial.
- Supports RGB565 color with 4-bit alpha and source-over compositing.
- Previews the encoded-and-decoded image rather than an idealized source render.
- Exports editable source JSON, MeshCore frame text, and decoded PNG previews.
- Provides Undo for imports and drawing actions, including `Ctrl+Z`.
- Includes CRC validation, decoder limits, a build-integrity guard, and a built-in regression suite.

## Requirements

- Python 3.10 or newer is recommended.
- Tkinter/Tk for the graphical interface.
- Pillow for accurate preview rendering and PNG export.

### Arch Linux

```bash
sudo pacman -S --needed python tk python-pillow
```

### Python package installation

Tkinter is normally supplied by the operating system. Pillow can also be installed into a Python environment with:

```bash
python -m pip install Pillow
```

## Running the Constructor

```bash
python MCoreIMG-SVG-Constructor-v5.1-DOCUMENTED.py
```

Confirm that the file is the intended build:

```bash
python MCoreIMG-SVG-Constructor-v5.1-DOCUMENTED.py --version
```

Expected output includes:

```text
2026.08.02-svg-v5.1-DOCUMENTED-LOCALSPACE-HYBRID-10MSG
PROTO5|LOCALSPACE|HYBRID|PRIMITIVES|GROUPCOPY|ALPHA|UNDO|10MSG
protocol=5 messages=10 source_version=5
```

Run the regression suite before distributing a modified build:

```bash
python MCoreIMG-SVG-Constructor-v5.1-DOCUMENTED.py --self-test
```

A successful run ends with:

```text
MCoreIMG Hybrid SVG Constructor self-test: PASS
```

## Quick Start

1. Launch the Constructor.
2. Use **Open SVG / Source** to start a new document, or **Add SVG(s)** to append one or more SVG files.
3. Click an imported object on the canvas.
4. Drag it to reposition the complete imported SVG group.
5. Drag the blue lower-right handle to scale the selected group.
6. Use the selected-artwork color controls to change fill, stroke, or Moon crater colors.
7. Open **Old Drawing Mode** to add text, lines, geometric shapes, or compact radio-oriented primitives.
8. Watch the status bar for payload characters and the current `n/10` message count.
9. Use **Preview Frames** to inspect or copy the final MeshCore messages.
10. Export `.mci`, editable `.mci.json`, or a decoded PNG preview.

When an import or drawing action pushes the image over the ten-message limit, press **Undo** or `Ctrl+Z` to restore the document to its previous state.

## Old Drawing Mode

The legacy drawing window supports normal vector shapes and compact MCoreIMG primitives, including:

- Text
- Line
- Rectangle
- Ellipse
- Filled and outlined triangles
- Arrow
- Star
- Arc
- Radio waves
- Yagi antenna
- Dish antenna
- Radio
- Moon
- DoubleBox

Some shapes use one canvas click. Line, Rectangle, and DoubleBox use two clicks. Exact coordinates and shape parameters can also be entered manually.

The selected drawing representation is not decided by the user interface alone. During encoding, the Constructor measures the contextual bit cost of:

1. the compact primitive record; and
2. the equivalent generic vector commands.

The primitive is used only when it is **strictly smaller**. Otherwise, the vector expansion is transmitted.

## Multiple and Copied SVGs

**Add SVG(s)** appends files instead of replacing the document. Imported commands receive editor-only group metadata so an entire SVG can be selected, moved, scaled, duplicated, or deleted as one object.

For transmission, the encoder independently looks for reusable geometry:

- translated single-command repeats;
- translated contiguous command groups;
- reusable protocol-v5 local SVG definitions;
- repeated local definitions with different placement or scale transforms.

Editor grouping itself is not transmitted and costs no direct bits. Keeping an imported group contiguous in draw order gives the encoder the best opportunity to reuse it.

## Why SVG Scale No Longer Changes the Message Count

Earlier branches flattened the displayed size into every path coordinate. Smaller artwork produced smaller coordinate deltas and therefore fewer bits, even though the SVG contained the same geometry.

Protocol v5 separates:

```text
local SVG geometry
        +
canvas placement and scale
```

The geometry is encoded in a stable local coordinate box. Moving or resizing the SVG changes only its fixed-width transform record. As a result, an imported SVG should have approximately the same encoded bit count at 100%, 50%, or 10% display scale.

Minor Base91 character-count differences can still occur because of byte padding, but the geometry cost and frame count should remain stable.

## SVG Support

The importer handles a practical, deliberately limited SVG subset intended for compact radio transmission.

Supported or normalized features include:

- `<path>`
- `<line>`
- `<polyline>`
- `<polygon>`
- `<rect>`, including conversion where required
- `<circle>` and `<ellipse>`
- nested groups
- basic transforms
- solid fill and stroke colors
- stroke width
- opacity, fill opacity, and stroke opacity
- inherited styles
- basic CSS tag, class, and ID selectors
- `<use>` references
- path operations based on move, line, quadratic Bézier, cubic Bézier, close-path, and normalized SVG shorthand

SVG arcs and unsupported geometry may be approximated during import. The **Import Notes** panel reports skipped or approximated features.

Features such as animation, scripting, filters, masks, complex clipping, embedded raster images, advanced text layout, gradients, and patterns are not intended to be fully preserved by this branch.

## Color and Alpha

Palette entries use:

- RGB565 color; and
- 4-bit alpha, providing 16 opacity levels.

The same Pillow source-over compositor is used for the GUI preview and PNG export. Preview rendering is performed from the transport-decoded commands whenever the image fits, so the screen reflects palette quantization, alpha quantization, coordinate precision, and protocol reconstruction.

## Message Envelope

The MeshCore profile is fixed at:

- **10 messages maximum**
- **150 characters per message**
- **135 payload characters per framed message**
- **1,350 Base91 payload characters maximum**

Each frame includes protocol and image identification, part numbering, payload length, and a frame CRC. The reconstructed stream also includes a stream CRC.

The Constructor rejects malformed, mixed, incomplete, corrupt, or over-limit frame sets.

## File Types

### SVG and SVGZ

Source artwork imported and normalized into editable MCoreIMG commands.

### `.mci.json`

Editable Constructor source. It preserves command order, styles, primitive parameters, document transforms, warnings, and editor grouping.

This is not the radio transport format and may evolve independently of the protocol version.

### `.mci`

A text file containing one complete MeshCore frame per line.

### PNG

A raster preview of the transport-visible decoded image. PNG export is useful for visual verification but is not transmitted.

## Reconstructor Compatibility

This Constructor emits **MCoreIMG protocol v5**. A Reconstructor must understand the v5 local-space SVG-group records, compact primitives, translated repeats, RGB565+A4 palette entries, and the ten-message envelope.

Older v2, v3, or v4 Reconstructors will not correctly decode all v5 images.

## Architecture Overview

The current file grew through several protocol experiments and intentionally contains layered redefinitions. Python uses the **last definition** of a repeated global name.

The active implementation consists conceptually of:

1. vector and SVG model helpers;
2. SVG parsing and geometry normalization;
3. rendering and palette conversion;
4. legacy primitive expansion and comparison;
5. translated command/group repeat planning;
6. protocol-v5 local-space group planning;
7. bitstream and MeshCore framing;
8. the final direct-editing and Undo GUI layer;
9. the final regression suite and entry point.

When maintaining the current single-file build, search from the bottom upward for repeated names such as:

- `encode_commands`
- `decode_commands`
- `encode_image`
- `CodecStats`
- `ConstructorApp`
- `run_self_test`

See [`MCoreIMG-v5.1-TECHNICAL-DEBT-GUIDE.md`](MCoreIMG-v5.1-TECHNICAL-DEBT-GUIDE.md) for the recommended cleanup sequence and module split.

## Development Rules

Before committing protocol or editor changes:

1. Run `--version` and verify protocol 5 and the expected feature signature.
2. Run `--self-test`.
3. Import a normal SVG and a multi-SVG document.
4. Verify that dragging and scaling operate on the intended complete group.
5. Verify that the payload remains stable when only an SVG's display scale changes.
6. Test a translucent overlap image and compare preview with exported PNG.
7. Draw at least one compact primitive and verify the primitive/vector comparison statistics.
8. Duplicate an imported SVG and verify local-definition or group-copy reuse.
9. Import an oversized SVG and verify that Undo restores the previous document.
10. Decode the exported frames with the matching protocol-v5 Reconstructor.

## TinyVG Relationship

TinyVG was used as an architectural reference for reducing SVG into a deterministic palette and ordered command stream. MCoreIMG is not a TinyVG implementation or TinyVG-compatible file format. It is optimized specifically for severe MeshCore message limits, text-safe framing, repeated artwork, compact legacy primitives, and radio-oriented reconstruction.

## Known Technical Debt

- The implementation is currently a large single Python file.
- Historical definitions remain in the file and are superseded by later definitions.
- Some command geometry is stored in opcode-dependent dictionaries rather than dedicated dataclasses.
- Local-space precision selection temporarily uses shared configuration state.
- Compression planning is contextual and greedy rather than globally optimal.
- The GUI is composed from layered classes that should eventually be collapsed.

These choices were retained to preserve a working protocol during rapid experimentation. Refactoring should be performed only with frozen frame and rendering fixtures so cleanup does not silently alter transport behavior.

## Safety and Operating Considerations

MCoreIMG is designed for occasional, deliberate image transmission over constrained radio networks. A technically valid frame set can still consume shared airtime. Operators should follow local rules, network conventions, and good amateur-radio practice when choosing when and how often to transmit images.
