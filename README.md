# MCoreIMG SVG Integration

**MCoreIMG SVG Integration is an experimental vector-image codec for importing practical SVG artwork, compressing it into MeshCore-sized text frames, and reconstructing it as a PNG.**

Instead of transmitting a raster image, MCoreIMG transmits a compact drawing program: a palette, vector commands, geometry, style changes, and repeat references. The receiving side validates the transport, decodes the command stream, and renders the image locally.

This branch extends the original MCoreIMG concept with:

- SVG and SVGZ import
- Scalable vector geometry
- Paths, curves, fills, strokes, transforms, and draw order
- True alpha-channel transport in protocol 3
- A ten-message MeshCore envelope
- Shared constructor/reconstructor codec behavior
- Aggressive stateful compression designed around very small messages

> [!WARNING]
> This project is pre-alpha. The source format, transport format, and protocol version may change without backward compatibility. Keep matching constructor and reconstructor builds together.

## Branch Status

The current SVG alpha branch uses:

| Component | Current behavior |
|---|---|
| Canvas | 720 × 480 pixels |
| Preferred protocol | Protocol 3 |
| Protocol 3 color | RGB565 + 4-bit alpha |
| Transport limit | Up to 10 messages |
| Message limit | 150 ASCII characters each |
| Frame header | 15 characters |
| Payload per frame | Up to 135 Base91 characters |
| Palette | Up to 32 entries per image |
| Command limit | 2048 vector commands |
| Integrity checks | Per-frame CRC-16 and whole-stream CRC-32 |

Protocol 2 remains useful for older opaque SVG/vector transports. The protocol-aware reconstructor supports both protocol 2 and protocol 3, but protocol 3 is preferred for the current branch.

## What This Branch Is

MCoreIMG SVG Integration is a constrained SVG-to-vector transport system. It is designed to preserve the useful visual structure of simple SVG artwork while fitting within severe MeshCore message limits.

It is **not**:

- A complete web-browser SVG implementation
- A conventional SVG compressor
- A lossless replacement for the original SVG document
- TinyVG-compatible
- Intended for photographs or arbitrary raster images

The editable `.mci.json` source retains MCoreIMG vector commands and document transforms. The transmitted `.mci` file contains only the information required to reconstruct the image.

## Current Components

Canonical filenames after merge should be:

| File | Purpose |
|---|---|
| `MCoreIMG-SVG-Constructor.py` or `MCoreIMG-Constructor.py` | SVG importer, vector editor, codec, preview renderer, PNG exporter, and MeshCore frame generator |
| `MCoreIMG-Reconstructor.py` | Protocol detector, frame validator, decoder, source exporter, and PNG reconstructor |
| `*.mci.json` | Editable MCoreIMG SVG/vector source |
| `*.mci` | MeshCore-ready transport frames |
| `*.svg` / `*.svgz` | Imported source artwork |
| `*.png` | Raster preview or reconstructed output |

The development branch may use versioned filenames such as:

```text
MCoreIMG-SVG-Constructor-v3.3-ALPHA-ROUNDTRIP.py
MCoreIMG-Reconstructor-v3.3-ALPHA-FIXED.py
```

The reconstructor searches for compatible constructor files in its own directory and the current working directory. A specific core can also be supplied with `--core`.

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

Tkinter may need to be installed through the operating system's package manager.

Pillow is required for:

- PNG export
- RGBA alpha compositing
- Authoritative GUI preview parity
- Reconstructor output
- The full self-test suite

## Quick Start

### Start the Constructor

Using canonical filenames:

```bash
python MCoreIMG-SVG-Constructor.py
```

Using the current versioned alpha filename:

```bash
python MCoreIMG-SVG-Constructor-v3.3-ALPHA-ROUNDTRIP.py
```

### Import and Export Artwork

1. Select **Open SVG / Source**.
2. Open an `.svg`, `.svgz`, or `.mci.json` file.
3. Adjust scale and offset as needed.
4. Use **Fit** to place the complete image inside the canvas.
5. Use **Center** to center the current transformed bounds.
6. Use **Bake Transform** to permanently apply document scale and offset.
7. Save editable work as `.mci.json`.
8. Export MeshCore transport as `.mci`.
9. Export a local preview as `.png`.

Imported SVG artwork is fitted inside the 720 × 480 canvas by default with a small margin. Document scale and offset remain non-destructive until **Bake Transform** is used.

The status display reports:

- Command count
- Palette size
- Encoded payload characters
- Required frame count
- Whether the image fits the ten-message profile
- Whether transformed artwork extends outside the canvas

### Reconstruct an Image

Using canonical filenames:

```bash
python MCoreIMG-Reconstructor.py image.mci
```

Specify an output path:

```bash
python MCoreIMG-Reconstructor.py image.mci --output reconstructed.png
```

Use a specific constructor core:

```bash
python MCoreIMG-Reconstructor.py image.mci \
  --core ./MCoreIMG-SVG-Constructor.py
```

Using the current versioned alpha files:

```bash
python MCoreIMG-Reconstructor-v3.3-ALPHA-FIXED.py image.mci \
  --core ./MCoreIMG-SVG-Constructor-v3.3-ALPHA-ROUNDTRIP.py
```

The reconstructor can also read:

- Plain text containing MCoreIMG frames
- Copied transcripts containing labeled frames
- `.mci.json` editable source
- `.svg` and `.svgz` source when a compatible constructor core is available

When no input file is supplied, a graphical file chooser opens.

## Reconstructor Options

```text
-o, --output PATH       Select the output PNG path
--core PATH             Select the matching constructor/codec Python file
--protocol {2,3}        Force protocol 2 or 3
--dump-json             Write reconstructed MCoreIMG source JSON
--list-commands         Print decoded commands and alpha values
--no-open               Do not automatically open the output PNG
--self-test             Run constructor/transport/reconstructor tests
```

Examples:

```bash
python MCoreIMG-Reconstructor.py image.mci --list-commands
```

```bash
python MCoreIMG-Reconstructor.py image.mci --dump-json --no-open
```

```bash
python MCoreIMG-Reconstructor.py --protocol 3 --self-test
```

## Supported Vector Commands

The current transport represents six general vector command types:

| Opcode | Command |
|---:|---|
| `0` | Rectangle |
| `1` | Ellipse |
| `2` | Line |
| `3` | Polyline |
| `4` | Polygon |
| `5` | Path |

SVG circles are represented as ellipses. Rounded rectangles may be converted into paths.

Paths support:

- MoveTo
- LineTo
- Quadratic Bezier
- Cubic Bezier
- ClosePath

The importer normalizes common SVG path shortcuts, including horizontal, vertical, smooth cubic, and smooth quadratic commands. SVG arc commands are approximated with line nodes before transport.

## Supported SVG Features

The importer currently handles a practical subset of SVG, including:

- `<rect>`
- `<circle>`
- `<ellipse>`
- `<line>`
- `<polyline>`
- `<polygon>`
- `<path>`
- Nested groups and containers
- Affine transforms
- `viewBox`
- Common physical and pixel length units
- Fill and stroke colors
- Stroke width
- `nonzero` and `evenodd` fill rules
- Inherited presentation attributes
- Inline style declarations
- Element opacity
- Fill opacity
- Stroke opacity
- `currentColor`
- Open-subpath SVG fill behavior

Supported transforms include:

- `matrix`
- `translate`
- `scale`
- `rotate`
- `skewX`
- `skewY`

## Alpha-Channel Support

Protocol 3 adds true transported transparency.

Each palette entry stores:

- RGB color quantized to RGB565
- Alpha quantized to 4 bits, from 0 through 15

This is referred to as **RGB565+A4**.

SVG `opacity`, `fill-opacity`, and `stroke-opacity` values are combined during import. Alpha then survives:

1. SVG import
2. MCoreIMG source storage
3. Palette quantization
4. Bitstream encoding
5. Base91 frame transport
6. Frame decoding
7. Command reconstruction
8. Source-over compositing
9. RGBA PNG output

Because alpha is quantized to 16 levels, reconstructed transparency may differ slightly from the original SVG.

Protocol 2 transports opaque RGB565 colors only.

## Rendering Semantics

Commands are rendered sequentially in source order. Later commands may cover or blend over earlier commands.

The constructor preview and PNG exporter use the same Pillow-based rendering path whenever Pillow is available. This prevents the GUI preview, exported PNG, and reconstructed PNG from using separate interpretations of paths or alpha.

Open SVG subpaths are implicitly closed for filling but are not automatically closed for stroking unless the source path contains a real ClosePath command.

Protocol 3 uses source-over alpha compositing and produces RGBA PNG output.

## Compression Model

MCoreIMG does not compress the original SVG text. It converts the artwork into a compact command stream and compresses that stream structurally.

Current compression techniques include:

- Per-image palette indexing
- RGB565+A4 palette entries
- Opcode-local style state
- Opcode-local point and geometry state
- Predictive coordinate coding
- Delta encoding
- Unsigned and signed Exp-Golomb values
- Rice-style coding where useful
- Reuse of prior style values
- Nonadjacent translated-repeat references
- Shared path segment representation
- Base91 text encoding

### Opcode-Local State

Each command type retains its own recent state. A rectangle can therefore reuse prior rectangle values even when lines, paths, or ellipses occur between the two rectangle commands.

This preserves ordinary artistic draw order without requiring the artist to group all commands of the same type together.

### Nonadjacent Translated Repeats

A command can reference a compatible earlier command and transmit only a translation offset when the same geometry appears elsewhere.

This is useful for repeated:

- Eyes
- Buttons
- Windows
- Stars
- Symbols
- Decorative shapes
- Repeated path components

The repeated command does not need to be adjacent to the original.

## Transport Profile

Each exported frame is no longer than 150 characters:

```text
[15-character MCoreIMG header][0 to 135 Base91 payload characters]
```

An image may occupy one through ten frames.

The complete encoded payload capacity is therefore:

```text
10 × 135 = 1350 Base91 payload characters
```

Transport frames begin with `MCI` and include:

- Protocol version
- Image identifier
- Frame index
- Total frame count
- Frame integrity information
- Encoded payload segment

The codec applies:

- CRC-16 to individual frames
- CRC-32 to the complete binary stream

A corrupted, missing, duplicated, incompatible, or incomplete frame set is rejected rather than rendered silently.

ACK handling and retransmission are transport/application responsibilities. The codec provides the frame numbering and corruption detection needed to request a missing or damaged frame.

## File Formats

### SVG and SVGZ

Original authoring input. SVGZ is gzip-compressed SVG.

### MCoreIMG Source JSON

`.mci.json` is the editable intermediate format.

It stores:

- Source format and source version
- Protocol version
- Canvas dimensions
- Source name
- Document scale
- Document offsets
- Ordered vector commands
- Fill and stroke styles
- Geometry
- Labels
- Visibility
- Import warnings

This file is not intended for transmission over MeshCore.

### MCoreIMG Transport

`.mci` contains one complete transport frame per line.

Example structure:

```text
MCI...
MCI...
MCI...
```

The payload should be treated as opaque transport text. Manually editing a frame will normally invalidate its CRC.

### PNG

The constructor exports previews, and the reconstructor produces the received image.

Protocol 3 output is RGBA. Protocol 2 output is opaque RGB.

## Protocol Compatibility

| Protocol | Palette | Alpha | Source version | Reconstructor support |
|---:|---|---|---:|---|
| 2 | RGB565 | No | 1 | Supported |
| 3 | RGB565+A4 | Yes | 2 | Preferred |

A protocol-2-only reconstructor cannot decode protocol-3 transport.

The protocol-aware reconstructor detects the protocol from transport frames and loads a matching constructor core. When a matching core cannot be found, it reports the searched and rejected candidates.

Keep the constructor beside the reconstructor, or use:

```bash
--core /path/to/matching-constructor.py
```

## Self-Tests

Run the constructor test:

```bash
python MCoreIMG-SVG-Constructor.py --self-test
```

Run the protocol-3 reconstructor test:

```bash
python MCoreIMG-Reconstructor.py --protocol 3 --self-test
```

Run the protocol-2 compatibility test when an appropriate protocol-2 core is present:

```bash
python MCoreIMG-Reconstructor.py --protocol 2 --self-test
```

The current tests cover important regression areas, including:

- Encoder/decoder command round trips
- Source JSON round trips
- Maximum ten-frame envelope behavior
- Exact 150-character frame limits
- SVG default fitting
- Open-subpath fill semantics
- RGB565+A4 alpha preservation
- Alpha compositing
- Constructor/reconstructor render parity
- Repeat-reference decoding
- Frame corruption rejection
- Stream CRC rejection

A basic syntax check can also be run before committing:

```bash
python -m py_compile MCoreIMG-SVG-Constructor.py MCoreIMG-Reconstructor.py
```

## Known Limitations

- This is a practical SVG subset, not a complete SVG implementation.
- SVG text is not transported as text.
- Embedded raster images are not supported.
- Filters are not supported.
- Masks and clip paths are not currently represented.
- Gradients and patterns may be approximated rather than reproduced.
- Arc commands are flattened into line segments.
- Bezier paths are rasterized through flattened geometry.
- RGB is quantized to RGB565.
- Alpha is quantized to four bits.
- Images with more than 32 required palette entries must be simplified or quantized further.
- Artwork extending outside 720 × 480 must be fitted or resized before export.
- Artwork requiring more than ten frames cannot be exported under the current MeshCore profile.
- Original SVG IDs, labels, CSS structure, and authoring metadata are not fully transmitted.
- A reconstructed `.mci.json` file cannot restore authoring details that were never included in transport.
- Pre-alpha protocol changes may make older `.mci` files incompatible.

## Suggestions for Smaller Transmissions

To reduce frame count:

- Remove invisible or redundant objects.
- Simplify paths before import.
- Reduce unnecessary path nodes.
- Reuse the same colors.
- Avoid tiny visual details.
- Prefer repeated geometry where possible.
- Convert complex gradients into a few flat colors.
- Merge overlapping shapes when that does not change the image.
- Remove off-canvas geometry.
- Avoid excessive stroke-width variation.
- Test whether reduced alpha variation is visually acceptable.

MCoreIMG compression works best with deliberately simple illustration-style artwork.

## Merge Checklist

Before merging the SVG integration branch:

- [ ] Rename versioned development files to the intended canonical filenames.
- [ ] Confirm the constructor reports protocol 3.
- [ ] Confirm the reconstructor prefers protocol 3 and supports protocol 2.
- [ ] Run both self-tests.
- [ ] Run `py_compile`.
- [ ] Import a representative SVG with paths and opacity.
- [ ] Confirm constructor preview and exported PNG match.
- [ ] Export the image to `.mci`.
- [ ] Reconstruct the `.mci` into PNG.
- [ ] Confirm alpha survives the complete round trip.
- [ ] Confirm the image remains within ten 150-character messages.
- [ ] Commit the updated README with the constructor and reconstructor together.

## Project Direction

The goal is not to reproduce every feature of SVG. The goal is to identify the smallest useful vector feature set that lets a human create recognizable artwork and transmit it through MeshCore without manually writing or optimizing the compressed stream.

The constructor should absorb the complexity. The operator should be able to import or create artwork normally, see whether it fits, and export a validated transport that the reconstructor can reproduce consistently.
