# MCoreIMG Reconstructor

The receiving half of MCoreIMG. Turns transport frames, or an editable source
file, back into a raster image.

> **Build:** `2026.08.05-reconstructor-v6.0-SLIMHEADER`
> **Depends on:** `MCoreIMG-compression.py` for decoding, `MCoreIMG-Constructor.py` for rendering

The Reconstructor contains **no codec of its own**. Decoding is done by the
compression module and rendering by the Constructor, so the receiver can never
drift from the transmitter.

---

## How this differs from earlier builds

Previous versions could not simply import the codec, because the Constructor was
a single file with several historical implementation layers and no stable public
API. To cope, this program carried roughly seven hundred lines of reflection: it
scanned candidate files, ranked them by a feature score, searched modules and
classes for anything resembling a decoder or renderer, and tried several calling
conventions until one bound successfully.

The codec is now an importable module with an explicit `__all__`. All of that
discovery machinery is gone.

The failure it was protecting against — a receiver paired with the wrong
transmitter — is now caught directly by comparing protocol numbers and reporting
every build string involved.

---

## Requirements

### Required

- Python 3.10 or newer
- `MCoreIMG-compression.py` and `MCoreIMG-model.py` for decoding
- `MCoreIMG-Constructor.py` and Pillow for rendering

### Optional

- Tkinter/Tk — only for the interactive file chooser when no input path is
  given on the command line

Decoding and JSON export work without Pillow or a display when `--no-render` is
used, which makes the Reconstructor usable on a headless relay host.

---

## File layout

Keep all four modules together:

```
mcoreimg/
├── MCoreIMG-model.py
├── MCoreIMG-compression.py
├── MCoreIMG-Constructor.py
└── MCoreIMG-Reconstructor.py
```

Each lookup falls back to the directory beside this file, then the working
directory, and can be overridden with `--compression` or `--constructor`.

---

## Quick start

### Reconstruct a frame file

```bash
python MCoreIMG-Reconstructor.py received.mci
```

Decodes, validates, renders a PNG beside the input, and opens it.

### Choose the input interactively

```bash
python MCoreIMG-Reconstructor.py
```

### Decode without rendering

```bash
python MCoreIMG-Reconstructor.py received.mci --no-render --list-commands
```

### Recover editable source

```bash
python MCoreIMG-Reconstructor.py received.mci --dump-json
```

### Verify the installed set

```bash
python MCoreIMG-Reconstructor.py --show-core
python MCoreIMG-Reconstructor.py --self-test
```

---

## Command-line options

| Option | Purpose |
|---|---|
| `input` | `.mci`, text, `.mci.json`, `.json`, `.svg`, or `.svgz` file |
| `-o`, `--output PATH` | Output PNG path |
| `--compression PATH` | Path to `MCoreIMG-compression.py` |
| `--constructor PATH` | Path to `MCoreIMG-Constructor.py` |
| `--protocol N` | Require a specific protocol number |
| `--dump-json` | Write a Constructor-compatible source JSON |
| `--list-commands` | Print the decoded command stream |
| `--no-render` | Decode only; do not produce a PNG |
| `--no-open` | Do not automatically open the PNG |
| `--self-test` | Run codec and rendering round-trip tests |
| `--show-core` | Print the loaded modules and exit |

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Interactive chooser cancelled |
| 2 | User-facing reconstruction or compatibility error |
| 130 | Interrupted with Ctrl-C |

---

## Supported input

### MCoreIMG transport

A `.mci` file with one frame per line, or any text containing complete frames.
Frames are located by their `MCI` magic and validated individually, so pasted
chat transcripts with surrounding conversation work as input.

### Editable MCoreIMG source

`.mci.json` and `.json` files are handed to the Constructor's source loader and
rendered directly. This path never touches the codec.

### SVG source

`.svg` and `.svgz` files are imported through the Constructor and rendered,
which is useful for previewing what artwork will look like after transport.

---

## Data flow

### Transport path

```
input text/.mci
    -> extract and validate complete MCI frames
    -> check the protocol number in the frame header
    -> mci.decode_frames(frames)          CRC and envelope validation
    -> ctor.render_to_pillow(commands)
    -> save PNG, optionally export editable JSON
```

### Source path

```
.mci.json / .json / .svg / .svgz
    -> ctor.load_source(path)
    -> document.transformed_commands()
    -> ctor.render_to_pillow(commands)
```

---

## Frame parsing and validation

Frame text is scanned before a codec is chosen, so the program keeps four
bootstrap constants (`FRAME_MAGIC`, `BASE62`, header length of 8, message
length).
These are **verified against the codec** as soon as it loads. If the transport
profile ever changes, the mismatch is reported rather than silently tolerated.

`extract_frames` recovers complete frames from noisy text, deduplicates them,
and confirms every frame declares the same protocol.

Protocol 6 has no length field, so frame boundaries are found differently:
chunking fills every frame except the last, so a non-final frame is always
exactly 150 characters and can be sliced out of a line containing several. A
final frame runs to the end of its line. The part descriptor in the header says
which kind it is.

Decoding then rejects frames that are the wrong length, carry the wrong magic or
protocol, declare an out-of-range index, disagree about the coding mode, carry a
short non-final payload, come from a different image, conflict with a duplicate,
leave a gap in the sequence, declare two different final frames, or fail the
stream CRC-32.

There is no per-frame CRC in protocol 6. MeshCore guarantees the integrity of a
delivered message, so the per-frame check was redundant on that path. Frames
pasted from a chat transcript leave MeshCore's protection, and for those the
stream CRC-32 still detects corruption — it just reports that the set is bad
rather than naming the offending frame.

---

## Protocol matching

Three protocol numbers must agree: the one in the input frames, the one the
codec implements, and the one the Constructor was built against.

- Input versus codec is checked before decoding.
- Codec versus Constructor is checked at load time.
- `--protocol N` adds an explicit assertion for scripted use.

Any mismatch produces exit code 2 and an error naming the files and their build
strings. For example:

```text
Reconstruction failed:
Input needs protocol 5, but the codec beside this file is protocol 6.
  Codec: /path/to/MCoreIMG-compression.py
Pair this Reconstructor with the matching codec, or pass --compression.
```

Transports older than protocol 6 are not decodable by this build, including
protocol 5: the frame header, point predictor, repeat records, and entropy coder
all changed. Use a Reconstructor and codec pair from the matching generation.

---

## Module loading

Both siblings use hyphenated filenames, which are not legal Python identifiers,
so they are loaded by path.

Import order matters. The codec is loaded first and registered in `sys.modules`;
the Constructor then reuses that same module object rather than creating a
second one. This is deliberate — two copies would define two distinct
`VectorCommand` classes, and objects decoded by one would not be recognised by
the other.

`--no-render` makes the Constructor optional, so a headless host can decode and
export JSON with only the codec and model present.

---

## Self-test

```bash
python MCoreIMG-Reconstructor.py --self-test
```

Six checks:

1. A simple document survives a full frame round trip.
2. Frames are recovered from surrounding chat noise.
3. A corrupted frame is rejected rather than silently rendered.
4. An incomplete frame set is rejected.
5. The protocol number in the header matches the codec.
6. Rendering produces a canvas-sized image, when a Constructor is present.

Successful output:

```text
MCoreIMG Reconstructor self-test: PASS (6 checks)
protocol=6 frames=1 commands=2
```

---

## Output

### PNG

Written beside the input unless `-o` is given, and opened automatically unless
`--no-open` is passed. The image is rendered by the Constructor's authoritative
Pillow renderer, so it matches what the sender previewed.

### Editable JSON

`--dump-json` writes a Constructor-compatible source document containing the
decoded commands plus a `reconstructed` block recording the Reconstructor build,
the timestamp, the input filename, and the codec build.

This is a best-effort recovery. Rendering can be lossless even when authoring
metadata — editor grouping, layer labels, the original document transform —
cannot be recovered, because those are not transmitted.

The result loads back into the Constructor and re-encodes to the same frames.

---

## Module structure

| Section | Contents |
|---|---|
| Bootstrap frame constants | The few values needed before a codec is chosen |
| Module loading | Path-based import, pairing checks, `Core` container |
| Frame text handling | Base62 headers, frame extraction, protocol detection |
| Input classification | Transport versus source, output paths, opening files |
| JSON export | Best-effort editable source recovery |
| Decode, render, export | The actual pipeline |
| Self-test | Round-trip and corruption checks |
| Command line | Parser, diagnostics, orchestration |

---

## Development notes

- The Reconstructor must never implement decoding. If it needs a codec
  behaviour it does not have, add it to `MCoreIMG-compression.py` and export it.
- Bootstrap constants are the one permitted duplication, and only because frame
  scanning must precede codec selection. They are checked against the codec at
  load time; keep that check working.
- Prefer clear pairing errors over silent fallbacks. The whole point of the
  rewrite was replacing guesswork with explicit checks.
