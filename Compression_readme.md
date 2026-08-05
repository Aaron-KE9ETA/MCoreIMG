# MCoreIMG Compression

The transport codec. Everything that turns drawing commands into radio bytes
and back again.

`MCoreIMG-compression.py` owns bit writing, record selection, palette assembly,
local-space group planning, Base91 payload coding, and MeshCore framing. No
other module writes a bit or chooses a record type.

> **Build:** `2026.08.05-compression-v5.2-MODULAR`
> **Protocol:** 5
> **Depends on:** `MCoreIMG-model.py`

If you are changing how images are compressed, this is the only file you need
to edit.

---

## Public API

### Full pipeline

```python
encode_image(commands) -> EncodedImage
```

Plan, compress, and frame. Searches the precision ladder for the highest
local-coordinate fidelity that fits ten messages.

```python
decode_frames(frames) -> list[VectorCommand]
```

Validate the envelope, check CRCs, reassemble, and decompress.

### Bitstream only

```python
encode_commands(commands, local_extent=LOCAL_GROUP_EXTENT)
    -> (payload_bytes, bit_count, metrics, palette)

decode_commands(data) -> (list[VectorCommand], palette)
```

For tests and tooling that bypass framing.

### Framing primitives

`base91_encode`, `base91_decode`, `enc62`, `dec62`, `frame_crc` — exposed for
frame-repair tooling.

### Re-exports

Importing the codec yields the full model vocabulary as well: `VectorCommand`,
`PaintStyle`, all opcodes and primitives, geometry helpers, and colour
quantization. One import gives you everything needed to build and encode an
image.

---

## Stream layout

```
[4 bits]   protocol version
[ue]       palette_count - 1
[16 + 4 bits] * palette_count    RGB565 + 4-bit alpha
[ue]       command_count
record*
```

Every record opens with a two-bit tag:

| Tag | Name | Meaning |
|---|---|---|
| 0 | `REC_NORMAL` | A fully specified command |
| 1 | `REC_SINGLE_REPEAT` | Re-emit one earlier command at a delta |
| 2 | `REC_GROUP_REPEAT` | Re-emit a contiguous run at a delta |
| 3 | `REC_TRANSFORM_GROUP` | A local-space SVG group |

The compressed stream is followed by a CRC-32, Base91-encoded, and split across
frames.

---

## Compression techniques

### Palette

Every fill and stroke colour is quantized to RGB565 with 4-bit alpha and
collected into an ordered table, capped at 32 entries (`MAX_PALETTE`). Commands
reference palette indices at `max(1, (len(palette)-1).bit_length())` bits.

`build_palette` raises `MCIError` when an image needs more colours than the
protocol supports.

### Opcode-local state

Point and style state are tracked **per opcode**, not globally. A rectangle
following a run of ellipses still deltas against the previous rectangle. This
matters because artwork tends to cluster by shape type.

`PointState` holds the last coordinate; style state holds the last `PaintStyle`
per opcode. Normal records carry a one-bit same-opcode shortcut.

### Point coding

Each point chooses between:

- **Delta** — Rice-coded signed offsets from the previous point for this opcode
- **Absolute** — 10-bit x and 9-bit y, sized from the 720 × 480 canvas

The writer measures both and picks the shorter. `rice_signed_length` gives the
cost without writing.

Counts and lengths use unsigned Exp-Golomb (`ue`).

### Single translated repeat

When a command is an exact translated copy of an earlier one — same opcode,
same style, same geometric structure — the encoder emits a back-reference and a
delta instead of the geometry.

`geom_translation` proves the relationship. It compares flattened point sets,
then re-translates the earlier command and requires the geometry dictionaries
to match exactly, so a coincidental point-count match cannot produce a false
positive.

### Translated group repeat

The same idea across a contiguous run of commands. `_best_group_repeat` searches
for the longest earlier run that repeats at a constant offset and emits one
record covering all of it.

### Local-space SVG groups

The protocol-v5 feature that makes display scale free.

A contiguous run of commands sharing an `editor_group` is normalized into a
stable local coordinate box, then transmitted as:

```
fixed-width display box (x, y, width, height, local_width, local_height)
        +
either an inline local definition or a back-reference to an earlier one
```

The display box is 10 + 9 + 10 + 9 + 8 + 8 bits. Fixed width is deliberate:
moving or resizing a group changes the *values* but never the field *length*.

Because geometry is encoded in local space, the same artwork costs the same
number of bits at 100%, 50%, or 10% display scale. Duplicating an SVG reuses
the definition and sends only a new transform.

Minor Base91 character-count differences can still occur from byte padding, but
geometry cost and frame count stay stable.

### Primitive pricing

`_plan_primitive_representations` walks the command list and, for every compact
primitive, simulates both representations against the current encoder state:

1. the compact primitive record, and
2. the equivalent generic vector expansion.

The primitive is used only when it is **strictly smaller**. Because the
comparison runs against live state, the same primitive can win in one image and
lose in another.

### Precision ladder

```python
PRECISION_LADDER = (255, 224, 192, 160, 144, 128, 112, 96,
                    80, 72, 64, 56, 48, 40, 32)
```

`encode_image` encodes at each extent in turn and returns the first result that
fits ten messages. Images without eligible local groups skip the search
entirely. If nothing fits, the smallest candidate is returned so the caller can
report an honest over-limit result.

The chosen precision depends on geometry complexity, never on displayed scale.

---

## MeshCore framing

### Envelope

| Property | Value |
|---|---|
| `MAX_MESSAGES` | 10 |
| `MESSAGE_LEN` | 150 |
| `FRAME_HEADER_LEN` | 15 |
| `FRAME_PAYLOAD_LEN` | 135 |
| `MAX_PAYLOAD_CHARS` | 1,350 |

### Header

15 characters, all Base62:

| Field | Width | Meaning |
|---|---|---|
| magic | 3 | `MCI` |
| version | 1 | Protocol number |
| image id | 3 | CRC-derived stream identifier |
| index | 1 | Zero-based part number |
| total | 1 | Total part count |
| length | 2 | Payload characters in this frame |
| crc | 3 | CRC-16 of this frame's payload |
| reserved | 1 | `0` |

### Alphabets

`BASE91` excludes `"`, `'`, and `\` so frames survive text transports and shell
quoting. `BASE62` carries the fixed-width header fields.

### Validation

`decode_frames` rejects frames that are the wrong length, carry the wrong magic
or protocol, declare an out-of-range part count, fail their CRC-16, come from a
different image, conflict with a duplicate, leave a gap in the sequence, or fail
the stream CRC-32. Every failure raises `FrameError`.

---

## Results

### `EncodedImage`

| Field | Meaning |
|---|---|
| `raw` | Compressed bytes plus stream CRC-32 |
| `payload` | Base91 text |
| `frames` | Complete MeshCore messages |
| `stats` | `CodecStats` |
| `image_id` | Stream identifier |
| `palette` | Ordered colour table |

### `CodecStats`

| Field | Meaning |
|---|---|
| `command_count` | Commands actually transmitted |
| `source_command_count` | Commands before planning |
| `palette_count` | Colours used |
| `bit_count` | Compressed size in bits |
| `packed_bytes` | Bytes including stream CRC |
| `base91_chars` | Payload characters |
| `frame_count` | Messages required |
| `repeat_count` | Single plus group repeats |
| `group_repeat_count` | Group repeats and transform references |
| `primitive_count` | Primitives kept compact |
| `vectorized_primitive_count` | Primitives expanded to vectors |
| `transformed_group_count` | Local-space groups emitted |
| `transformed_group_reference_count` | Reused local definitions |
| `local_group_extent` | Precision the encoder settled on |

`stats.fits` reports whether the image is within the ten-message budget.

---

## Editing this file

**Mirror every change.** A change to a writer must be mirrored in its reader,
and both must be exercised by round-trip fixtures.

**Bit changes are protocol changes.** If unchanged input produces different
bits, bump `PROTOCOL_VERSION` and update the Reconstructor in the same commit.

**Keep it re-entrant.** Local-coordinate precision is passed as an argument
through `encode_commands`, not held in module state. Earlier builds mutated a
global here; that was removed so the codec is safe to call concurrently or as a
library service. Do not reintroduce mutable module-level configuration.

**Do not import upward.** The codec may import the model. It must never import
the Constructor or the Reconstructor.

### Verifying a change

Freeze fixtures before refactoring, then confirm the bytes are unchanged:

```python
data, bits, metrics, palette = mci.encode_commands(commands)
hashlib.sha256(data).hexdigest()
```

Both self-tests must pass:

```bash
python MCoreIMG-Constructor.py --self-test
python MCoreIMG-Reconstructor.py --self-test
```

---

## Known limitations

- Compression planning is contextual and greedy rather than globally optimal.
  A better plan may exist for a given image.
- Repeat detection requires exact translation. Rotated or scaled copies are not
  detected outside local-space groups.
- The 32-colour palette is a hard protocol limit, not a tunable.
