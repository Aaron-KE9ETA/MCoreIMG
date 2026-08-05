# MCoreIMG Compression

The transport codec. Everything that turns drawing commands into radio bytes
and back again.

`MCoreIMG-compression.py` owns geometry simplification, bit modelling, entropy
coding, record selection, palette assembly, local-space group planning, Base91
payload coding, and MeshCore framing. No other module writes a bit or chooses a
record type.

> **Build:** `2026.08.05-compression-v6.0-SLIMHEADER`
> **Protocol:** 6
> **Depends on:** `MCoreIMG-model.py`

If you are changing how images are compressed, this is the only file you need
to edit.

---

## Public API

### Full pipeline

```python
encode_image(commands) -> EncodedImage
```

Simplify, plan, compress, and frame. Searches the precision ladder for the
highest local-coordinate fidelity that fits ten messages, and chooses between
raw and entropy coding.

```python
decode_frames(frames) -> list[VectorCommand]
```

Validate the envelope, reassemble, check the stream CRC, and decompress.

### Bitstream only

```python
encode_commands(commands, local_extent=LOCAL_GROUP_EXTENT)
    -> (payload_bytes, bit_count, metrics, palette)

decode_commands(data, mode=1) -> (list[VectorCommand], palette)
```

`mode` selects raw (`0`) or entropy-coded (`1`). Frames carry it in the header;
call `decode_commands` directly only with the mode reported in
`stats.coding_mode`.

### Encoder-side tools

`simplify_commands`, `apply_symmetry`, `geom_symmetry`, `build_palette`,
`geom_translation`.

### Framing primitives

`base91_encode`, `base91_decode`, `enc62`, `dec62` — exposed for frame-repair
tooling.

### Re-exports

Importing the codec yields the full model vocabulary as well: `VectorCommand`,
`PaintStyle`, all opcodes and primitives, geometry helpers, and colour
quantization.

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
| 1 | `REC_SINGLE_REPEAT` | Re-emit one earlier command under a symmetry and delta |
| 2 | `REC_GROUP_REPEAT` | Re-emit a contiguous run at a delta |
| 3 | `REC_TRANSFORM_GROUP` | A local-space SVG group |

The compressed stream is followed by a CRC-32, Base91-encoded, and split across
frames.

---

## Compression techniques

Roughly in the order the encoder applies them.

### Geometry simplification

Transport coordinates are integers. A vertex that rounds onto the same pixel as
its neighbour, or that sits within half a pixel of the line between them,
cannot change the decoded image — so removing it is **lossless at transport
precision**. No quality trade-off, no search, and the decoder is unaffected.

`SIMPLIFY_TOLERANCE` defaults to `0.5`, the lossless boundary. Raising it is not
a protocol change; it only trades fidelity for size.

This matters because coordinates dominate the payload, and real SVG — traced
logos, map exports, illustrator output — carries far more vertices than a
720 × 480 canvas can resolve. Measured on synthetic traced artwork:

| Input | Vertices removed | Payload | Max deviation |
|---|---|---|---|
| 400-point traced outline | 80% | −73% | 0.49 px |
| 1200-point traced outline | 93% | −91% | 0.48 px |
| Collinear axis-aligned outline | 83% | −69% | 0.00 px |

Structural minimums are always honoured: a polygon keeps three points and a
polyline two. Bézier control points are never dropped, because a control point
is structural rather than a sample — only maximal runs of `LineTo` are reduced.

### Palette

Every fill and stroke colour is quantized to RGB565 with 4-bit alpha and
collected into an ordered table, capped at 32 entries (`MAX_PALETTE`).

### Opcode-local state

Point and style state are tracked **per opcode**, not globally, because artwork
clusters by shape type. A rectangle following a run of ellipses still predicts
from the previous rectangle.

Normal records carry one-bit same-opcode and same-style shortcuts.

### Point prediction

Each point is coded as a residual against a prediction, or absolutely when that
is cheaper. Two predictors run in parallel:

- **first order** — the previous point for this opcode
- **second order** — linear extrapolation, `2·p[n-1] − p[n-2]`

Second order tracks flattened Béziers and traced outlines far better, because
those advance in a roughly constant direction.

**The choice costs no bits.** Both ends keep decayed absolute-error scores for
the two predictors, updated identically from decoded history, and use whichever
is currently winning. The codec follows the artwork instead of committing to one
predictor or spending a selector bit per point.

### Repeats under symmetry

When a command repeats an earlier one, the encoder emits a back-reference and a
delta instead of the geometry.

Matching is done under the **eight symmetries of the square** — four rotations
by 90°, each optionally mirrored. Those are exactly the transforms that map
integer coordinates to integer coordinates with no rounding, so a match is exact
rather than approximate. Arbitrary rotation and scaling would need tolerance
handling and are deliberately out of scope.

`REC_SINGLE_REPEAT` carries a flag saying "plain translation" and, when it is
not, a 3-bit symmetry code. With entropy coding that flag is strongly skewed and
costs a fraction of a bit, so plain translations are barely affected.

Rect and ellipse are transformed directly rather than through the generic matrix
path, because every symmetry maps an axis-aligned box to an axis-aligned box.
Keeping the opcode intact is what allows the repeat to match at all. Compact
primitives are excluded: their orientation lives in a geometry field rather than
in coordinates, so rotating one is not representable.

Measured on symmetric artwork:

| Artwork | Saving vs translation-only |
|---|---|
| One shape in four rotations | −32% |
| Mirrored pairs | −13% |
| Symmetric tiling | −18% |

`geom_translation` proves a plain translation by re-translating the earlier
command and requiring the geometry dictionaries to match exactly, so a
coincidental point-count match cannot produce a false positive. `geom_symmetry`
layers the symmetry search on top, trying identity first.

### Delta coding

Repeat deltas use signed Exp-Golomb (zigzag + `ue`), not Rice.

Repeat distances span the whole canvas: an adjacent copy is a few units away, a
mirrored one on the far side is several hundred. A fixed Rice parameter cannot
serve both — `k=2` spends over a hundred unary bits on a distance of 200, which
made every distant repeat lose to a full record. Exp-Golomb grows
logarithmically instead.

### Local-space SVG groups

A contiguous run of commands sharing an `editor_group` is normalized into a
stable local coordinate box and transmitted as:

```
fixed-width display box (x, y, width, height, local_width, local_height)
        +
either an inline local definition or a back-reference to an earlier one
```

The display box is 10 + 9 + 10 + 9 + 8 + 8 bits. Fixed width is deliberate:
moving or resizing a group changes the *values* but never the field *length*.
The same artwork therefore costs the same bits at 100%, 50%, or 10% display
scale, and duplicated SVGs reuse the definition.

### Primitive pricing

Compact primitives are simulated against their generic vector expansion using
live encoder state. The primitive is used only when it is **strictly smaller**,
so the same primitive can win in one image and lose in another.

### Precision ladder

```python
PRECISION_LADDER = (255, 224, 192, 160, 144, 128, 112, 96,
                    80, 72, 64, 56, 48, 40, 32)
```

`encode_image` encodes at each extent in turn and returns the first result that
fits ten messages. Images without eligible local groups skip the search. The
chosen precision depends on geometry complexity, never on displayed scale.

---

## Entropy coding

Protocol 6 replaces raw bit packing with an **adaptive binary range coder**
(LZMA-style). A flag that is 90% one value costs about 0.47 bits instead of 1.

Bits fall into two classes:

- **contexted** — skewed and worth modelling: record tags, opcodes, same-opcode
  and same-style flags, style flags, point mode flags, and the unary prefixes of
  Exp-Golomb and Rice codes. Each context adapts independently.
- **bypass** — near-uniform: absolute coordinate bits, palette indices, Rice
  remainders. Coded at a flat 1 bit so they cannot pollute the models.

Contexts are plain strings, so a new syntax element can be added without
renumbering a table. They are created on demand and start at p = 0.5.

### Why records are elements, not bits

The planner builds competing candidate records and splices the winner. An
adaptive coder cannot be spliced after the fact, because each bit's cost depends
on every bit before it.

So `BitWriter` records **syntax elements** with an estimated pre-entropy cost.
Splicing is list concatenation, planning compares `cost`, and entropy coding
happens once at the end over the final sequence. `cost` deliberately matches the
older raw-bit measure so representation choices stay stable.

### Dual mode

The range coder pays a fixed flush cost and needs data before its models are
worth anything, so it loses on very small images — up to +33% on a single
command.

`best_bytes()` therefore codes both ways and keeps the winner. **Protocol 6 is
never worse than protocol 5 on any image.** The cost is one extra encode pass
over a payload of at most 1,420 characters.

The mode flag rides free in the frame header. `stats.coding_mode` reports which
was used.

### Writer/reader symmetry is critical

If a writer and its reader disagree about which context a bit belongs to, **raw
mode still works** and only entropy-coded images corrupt. That makes the bug
easy to introduce and hard to spot — it surfaces as an `IndexError` or a
nonsense opcode far from its cause.

Every `w.bit(v, ctx)` must have a matching `r.bit(ctx)`. When adding a syntax
element, add its context to both sides in the same edit, then verify by
recording the encoder's context sequence and the decoder's and diffing them.
That trace is the only reliable way to locate a mismatch.

---

## MeshCore framing

### Envelope

| Property | Value |
|---|---|
| `MAX_MESSAGES` | 10 |
| `MESSAGE_LEN` | 150 |
| `FRAME_HEADER_LEN` | 8 |
| `FRAME_PAYLOAD_LEN` | 142 |
| `MAX_PAYLOAD_CHARS` | 1,420 |

### Header

8 characters, all Base62:

| Field | Width | Meaning |
|---|---|---|
| magic | 3 | `MCI` |
| version | 1 | Protocol number |
| image id | 3 | CRC-derived stream identifier |
| part | 1 | `index + 10·final + 20·mode` |

The part descriptor packs three things into one character — the frame index
(0–9), whether it is the final frame, and the coding mode — using 40 of the 62
available values.

**There is no per-frame CRC.** MeshCore already guarantees the integrity of a
delivered message, and the stream CRC-32 still catches what MeshCore cannot see:
frames from two images with colliding identifiers, or the wrong set pasted
together.

**There is no length field.** Chunking fills every frame except the last, so a
non-final frame is always exactly `MESSAGE_LEN` characters and the final frame
is simply what remains. A non-final frame that is not exactly full has been
truncated or concatenated, and is rejected.

Together these took the header from 15 characters to 8 and raised usable payload
from 1,350 to 1,420 characters.

### Alphabets

`BASE91` excludes `"`, `'`, and `\` so frames survive text transports and shell
quoting. MeshCore carries strings rather than binary, so Base91's ~1.22×
expansion is the price of admission. `BASE62` carries the header fields.

### Validation

`decode_frames` rejects frames that are the wrong length, carry the wrong magic
or protocol, declare an out-of-range index, disagree about the coding mode,
carry a short non-final payload, come from a different image, conflict with a
duplicate, leave a gap in the sequence, declare two different final frames, or
fail the stream CRC-32. Every failure raises `FrameError`.

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
| `coding_mode` | `1` entropy-coded, `0` raw |

`stats.fits` reports whether the image is within the ten-message budget.

---

## Editing this file

**Mirror every change.** A writer change must be mirrored in its reader, in both
syntax *and* context. Both must be exercised by round-trip fixtures.

**Bit changes are protocol changes.** If unchanged input produces different
bits, bump `PROTOCOL_VERSION` and update the Reconstructor in the same commit.

**Encoder-only changes are not.** Simplification tolerance, planning heuristics,
and search strategy affect size without changing the format. Those ship freely.

**Keep it re-entrant.** Local-coordinate precision is passed as an argument, not
held in module state. Do not reintroduce mutable module-level configuration.

**Do not import upward.** The codec may import the model, never the Constructor
or the Reconstructor.

### Verifying a change

Because protocol changes intentionally alter the bytes, byte-level golden
fixtures stop applying. Correctness is proven by **semantic round trip**
instead: what the decoder returns must equal what the encoder promised to send.

```python
enc = mci.encode_image(commands)
decoded  = mci.decode_frames(enc.frames)
promised = mci.decode_commands(enc.raw[:-4], enc.stats.coding_mode)[0]
assert [c.to_json() for c in decoded] == [c.to_json() for c in promised]
```

Both self-tests must pass:

```bash
python MCoreIMG-Constructor.py --self-test
python MCoreIMG-Reconstructor.py --self-test
```

---

## Known limitations

- Planning is contextual and greedy rather than globally optimal.
- Group repeats match under translation only. The symmetry search is applied to
  single commands, where the candidate set is one reference per opcode and the
  search is cheap.
- Scaled repeats are not detected outside local-space groups.
- The 32-colour palette is a hard protocol limit, not a tunable.
- Base91's ~1.22× expansion is unavoidable while MeshCore carries text. A
  binary-safe transport would recover roughly 18% immediately.
