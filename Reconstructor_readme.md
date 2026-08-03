# MCoreIMG Reconstructor

The **MCoreIMG Reconstructor** converts MCoreIMG transport frames back into a PNG image by loading the matching MCoreIMG Constructor as its codec and renderer.

The current compatibility target is the **Constructor v5.1 local-space hybrid format**, including:

- Protocol 5 transport
- Local-space SVG groups
- Fixed-width placement transforms
- Hybrid SVG/vector and legacy primitive commands
- Automatic primitive-versus-vector representation selection
- Translated group-copy references
- RGB565+A4 color and alpha
- Source-over RGBA compositing
- Up to ten 150-character MeshCore messages

The reconstructor also retains best-effort compatibility with matching protocol 2, 3, and 4 Constructor builds.

---

## Why the Constructor is required

The reconstructor does not maintain a second handwritten implementation of the MCoreIMG codec.

Instead, it imports the matching Constructor Python file and uses the Constructor's own:

- Frame decoder
- Opcode definitions
- Palette unpacking
- Alpha handling
- Primitive and SVG command models
- Local-space group logic
- Translated-copy reference handling
- Drawing-order rules
- Pillow renderer
- CRC and transport validation

This prevents the Constructor and Reconstructor from silently drifting apart as the protocol evolves.

A protocol-5 image must be decoded by a Constructor that declares:

```python
PROTOCOL_VERSION = 5
```

The filename alone does not determine compatibility.

---

## Current status

| Component | Current target |
|---|---|
| Reconstructor build | `2026.08.02-v5.1-localspace-hybrid-sync-documented` |
| Preferred protocol | 5 |
| Preferred Constructor | v5.1 local-space hybrid |
| Current source version | 5 |
| Canvas default | 720 × 480 |
| Transport envelope | 10 messages × 150 characters |
| Output | PNG through Pillow |
| Editable recovery | Best-effort `.mci.json` |

Current protocol-5 feature signature:

```text
PROTO5 | LOCALSPACE | HYBRID | PRIMITIVES | GROUPCOPY | ALPHA | 10MSG
```

Older Constructor builds may still work when they expose a supported decoder and renderer and declare the same protocol as the input.

---

## Requirements

### Required

- Python 3.10 or newer recommended
- A matching MCoreIMG Constructor Python file
- The Python dependencies required by that Constructor
- Pillow, normally imported and used by the Constructor renderer

### Optional

- Tkinter, for the graphical input-file chooser
- `xdg-open`, `open`, or the Windows shell, for opening the completed PNG automatically

The Reconstructor itself uses only the Python standard library. Image rendering is delegated to the Constructor.

---

## Recommended file layout

Place the Reconstructor beside the current Constructor:

```text
MCoreIMG-svg/
├── MCoreIMG-Reconstructor.py
├── MCoreIMG-Constructor.py
├── example.mci
└── example.mci.json
```

Versioned Constructor filenames are also supported:

```text
MCoreIMG-svg/
├── MCoreIMG-Reconstructor.py
└── MCoreIMG-SVG-Constructor-v5.1-LOCALSPACE-HYBRID-VERIFIED.py
```

The Reconstructor searches:

1. Its own directory
2. The current working directory
3. Preferred Constructor filenames
4. Versioned filenames matching broad Constructor patterns

Use `--core` when an exact Constructor must be selected.

---

## Quick start

Reconstruct a transport file:

```bash
python MCoreIMG-Reconstructor.py image.mci
```

Specify the output filename:

```bash
python MCoreIMG-Reconstructor.py image.mci \
  --output reconstructed.png
```

Force an exact Constructor:

```bash
python MCoreIMG-Reconstructor.py image.mci \
  --core ./MCoreIMG-Constructor.py
```

Prevent the PNG from opening automatically:

```bash
python MCoreIMG-Reconstructor.py image.mci --no-open
```

When no input path is supplied, the Reconstructor opens a graphical file chooser:

```bash
python MCoreIMG-Reconstructor.py
```

Tkinter is only required for this interactive mode.

---

## Supported input formats

### MCoreIMG transport

```text
.mci
.txt
```

The transport reader can extract frames from:

- Normal one-frame-per-line exports
- Plain copied frame text
- Lines containing labels before an `MCI...` frame
- Multiple complete frames surrounded by unrelated text

Duplicate frames are removed while preserving their original order.

All frames in one input must use the same protocol version.

### Editable MCoreIMG source

```text
.mci.json
.json
```

Editable source files are passed to the selected Constructor's source loader.

When the JSON contains `protocol_version`, the Reconstructor uses it to select a matching core.

### SVG source

```text
.svg
.svgz
```

SVG files are passed to the Constructor's SVG/source loader. This is useful for testing import and rendering parity without first exporting transport frames.

SVG support depends on the selected Constructor.

---

## Output behavior

Unless `--output` is supplied, the Reconstructor writes a timestamped PNG beside the input file:

```text
image-reconstructed-20260802-211400.png
```

For an input named:

```text
image.mci.json
```

the generated name uses `image` as the base rather than `image.mci`.

After rendering, the Reconstructor reports:

- Reconstructor build
- Selected Constructor path
- Constructor build and version
- Protocol and source version
- Advertised feature signature
- Transport envelope
- Decoder and renderer APIs
- Loaded representation
- Decoded command count, when available
- PNG dimensions and color mode
- Final output path

The PNG opens automatically unless `--no-open` is used.

---

## Command-line options

| Option | Purpose |
|---|---|
| `input` | `.mci`, text, `.mci.json`, `.json`, `.svg`, or `.svgz` input |
| `-o`, `--output PATH` | Select the output PNG path |
| `--core PATH` | Use one exact Constructor Python file |
| `--protocol N` | Force a protocol for source loading or self-test |
| `--dump-json` | Write best-effort Constructor-compatible source JSON |
| `--list-commands` | Print decoded scene and command contents |
| `--no-open` | Do not open the PNG after reconstruction |
| `--self-test` | Run Constructor encode/decode/render round-trip tests |
| `--show-core` | Show the selected Constructor and API bindings, then exit |
| `-h`, `--help` | Show command help |

---

## Common workflows

### Verify which Constructor will be used

```bash
python MCoreIMG-Reconstructor.py --show-core
```

Example diagnostic output:

```text
Codec core: /path/to/MCoreIMG-Constructor.py
Constructor build: 2026.08.02-v5.1-localspace-hybrid
Constructor version: v5.1
Protocol: 5
Source version: 5
Features: PROTO5|LOCALSPACE|HYBRID|PRIMITIVES|GROUPCOPY|ALPHA|10MSG
Envelope: 10 message(s) × 150 characters
Decoder API: module.decode_frames
Renderer API: module.render_to_pillow
Source loader: module.load_source
```

### Test Constructor/Reconstructor compatibility

```bash
python MCoreIMG-Reconstructor.py --self-test
```

The self-test attempts to use the selected Constructor's real:

1. Sample document generator
2. Encoder
3. Frame decoder
4. Renderer

It verifies that the reconstructed image has the expected canvas dimensions.

Hybrid optimization may legally change the command count, so the self-test does not require the decoded command count to equal the original authoring command count.

### Test a specific protocol and core

```bash
python MCoreIMG-Reconstructor.py \
  --self-test \
  --protocol 5 \
  --core ./MCoreIMG-Constructor.py
```

### Inspect decoded commands

```bash
python MCoreIMG-Reconstructor.py image.mci --list-commands
```

This diagnostic traversal supports both:

- Flat command lists
- Hierarchical scene or document objects

It is cycle-safe and does not attempt to reinterpret Constructor-owned group or copy semantics.

### Recover editable JSON

```bash
python MCoreIMG-Reconstructor.py image.mci --dump-json
```

This writes:

```text
image-reconstructed-YYYYMMDD-HHMMSS.mci.json
```

beside the generated PNG.

The JSON export is intended for recovery and debugging, not guaranteed restoration of the original SVG authoring document.

---

## Protocol matching

The Reconstructor reads the protocol number directly from the MCoreIMG frame header before selecting a Constructor.

The selection rule is strict:

```text
input protocol == Constructor PROTOCOL_VERSION
```

A protocol-5 frame is not decoded with a protocol-4 Constructor, even when the APIs appear similar.

For editable JSON, `protocol_version` is used when present.

For SVG files or JSON without a protocol declaration, the newest compatible Constructor is selected unless `--protocol` or `--core` is supplied.

### Compatibility policy

| Protocol | Support level |
|---|---|
| 5 | Current target |
| 4 | Best-effort through dynamic API adaptation |
| 3 | Best-effort, including alpha-aware builds |
| 2 | Best-effort for matching SVG Constructor builds |
| Other | Only when a matching compatible Constructor exposes supported APIs |

The adapter does not assume that the Constructor application version and transport protocol number are identical.

---

## Constructor discovery and ranking

When `--core` is not supplied, all viable Constructor candidates are inspected and ranked.

The ranking considers:

1. Required protocol match
2. Current protocol-5 feature coverage
3. Inferred Constructor application version
4. Preferred canonical filename
5. File modification time

For protocol 5, the current feature tokens receive preference:

```text
PROTO5
LOCALSPACE
HYBRID
PRIMITIVES
GROUPCOPY
ALPHA
10MSG
```

An older protocol-5 experiment can still be selected when it is the only compatible option, but `--show-core` may print a feature warning.

### Exact override behavior

`--core` is exclusive.

When this is supplied:

```bash
--core ./MCoreIMG-Constructor.py
```

the Reconstructor will inspect only that file. It will not silently substitute another Constructor from the same directory.

This is intentional because silent substitution can hide compatibility mistakes during development.

---

## Dynamic API compatibility

Different Constructor generations have exposed equivalent operations under different names and object layouts.

The Reconstructor uses capability discovery to support variations such as:

### Decoder locations

- Module-level function
- `codec` object
- `decoder` object
- No-argument `Codec` class
- No-argument `Decoder` class

### Example decoder names

```text
decode_frames
decode_transport_frames
decode_message_frames
decode_mci_frames
decode_transport
decode_image
reconstruct_frames
decode
```

### Example renderer names

```text
render_to_pillow
render_commands_to_pillow
render_scene_to_pillow
render_document_to_pillow
render_scene
render_document
render_image
rasterize_scene
rasterize
to_pillow
```

### Example source-loader names

```text
load_source
load_document
import_source
open_source
load_file
import_svg
```

The adapter records the selected APIs and reports them through `--show-core`.

---

## Preserving scene-level state

A decoder may return:

- A list of commands
- A document object
- A scene object
- A mapping
- A wrapper containing commands and metadata
- A tuple such as `(commands, metadata)`
- An object containing local group tables or copy-reference state

The Reconstructor preserves the raw decoded result instead of flattening it immediately.

This matters for current and future features such as:

- Local-space SVG definitions
- Group-reference tables
- Translated copies
- Palette metadata
- Primitive/vector optimization metadata
- Scene-level transforms
- Drawing-order state
- Renderer-specific context

Commands are extracted for diagnostics and fallback serialization, but the authoritative renderer receives the richest supported representation.

---

## Constructor v5.1 behavior

The current v5.1 Constructor introduces a hybrid local-space model.

### Local-space SVG groups

SVG geometry is encoded once in its own stable local coordinate system.

Position and scale are stored separately as placement transforms. Resizing an imported SVG should therefore not cause its internal path data to grow merely because its rendered dimensions changed.

### Fixed-width placement transforms

Placement transforms provide bounded encoding cost for moving and scaling groups.

This is central to the v5.1 correction for the earlier scaling problem where smaller or differently scaled SVGs could unexpectedly consume more transport space.

### Hybrid primitive and SVG representation

The Constructor can represent artwork using:

- Legacy MCoreIMG primitive commands
- General SVG/vector paths
- Local-space SVG groups
- Repeated translated group references

The encoder can compare viable representations and choose the smaller transport encoding.

### Translated group copies

Repeated artwork can reuse a previously encoded local-space group and transmit only a placement difference when the Constructor determines that doing so is smaller.

The Reconstructor delegates group-copy expansion and rendering to the matching Constructor.

### Alpha

Protocol 5 retains RGB565 color quantization with 4-bit alpha.

The Constructor renderer remains authoritative for:

- Alpha expansion
- Fill compositing
- Stroke compositing
- Source-over behavior
- Transparent overlaps
- SVG fill rules

---

## Architectural data flow

### Transport reconstruction

```text
.mci or text input
        |
        v
Extract complete MCI frames
        |
        v
Validate one shared protocol version
        |
        v
Discover matching Constructor
        |
        v
Decode through Constructor API
        |
        v
Preserve raw scene/document result
        |
        v
Render through Constructor Pillow API
        |
        v
Save PNG
```

### Source rendering

```text
.mci.json / .json / .svg / .svgz
        |
        v
Inspect optional source protocol
        |
        v
Discover matching Constructor
        |
        v
Load through Constructor source API
        |
        v
Render through Constructor Pillow API
        |
        v
Save PNG
```

---

## Frame parsing and validation

The current frame parser expects:

- `MCI` magic
- Printable ASCII transport
- A 15-character current header
- Base62 header fields
- A payload length that keeps the full message within 150 characters

The parser also retains a fallback for development transports that still store one complete frame per line but temporarily moved the payload-length field.

The Reconstructor rejects:

- Inputs containing no recognizable `MCI` frames
- Mixed protocol versions in one stream
- More frames than the selected Constructor's `MAX_MESSAGES`
- A forced `--protocol` that conflicts with the detected input protocol

Duplicate frames are removed in first-seen order.

---

## Editable JSON recovery limitations

Transport reconstruction can be visually exact while editable source recovery remains incomplete.

Information that may not survive transport includes:

- Original SVG layer names
- Editor-specific metadata
- Original XML structure
- Unused definitions
- Authoring-time grouping
- Labels not included in transport
- Pre-optimization command choices
- Primitive-versus-vector alternatives discarded by optimization

`--dump-json` follows this priority:

1. Constructor-provided `to_json()` data
2. A decoded mapping that already contains command data
3. A generic fallback containing canvas metadata and extracted commands

When scene-level local groups or repeat records remain available in the decoded object, the Constructor's own JSON representation is preferred.

---

## Troubleshooting

### Compatible Constructor not found

Example:

```text
A compatible MCoreIMG Constructor core for protocol 5 was not found.
```

Check that:

- The Constructor is beside the Reconstructor
- The Constructor declares the same `PROTOCOL_VERSION` as the input
- The Constructor imports without errors
- The Constructor exposes a supported decoder
- The Constructor exposes a supported Pillow renderer

Inspect selection directly:

```bash
python MCoreIMG-Reconstructor.py --show-core
```

Or specify the file:

```bash
python MCoreIMG-Reconstructor.py image.mci \
  --core /full/path/to/MCoreIMG-Constructor.py
```

### Rejected candidate: wrong protocol

Example:

```text
protocol 4; input requires protocol 5
```

The transport and Constructor do not match. Use the Constructor version that originally exported the transport, or re-export the image with the current Constructor.

Do not bypass this check by changing the protocol constant manually. Protocol generations may differ in opcodes, palette representation, group semantics, and frame layout.

### Constructor import failed

The final error lists rejected candidates and their import errors.

Run the Constructor directly to expose missing dependencies:

```bash
python MCoreIMG-Constructor.py
```

Common causes include:

- Missing Pillow
- Missing Tkinter
- Syntax errors in a development Constructor
- Imports that rely on files not present beside the Constructor
- Running an unsupported Python version

### No MCoreIMG frames found

Confirm the file contains complete lines beginning with:

```text
MCI
```

Do not paste truncated chat previews or wrapped messages. Each exported frame must remain complete.

### Mixed protocol versions

One input stream cannot combine frames from different protocol generations.

Remove unrelated frames and reconstruct one exported image at a time.

### Too many frames

The selected Constructor determines the maximum through `MAX_MESSAGES`.

The current target allows ten messages. A file containing more unique frames is rejected before decoding.

### Tkinter unavailable

Supply the input path explicitly:

```bash
python MCoreIMG-Reconstructor.py image.mci
```

The graphical chooser is optional.

### PNG reconstructed but did not open

The PNG has already been saved successfully. Desktop opening is only a convenience step.

Open the reported output path manually or use:

```bash
python MCoreIMG-Reconstructor.py image.mci --no-open
```

### `--dump-json` does not recreate the original SVG

This is expected when the transport did not preserve authoring metadata. Compare rendered output rather than expecting byte-for-byte restoration of the original SVG source.

### Self-test helpers unavailable

Some older Constructors do not expose a sample document or encoder suitable for the integrated round trip.

The Constructor may still reconstruct real transport successfully even when `--self-test` cannot run.

---

## Security note

A Constructor selected through `--core` or automatic discovery is imported and executed as Python code.

Only use Constructor files you trust.

Do not place untrusted Python files matching names such as:

```text
MCoreIMG-Constructor*.py
MCoreIMG-SVG-Constructor*.py
```

beside the Reconstructor or in the working directory.

MCoreIMG transport data is treated as data, but the Constructor core is executable code.

---

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Graphical file selection cancelled |
| `2` | Reconstruction, compatibility, input, or rendering error |
| `130` | Interrupted with Ctrl-C |

These stable exit codes make the Reconstructor suitable for scripts and future application integration.

Example:

```bash
python MCoreIMG-Reconstructor.py image.mci --no-open
status=$?

if [ "$status" -eq 0 ]; then
    echo "Reconstruction succeeded"
else
    echo "Reconstruction failed with code $status"
fi
```

---

## Maintenance guide

The code is divided into documented technical-debt boundaries:

1. Protocol constants and API-name registries
2. Constructor discovery and ranking
3. Dynamic API inspection
4. Safe version-dependent invocation
5. Decoded scene normalization
6. Frame extraction and protocol detection
7. JSON recovery
8. Diagnostic command traversal
9. Integrated self-testing
10. CLI orchestration and error handling

When adapting the Reconstructor to a future Constructor:

### 1. Identify the transport protocol

Check:

```python
PROTOCOL_VERSION
SOURCE_VERSION
CONSTRUCTOR_BUILD
FEATURE_SIGNATURE
MAX_MESSAGES
MESSAGE_LEN
```

### 2. Add preferred filenames only when useful

The broad glob search already finds most versioned Constructor names.

Add a filename to `PREFERRED_CORE_FILENAMES` when it should receive a canonical ranking bonus.

### 3. Extend API registries before adding special cases

When a Constructor renames a public operation, first update the appropriate registry:

- `DECODER_NAMES`
- `HIGH_LEVEL_RENDER_NAMES`
- `RENDERER_NAMES`
- `SOURCE_LOADER_NAMES`
- `ENCODER_NAMES`
- `OWNER_NAMES`

Capability discovery is preferable to protocol-specific branching.

### 4. Preserve the raw decoder result

Do not replace the scene or document with only a command list unless the Constructor itself returns only commands.

Future renderers may depend on scene-level tables and metadata.

### 5. Keep the Constructor renderer authoritative

Do not duplicate:

- Alpha compositing
- SVG path filling
- Fill rules
- Palette expansion
- Group placement
- Primitive rendering
- Copy-reference expansion

unless the protocol is intentionally being split into an independent decoder library.

### 6. Update feature-signature ranking

When a new current protocol becomes authoritative, update:

- `PREFERRED_PROTOCOL_VERSION`
- Preferred filenames
- Current feature-token set
- Build documentation
- Self-test expectations

### 7. Test both API styles

At minimum, test:

- Flat command-list decoder output
- Scene/document decoder output
- Tuple `(commands, metadata)` output
- Module-level APIs
- Object/class-owned APIs
- Explicit `--core`
- Automatic discovery
- Protocol mismatch rejection
- PNG output
- RGBA alpha output
- JSON export
- Self-test

---

## Design invariants

Future maintenance should preserve these rules:

1. Never decode protocol N with a Constructor declaring another protocol.
2. Prefer a verified current feature signature among same-protocol candidates.
3. Treat `--core` as an exact override.
4. Preserve the raw decoded scene until rendering and export are complete.
5. Prefer the Constructor's renderer over local drawing reimplementation.
6. Do not hide real codec errors as argument-signature fallbacks.
7. Keep frame extraction strict enough to reject unrelated text.
8. Retain one-frame-per-line compatibility for development transports.
9. Use the Constructor's own message limit.
10. Exercise the real encoder, decoder, and renderer during self-test when available.

---

## Intended role in MCoreIMG

The Reconstructor is the receiving-side reference application for the MCoreIMG image transport workflow:

```text
Artwork
  -> Constructor
  -> MCoreIMG transport frames
  -> MeshCore messages
  -> Reconstructor
  -> PNG
```

Its priorities are:

1. Correct protocol matching
2. Exact rendering parity with the Constructor
3. Resilience across Constructor API refactors
4. Useful diagnostics during rapid protocol development
5. Minimal duplicated codec logic
6. Clear failure messages instead of silent corruption

The Reconstructor is not intended to be a general-purpose SVG editor or a substitute for the Constructor's authoring interface.

---

## Example complete workflow

Export transport from the Constructor, then copy both files into the same directory:

```text
MCoreIMG-Constructor.py
MCoreIMG-Reconstructor.py
my-image.mci
```

Verify compatibility:

```bash
python MCoreIMG-Reconstructor.py --show-core
```

Run the codec round trip:

```bash
python MCoreIMG-Reconstructor.py --self-test
```

Reconstruct the image:

```bash
python MCoreIMG-Reconstructor.py my-image.mci --no-open
```

Inspect the decoded representation and create recovery JSON:

```bash
python MCoreIMG-Reconstructor.py my-image.mci \
  --list-commands \
  --dump-json \
  --output my-image-reconstructed.png \
  --no-open
```

Expected results:

```text
my-image-reconstructed.png
my-image-reconstructed.mci.json
```

---

## Project files

```text
MCoreIMG-Reconstructor.py
README.md
```

The matching Constructor is maintained separately but must be available at runtime.

---

## License

Use the same license selected for the main MCoreIMG repository. Add the repository's license file at the project root so the Constructor and Reconstructor remain under one consistent licensing policy.
