# MCoreIMG Architecture

How the four modules fit together, why the boundaries sit where they do, and
what the rules are for changing them.

This document is for maintainers. For usage, see the per-module READMEs.

---

## Design goal

MCoreIMG has one hard constraint that shapes everything else:

> An image must fit in **ten 150-character text messages**.

That budget is small enough that compression decisions dominate the design. Most
architectural choices here exist to make compression changes safe, isolated, and
verifiable.

The secondary goal is that the sender and the receiver can never disagree. A
protocol this dense fails silently when two halves drift apart, so the
architecture removes opportunities for drift rather than trying to detect it
after the fact.

---

## Layering

Four modules in a strict chain. Each layer may import the ones above it and
never the ones below.

```
┌──────────────────────────────────────────────┐
│  MCoreIMG-model.py                           │
│  Vocabulary: opcodes, commands, geometry,    │
│  colour quantization, primitives             │
│  Imports: stdlib only                        │
└──────────────────────────────────────────────┘
                     ▲
┌──────────────────────────────────────────────┐
│  MCoreIMG-compression.py                     │
│  Codec: bit IO, records, palette assembly,   │
│  local-space groups, Base91, framing         │
│  Imports: model                              │
└──────────────────────────────────────────────┘
                     ▲
┌──────────────────────────────────────────────┐
│  MCoreIMG-Constructor.py                     │
│  Authoring: SVG import, rendering, GUI       │
│  Imports: model, compression                 │
└──────────────────────────────────────────────┘
                     ▲
┌──────────────────────────────────────────────┐
│  MCoreIMG-Reconstructor.py                   │
│  Receiving: decode, render, export           │
│  Imports: model, compression, constructor    │
└──────────────────────────────────────────────┘
```

### Verified properties

These are checked mechanically and should stay true:

| Property | Status |
|---|---|
| Model imports nothing but stdlib | yes |
| Model contains no bit IO or framing symbols | yes |
| Model contains no GUI symbols | yes |
| Compression contains no GUI or SVG parsing | yes |
| Constructor defines no `encode_commands` or `BitWriter` | yes |
| No shadowed top-level definitions in any module | yes, all four |

---

## Where the boundaries sit, and why

### Model versus compression

The dividing question is: **does the transport depend on this behaviour, or does
this behaviour depend on the transport?**

`quantize_command` lives in the **model** even though it is obviously
transport-flavoured. The encoder, the preview renderer, and the receiver must
all round identically, or the sender's preview stops matching what arrives. It
is shared vocabulary, not codec machinery.

`build_palette` lives in **compression** because assembling an ordered table and
enforcing a 32-entry cap is a codec policy. The colour quantization it calls
(`quantize_palette_color`) is model.

`flatten_path` lives in the **model**, which surprises people who assume it is a
renderer helper. `command_points` needs it to compute path bounding boxes, and
those bounding boxes drive local-space normalization — so it is a transport
dependency.

`geom_translation` lives in **compression**. It compares commands at transport
precision to decide whether a repeat record is legal, which is a codec question
about a codec optimization.

### Compression versus Constructor

Anything that produces or consumes bits is compression. Anything that produces
or consumes pixels or user input is Constructor.

The interesting case is `primitive_to_vectors`, which lives in the model and has
two callers with different motives. The renderer uses it to draw. The encoder
uses it to *price* a primitive against its expansion. Both need identical
output, so it belongs to shared vocabulary.

### Constructor versus Reconstructor

The Reconstructor imports the Constructor purely for rendering and source
loading. It does not import it for anything protocol-related.

This means a headless receiver can run with `--no-render` using only the model
and the codec, which is the reason the Constructor dependency is optional.

---

## Data flow

### Authoring

```
SVG / XML or editable JSON
    → VectorDocument
    → ordered VectorCommand objects
    → editor/document transforms
    → primitive representation planning        (compression)
    → local-space SVG group planning           (compression)
    → palette construction, RGB565 + A4        (compression)
    → record selection and bit writing         (compression)
    → stream CRC-32
    → Base91 payload
    → MeshCore frames with per-frame CRC-16
```

### Receiving

```
frame text
    → frame extraction and per-frame validation
    → reassembly and stream CRC-32 check
    → Base91 decode
    → record parsing and state reconstruction  (compression)
    → ordered VectorCommand objects
    → Pillow render                            (constructor)
    → PNG, optionally editable JSON
```

### Preview parity

The Constructor previews the **decoded** command stream, not the source
document, whenever the image fits the budget. The screen therefore shows palette
quantization, alpha quantization, coordinate precision, and protocol
reconstruction exactly as a receiver will see them.

This is why rounding rules must be shared rather than duplicated: preview parity
is a correctness property, not a nicety.

---

## Protocol v5 concepts

### Local-space SVG groups

The defining feature of v5, and the reason the codec is structured the way it
is.

Earlier branches baked displayed size into every coordinate. Smaller artwork
produced smaller deltas and therefore fewer bits, even for identical geometry.
Resizing an SVG changed its transmission cost, which made the ten-message budget
unpredictable.

v5 separates the two concerns:

```
local SVG geometry                (encoded once, in a stable local box)
        +
canvas placement and scale        (a fixed-width transform record)
```

Because the placement fields are fixed width, moving or resizing changes their
*values* but never their *length*. The same drawing costs the same bits at 100%,
50%, or 10% display scale.

Duplicated SVGs reuse the local definition and send only a new transform, which
is why the encoder actively looks for repeated group signatures.

### Editor groups as encoder hints

`editor_group` is the only piece of purely editorial metadata on a command. The
GUI uses it to move an imported SVG as one object. The encoder uses it to
identify contiguous runs that are candidates for local-space groups.

It is never transmitted. Keeping an imported group contiguous in draw order is
what gives the encoder its opportunity to reuse geometry.

### Contextual primitive pricing

Compact primitives are not unconditionally cheaper than their vector expansion.
The encoder simulates both against live encoder state and picks the smaller one,
so the same primitive can win in one image and lose in another.

This is why primitive expansion is shared vocabulary rather than renderer code.

### Precision search

`encode_image` walks a ladder of local-coordinate extents from 255 down to 32
and returns the highest precision that still fits ten messages. Images without
eligible local groups skip the search.

The chosen precision depends on geometry complexity, never on displayed scale.

---

## Module loading

All four filenames contain hyphens, which are not legal Python identifiers, so
siblings are loaded by path with `importlib.util.spec_from_file_location`.

### Single-instance rule

A module is registered in `sys.modules` under a canonical name
(`mcoreimg_model`, `mcoreimg_compression`) and reused if already present.

This matters more than it looks. Two copies of the model would define two
distinct `VectorCommand` classes. Objects decoded through one would not be
recognised by the other, and the failure would appear as a mysterious
`isinstance` mismatch far from its cause.

The Reconstructor loads the codec first, then the Constructor, which finds the
already-loaded codec rather than creating a second one.

### Lookup order

1. An explicit `--compression` / `--constructor` path
2. Beside the importing file
3. The current working directory

Failures list every path searched.

---

## Refactor history and rationale

The current structure replaced a two-file build in which the Constructor was
5,067 lines containing several historical implementation layers.

### What was wrong

Python resolves top-level names by last definition. The old Constructor relied
on that: `encode_commands`, `decode_commands`, `encode_image`, `CodecStats`,
`ConstructorApp`, and `run_self_test` each had two or three definitions, and only
the last was live.

**1,054 lines were dead** across 19 shadowed names. Worse, the distinction was
not visible from reading: four functions at lines 784–916 looked superseded but
were kept alive by `_v3_*` alias assignments 1,500 lines later, while four other
aliases were genuinely unused.

The Reconstructor carried roughly 700 lines of reflection — candidate scanning,
feature-score ranking, callable discovery, argument-binding attempts — whose
only purpose was coping with the fact that the Constructor had no stable
importable API.

### What changed

| Change | Effect |
|---|---|
| Extracted the codec into its own module | Compression changes touch one file |
| Extracted the model beneath it | Codec changes cannot disturb shared vocabulary |
| Deleted all shadowed definitions | Zero in all four modules |
| Replaced `_v3_*` aliases with explicit `_generic_*` dispatch | No function kept alive by a bare assignment |
| Threaded local precision as a parameter | Removed a mutated module global; codec is now re-entrant |
| Deleted the reflection layer | Reconstructor dropped from 1,713 to 813 lines |
| Named the three GUI classes distinctly | No more three definitions of `ConstructorApp` |
| Rewrote the integrity guard | Now checks module pairing, the real failure mode |

Line counts: 6,780 across two files became 5,746 across four.

### How it was verified

Golden fixtures were captured from the original build **before** any change: 12
cases covering every opcode, all 12 primitives, alpha compositing, single and
group repeats, local-space groups, real SVG imports, a 10-frame stress case, and
boundary conditions. Each records SHA-256 of transport bytes, full frame text,
bit counts, codec metrics, decoded command structure, and rendered raster
hashes.

Code was moved **verbatim** using an AST extractor rather than retyped, so
transcription could not silently alter behaviour.

Results: 120/120 fixture checks bit-identical, 29/29 integration checks passing,
self-test output character-for-character unchanged, and frames produced by the
original build still decode and render pixel-identically.

---

## Remaining technical debt

### GUI inheritance chain

The Constructor still has three cooperating application classes rather than one:

```
_DocumentAppBase  →  _DrawingAppBase  →  ConstructorApp
```

They now have distinct names and inherit explicitly, which removes the shadowing
problem. Flattening them into a single class is still worthwhile — there are 13
overridden methods and 8 `super()` call sites — but it requires interactive GUI
testing with a display, which no automated fixture currently covers.

Do not attempt it without a GUI test plan.

### Greedy compression planning

Record selection is contextual and greedy rather than globally optimal. A better
plan may exist for a given image. Improving this is safe from an architecture
standpoint because it is entirely inside the codec, but it changes emitted bits
and is therefore a protocol change.

### Exact-translation-only repeat detection

Repeat detection requires exact translation. Rotated or scaled copies are only
handled inside local-space groups.

### Opcode-dependent geometry dictionaries

Command geometry is stored in `Dict[str, Any]` keyed by opcode rather than in
dedicated dataclasses. This is compact and serializes easily, but it means the
schema is enforced by `validate_command` rather than by the type system.

---

## Rules for changing things

### Do not import upward

The model must never import the codec. The codec must never import the
Constructor. Violating this creates a cycle that will only surface at import
time in some configurations.

### Bit changes are protocol changes

If unchanged input produces different bits, bump `PROTOCOL_VERSION` and update
both ends in the same commit. This includes rounding-rule changes in the model,
which are easy to underestimate.

### Mirror every writer with its reader

A change to a writer must be mirrored in its reader, and both must be exercised
by round-trip fixtures.

### Separate structural changes from format changes

Do not combine a refactor with a protocol change. Those are two different review
problems, and mixing them makes it impossible to tell whether a fixture
difference is intentional.

### Refactor against frozen fixtures

Capture golden transport bytes and rendered rasters before restructuring, and
confirm they are unchanged afterwards. The codebase has a history of behaviour
hiding in surprising places; fixtures are the only reliable defence.

### Keep the codec re-entrant

Local-coordinate precision is passed as an argument, not held in module state.
An earlier build mutated a global here. Do not reintroduce mutable module-level
configuration — it makes the codec unsafe to use concurrently or as a library
service.

---

## Verification commands

```bash
# identity and pairing
python MCoreIMG-Constructor.py --version
python MCoreIMG-Reconstructor.py --show-core

# regression suites
python MCoreIMG-Constructor.py --self-test
python MCoreIMG-Reconstructor.py --self-test

# end-to-end round trip
python MCoreIMG-Reconstructor.py sample.mci --list-commands --dump-json --no-open
```

A healthy installation reports the same protocol number from every module and
matching `-v5.2-MODULAR` build suffixes.
