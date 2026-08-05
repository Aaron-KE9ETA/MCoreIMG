# MCoreIMG

Compact vector image transmission over MeshCore.

MCoreIMG encodes vector artwork into no more than **ten 150-character MeshCore
messages**. It imports a practical SVG subset, combines it with compact
radio-oriented drawing primitives, and compresses the result into a text-safe
transport that survives ordinary chat relays.

> **Protocol:** MCoreIMG v5
> **Feature signature:** `PROTO5|LOCALSPACE|HYBRID|PRIMITIVES|GROUPCOPY|ALPHA|UNDO|10MSG`
> **Status:** pre-alpha

---

## Modules

The project is four files in a strict dependency chain. Each layer may import
the ones above it and never the ones below.

| Module | Role | Imports | Docs |
|---|---|---|---|
| `MCoreIMG-model.py` | Shared vocabulary: opcodes, commands, geometry, colour | stdlib only | [Model_readme.md](Model_readme.md) |
| `MCoreIMG-compression.py` | Bitstream codec, record selection, MeshCore framing | model | [Compression_readme.md](Compression_readme.md) |
| `MCoreIMG-Constructor.py` | SVG import, rendering, editor GUI | model, compression | [Constructor_readme.md](Constructor_readme.md) |
| `MCoreIMG-Reconstructor.py` | Frame decoding, PNG output, JSON export | model, compression, constructor | [Reconstructor_readme.md](Reconstructor_readme.md) |

Design rationale, layering rules, and refactor history are in
[Architecture.md](Architecture.md).

```
MCoreIMG-model.py            no dependencies
        |
MCoreIMG-compression.py      transport codec
        |
MCoreIMG-Constructor.py      authoring
        |
MCoreIMG-Reconstructor.py    receiving
```

### Which file do I edit?

| Change | File |
|---|---|
| Compression scheme, record types, bit layout, framing | `MCoreIMG-compression.py` |
| New opcode, new primitive, geometry or colour rules | `MCoreIMG-model.py` (then teach the codec to carry it) |
| SVG parsing, rendering, editor behaviour | `MCoreIMG-Constructor.py` |
| Decoding workflow, output formats, CLI | `MCoreIMG-Reconstructor.py` |

Compression is the single file to touch when changing how images are packed.
That is the point of the split.

---

## Requirements

- Python 3.10 or newer
- Tkinter/Tk — required by the Constructor GUI; the Reconstructor needs it
  only for the interactive file chooser
- Pillow — required for preview rendering and PNG export

### Arch Linux

```bash
sudo pacman -S --needed python tk python-pillow
```

### Other environments

Tkinter normally ships with the operating system. Pillow installs with:

```bash
python -m pip install Pillow
```

---

## File layout

Keep all four modules in one directory. The Constructor finds the codec beside
itself; the codec finds the model beside itself; the Reconstructor finds both.

```
mcoreimg/
├── MCoreIMG-model.py
├── MCoreIMG-compression.py
├── MCoreIMG-Constructor.py
└── MCoreIMG-Reconstructor.py
```

Each lookup can be overridden on the command line if you keep builds elsewhere.

---

## Quick start

### Authoring

```bash
python MCoreIMG-Constructor.py
```

Import SVG, arrange it, watch the `n/10` message counter, then export `.mci`
frames.

### Receiving

```bash
python MCoreIMG-Reconstructor.py received.mci
```

Decodes, validates, renders a PNG, and opens it.

### Verifying an installation

```bash
python MCoreIMG-Constructor.py --version
python MCoreIMG-Constructor.py --self-test
python MCoreIMG-Reconstructor.py --self-test
```

`--version` prints the whole chain, so a mismatched set is visible immediately:

```text
2026.08.05-svg-v5.2-MODULAR-LOCALSPACE-HYBRID-10MSG
PROTO5|LOCALSPACE|HYBRID|PRIMITIVES|GROUPCOPY|ALPHA|UNDO|10MSG
protocol=5 messages=10 source_version=5
codec=2026.08.05-compression-v5.2-MODULAR
codec_path=/path/to/MCoreIMG-compression.py
model=2026.08.05-model-v5.2-MODULAR
model_path=/path/to/MCoreIMG-model.py
```

---

## Transport profile

| Property | Value |
|---|---|
| Messages per image | 10 maximum |
| Characters per message | 150 |
| Frame header | 15 characters |
| Payload per frame | 135 characters |
| Total payload | 1,350 Base91 characters |
| Canvas | 720 × 480 |
| Palette | 32 entries, RGB565 + 4-bit alpha |
| Commands | 2,048 maximum after expansion |

Each frame carries protocol and image identification, part numbering, payload
length, and a CRC-16. The reassembled stream carries a CRC-32.

---

## How compression works

A short tour. [Compression_readme.md](Compression_readme.md) has the detail.

**Palette.** Every colour is quantized to RGB565 plus 4-bit alpha and collected
into an ordered table. Commands then reference palette indices.

**Stateful coding.** Point and style state are tracked per opcode. Coordinates
are usually deltas against the previous point for the same opcode, coded with
Rice and Exp-Golomb, falling back to absolute only when that is cheaper.

**Repeat detection.** The encoder looks for translated copies of single
commands and of contiguous command runs, and emits a short back-reference plus
a delta rather than the geometry again.

**Local-space SVG groups.** An imported SVG is normalized into a stable local
coordinate box, transmitted once, and placed with a fixed-width transform.
Moving or resizing the artwork changes only the transform, so the same drawing
costs the same number of bits at 100%, 50%, or 10% display scale. Duplicated
SVGs reuse the definition and send only a new transform.

**Primitive pricing.** Compact primitives such as Yagi, Dish, and Moon are
measured against their generic vector expansion in context. The primitive is
used only when it is strictly smaller.

**Precision search.** `encode_image` walks a ladder of local-coordinate extents
from 255 down to 32 and returns the highest precision that still fits the
ten-message budget.

---

## Record types

| Tag | Name | Meaning |
|---|---|---|
| 0 | `REC_NORMAL` | A fully specified command |
| 1 | `REC_SINGLE_REPEAT` | Re-emit one earlier command at a delta |
| 2 | `REC_GROUP_REPEAT` | Re-emit a contiguous run at a delta |
| 3 | `REC_TRANSFORM_GROUP` | A local-space SVG group: display box plus an inline definition or a back-reference |

---

## File formats

| Extension | Purpose | Transmitted |
|---|---|---|
| `.svg` / `.svgz` | Source artwork for import | no |
| `.mci.json` | Editable Constructor source | no |
| `.mci` | One MeshCore frame per line | yes |
| `.png` | Raster preview of the decoded image | no |

`.mci.json` preserves command order, styles, primitive parameters, document
transforms, warnings, and editor grouping. It is not the radio transport format
and may evolve independently of the protocol version.

---

## Compatibility

The Constructor emits protocol v5. A Reconstructor must understand v5
local-space SVG group records, compact primitives, translated repeats,
RGB565+A4 palette entries, and the ten-message envelope.

Older v2, v3, and v4 receivers will not correctly decode v5 images.

The current build detects mismatches directly. Protocol numbers are compared
between the input, the codec, and the renderer, and every failure names the
files involved and their build strings.

---

## Development rules

Before committing protocol or editor changes:

1. Run `--version` on both tools and confirm protocol 5 with the expected
   feature signature and matching model/codec builds.
2. Run `--self-test` on both tools.
3. Import a normal SVG and a multi-SVG document.
4. Verify dragging and scaling operate on the intended complete group.
5. Verify the payload stays stable when only display scale changes.
6. Test a translucent overlap image and compare preview against exported PNG.
7. Draw at least one compact primitive and check the primitive/vector
   statistics.
8. Duplicate an imported SVG and confirm local-definition reuse.
9. Import an oversized SVG and confirm Undo restores the previous document.
10. Decode the exported frames with the paired Reconstructor.

### Changing the compression scheme

Any change to a writer must be mirrored in its reader. A change that alters
emitted bits for unchanged input is a **protocol change**: bump
`PROTOCOL_VERSION` and update both ends in the same commit.

Do not combine a structural cleanup with a protocol-format change. Those are two
separate review problems.

---

## TinyVG relationship

TinyVG was an architectural reference for reducing SVG into a deterministic
palette and ordered command stream. MCoreIMG is **not** a TinyVG implementation
or a TinyVG-compatible file format. It is optimized specifically for severe
MeshCore message limits, text-safe framing, repeated artwork, compact legacy
primitives, and radio-oriented reconstruction.

---

## Safety and operating considerations

MCoreIMG is designed for occasional, deliberate image transmission over
constrained radio networks. A technically valid frame set can still consume
shared airtime. Operators should follow local rules, network conventions, and
good amateur-radio practice when choosing when and how often to transmit
images.
