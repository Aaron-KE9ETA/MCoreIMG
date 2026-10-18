# Parity branch

Branch notes for review and merge. This branch adds RAID 6–style parity to
MCoreIMG and, building on it, prepares the codec for its targeted use: images
broadcast into a shared MeshCore channel.

| | Before the branch | After the branch |
|---|---|---|
| Codec build | `2026.08.05-compression-v6.0-SLIMHEADER` | `2026.10.17-compression-v7.1-CHANNEL120` |
| Constructor build | `2026.08.05-svg-v6.0-MODULAR-SLIMHEADER-10MSG` | `2026.10.17-svg-v7.1-MODULAR-CHANNEL120-10+2MSG` |
| Reconstructor build | `2026.08.05-reconstructor-v6.0-SLIMHEADER` | `2026.10.17-reconstructor-v7.1-CHANNEL120` |
| Model build | `2026.08.05-model-v6.0-MODULAR` | unchanged |
| Wire protocol | 6 | **7** |
| Feature signature | `PROTO6\|…\|10MSG` | `PROTO7\|…\|10MSG\|RAID6\|MULTIIMAGE` |

This document describes the change at the level of wire format, API, and
behaviour. For the exact line-level change, use git:

```bash
git diff main...parity --stat
git log main..parity --oneline
```

---

## Contents

1. [Summary](#summary)
2. [The three stages](#the-three-stages)
3. [Wire format, protocol 7](#wire-format-protocol-7)
4. [How the parity works](#how-the-parity-works)
5. [Multi-image receive](#multi-image-receive)
6. [API changes](#api-changes)
7. [Tool changes](#tool-changes)
8. [Breaking changes](#breaking-changes)
9. [Verification](#verification)
10. [Costs](#costs)
11. [Known limitations and open decisions](#known-limitations-and-open-decisions)
12. [Kotlin port checklist](#kotlin-port-checklist)
13. [Merge checklist](#merge-checklist)

---

## Summary

- **Two parity messages, P and Q, follow every image that fits.** Any two of
  the k + 2 messages can be lost — data, parity, or one of each — and the
  receiver rebuilds the image exactly. A third loss is refused, never
  mis-decoded.
- **Parity is computed directly on the Base91 alphabet**, so a parity frame is
  never longer than a data frame. Byte-level GF(256) parity would not have fit
  in a message.
- **Messages from many images can share one channel.** Each frame carries a
  4-character image id; a receive engine, `FrameSorter`, sorts interleaved
  frames into per-image sets and decodes each image as soon as it is complete.
- **Messages are now 120 characters**, the MeshCore channel limit. Capacity per
  image drops from 1,420 to 1,120 payload characters.
- **Data payload size is unchanged** by parity and by the header change; the
  only payload movement on the branch comes from the shorter messages.

---

## The three stages

The branch was built in three stages. Each is described separately so it can
be reviewed separately, though they are not independent: later stages build on
earlier ones.

### Stage 1 — RAID 6 parity (codec v6.1)

- New parity layer in `MCoreIMG-compression.py`: `build_parity_frames`,
  `reassemble_frames`, and the `Reassembly` result.
- `encode_image` gains `parity=True` and emits P and Q when the image fits.
- `decode_frames` accepts any mix of data and parity frames, in any order, with
  duplicates, and rebuilds up to two missing frames.
- Parity frames use a distinct magic, `MCP`. Data frames were unchanged, so
  this stage kept protocol 6.
- Constructor lists, copies, and exports all k + 2 frames; the status bar shows
  parity separately (`10/10 frames +2 parity`).
- Reconstructor recognises `MCP` frames while scanning text and reports what it
  rebuilt.
- Missing-part errors are numbered from 1, matching the GUI.

### Stage 2 — Multi-image channel receive (protocol 7)

- Image id widened from 3 to 4 Base62 characters.
- Header version field removed to pay for it; the magic now names the frame
  format. Header length stays 8. **This is the protocol bump to 7.**
- The image id is checked against the content of a completed set.
- New receive engine: `FrameSorter`, `DecodedImage`, `SetStatus`,
  `sort_and_decode`.
- Reconstructor decodes every image in an input, writing one PNG per image
  named by image id, and reports incomplete sets.
- Editable `.mci.json` sources are no longer refused for carrying an older
  `protocol_version`.

### Stage 3 — 120-character channel messages (v7.1)

- `MESSAGE_LEN` 150 → 120; payload per frame 142 → 112; capacity 1,420 → 1,120.
- The Constructor's precision-ladder self-test image was resized, and its
  checks made unconditional (see [Verification](#verification)).
- Test references and port fixtures regenerated.
- The Aflaq sample no longer fits and became the over-budget test case.

---

## Wire format, protocol 7

Every message is at most `MESSAGE_LEN` = **120** characters: an 8-character
header and up to 112 payload characters.

### Data frame

| Field | Width | Content |
|---|---|---|
| magic | 3 | `MCI` — also names the frame format |
| image id | 4 | Base62; ties every message of one image together |
| descriptor | 1 | Base62: `index + 10·final + 20·mode` |
| payload | ≤ 112 | Base91 slice of the stream |

`index` is 0–9, `final` marks the last data frame, `mode` is 1 for
range-coded and 0 for raw. Every non-final data frame is exactly 120
characters; the final frame is whatever remains.

### Parity frame

| Field | Width | Content |
|---|---|---|
| magic | 3 | `MCP` |
| image id | 4 | Same as the data frames |
| descriptor | 1 | Base62: `which + 2·(k−1) + 20·mode`, which 0 = P, 1 = Q |
| symbols | L | Parity symbols, Base91 alphabet |

`L` is the longest data payload rounded up to even: 112 when k > 1, so the
frame is exactly 120 characters; for k = 1 it matches the single data frame.

The descriptor carries **k and the coding mode**, so a receiver that has lost
the final data frame — or every data frame, when k = 1 — still knows how many
frames exist and how to decode them.

### Removed from the header

| Field | Removed in | Why |
|---|---|---|
| Per-frame CRC | protocol 6 | MeshCore guarantees delivered-message integrity |
| Length | protocol 6 | Derivable: non-final frames are always full |
| Version | **protocol 7** | The magic names the format; the codec version is in the stream |

The stream CRC-32 remains, inside the payload.

### Image id

`image_id_for(raw)`: CRC-32 of the stream bytes (data plus stream CRC),
reduced modulo 62⁴ = 14,776,336 and written as 4 Base62 characters.

Derived from content, so:

- identical images from different stations produce identical frames, which
  merge at the receiver;
- after assembly the receiver recomputes the id from the content and rejects
  a set whose content does not match the id it carries.

### Annotated example

The cart sample at 120-character messages (k = 6, range-coded), payloads
truncated:

```text
MCI Dcvx K  120 chars  descriptor 20  data part 1           | oRH=G6bz$M$Of6a}d})jp}#~...
MCI Dcvx L  120 chars  descriptor 21  data part 2           | Zw4oG10],;&n`/lAm/LY46%B...
MCI Dcvx M  120 chars  descriptor 22  data part 3           | =@ru?vse^9>b8fn0^Yt9f[WW...
MCI Dcvx N  120 chars  descriptor 23  data part 4           | 6heuqT9zlxqAt)dE}tM=9tiY...
MCI Dcvx O  120 chars  descriptor 24  data part 5           | ;I30)A5)0CJ)xie@#H,l|_MG...
MCI Dcvx Z   86 chars  descriptor 35  data part 6 (final)   | PaE0LbeXbT[l{^(IaRW&L,;B...
MCP Dcvx U  120 chars  descriptor 30  parity P, k = 6       | bXe3MnqtEk:2EShVmQSfHJ,8...
MCP Dcvx V  120 chars  descriptor 31  parity Q, k = 6       | ZX.&8y-$[5&I*rM`YGN<l=ol...
```

On the wire there are no spaces: `MCIDcvxK` is the first frame's header.
Descriptor 35 = index 5 + 10 (final) + 20 (range-coded). Descriptor 30 =
P (0) + 2·(6−1) + 20.

---

## How the parity works

### The constraint

MeshCore carries text, not bytes. Standard RAID 6 parity over GF(256) yields
arbitrary bytes; Base91-encoding them grows them by about 23%, so protecting
112 characters would need about 146, and a parity frame would not fit in a
120-character message.

### The alphabet-preserving code

Because **91 = 7 × 13**, the Chinese remainder theorem makes every Base91
symbol `c` exactly the pair `(c mod 7, c mod 13)`, and every pair exactly one
symbol. Parity is computed separately in each residue and recombined:

```text
symbol = (78·r7 + 14·r13) mod 91
```

P + Q RAID 6 needs one distinct non-zero coefficient per data frame, so the
field's multiplicative group must have at least 10 elements:

| Residue | Field | Group order | Generator | Coefficients for frames 0–9 |
|---|---|---|---|---|
| mod 13 | GF(13) | 12 | 2 | `1 2 4 8 3 6 12 11 9 5` |
| mod 7 | GF(49) = GF(7)[i], i² = −1 | 48 | 2 + i (encoded 9) | `1 9 31 30 21 46 16 44 5 38` |

GF(7) alone has six non-zero elements, too few for ten frames, so the mod-7
residues of adjacent symbols are paired into one GF(49) element, encoded
`re + 7·im`. x² + 1 is irreducible over GF(7) because 7 ≡ 3 (mod 4).

For each symbol position:

```text
P = Σ D_j            Q = Σ g^j · D_j
```

With distinct coefficients, every system the decoder can meet — one or two
unknown data frames, with P, Q, or both — is non-singular, which is the whole
RAID 6 guarantee. The coefficient tables are asserted at import, so changing a
generator cannot happen silently.

### Padding and the final frame

Data shorter than `L` is padded with the pad symbol `!` (Base91 value 0) for
parity computation only; padding is never transmitted.

A parity frame is already full, so the true length of the final data frame is
not carried anywhere. When the final frame has to be rebuilt, every position
past its true end rebuilds to the pad symbol, which narrows the candidates.
Each remaining length is tried, and the one kept must:

1. be canonical Base91 (re-encoding the decoded bytes reproduces the text),
2. pass the stream CRC-32,
3. hash to the frame set's image id,
4. parse as a command stream.

If more than one length survives, the decoder raises rather than guessing.

---

## Multi-image receive

### `FrameSorter`

The reference receive engine. Messages go in one at a time; images come out
as each completes.

```python
sorter = FrameSorter()
for message in channel:
    for image in sorter.add(message):      # images this message completed
        show(image.image_id, image.commands)
sorter.pending()                           # SetStatus for each incomplete set
sorter.expire(older_than=cutoff)           # drop sets not heard from since
```

An image completes on the first message after which it can be proven: all k
data frames, or any k of the k + 2.

### Guarantees

- **No mixing.** An image is reported only when its stream passes CRC-32 and
  hashes to its own image id.
- **Merging.** Re-sends, and the same image sent by two stations, merge
  silently, before and after completion.
- **No exceptions from traffic.** Unparseable text is counted in
  `sorter.rejected` and dropped.
- **Bounded memory.** At most 64 pending sets, 48 frames per set, and 256
  remembered completed images, evicting the oldest first.
- **No timing policy.** The sorter never abandons a set on its own. When to
  give up on an incomplete set belongs to the channel layer.

### Id collisions

With 4 characters, a new image collides with a live set about once in 300,000
images when 50 sets are live; 3 characters would have been about once in
5,000, and a collision loses both images.

When one happens, the sorter keeps every frame and tries candidate assemblies,
most complete first, including leaving out a held frame and rebuilding that
part from parity — a slot can hold the other image's frame. Each candidate is
proven by CRC and id or discarded. Disproved candidates are remembered, and at
most 512 new ones are tried per arriving message, which bounds the time a
flood of conflicting frames can cost.

---

## API changes

All in `MCoreIMG-compression.py` unless noted.

### New

| Name | Purpose |
|---|---|
| `build_parity_frames(data_frames)` | P and Q for a complete, in-order data frame set |
| `reassemble_frames(frames)` → `Reassembly` | Validate one image's frames and rebuild up to two missing |
| `Reassembly` | `frames`, `recovered`, `mode`, `k`, `parity_received`, `image_id` |
| `FrameSorter` | Multi-image receive engine: `add`, `add_many`, `pending`, `expire` |
| `DecodedImage` | `image_id`, `commands`, `frames`, `recovered`, `k`, `parity_received`, `first_seen`, `completed_at` |
| `SetStatus` | Progress of an incomplete set; `missing`, `recoverable_now`, `describe()` |
| `sort_and_decode(frames)` | Batch form: `(images, incomplete, sorter)` |
| `image_id_for(raw)` | The 4-character id for an encoded stream |
| `PARITY_MAGIC`, `PARITY_FRAMES`, `PARITY_P`, `PARITY_Q` | Parity constants |
| `IMAGE_ID_LEN`, `IMAGE_ID_SPACE` | 4 and 14,776,336 |
| `EncodedImage.parity_frames` | P and Q, or empty |
| `EncodedImage.transmit_frames` | What to send: data frames, then P, then Q |
| `CodecStats.parity_frame_count` | 2 when parity was added |
| `CodecStats.message_count` | Data plus parity actually transmitted |

### Changed

| Name | Change |
|---|---|
| `encode_image(commands, parity=True)` | New `parity` argument; adds P and Q when the image fits |
| `decode_frames(frames)` | Accepts parity frames; rebuilds up to two missing; checks the image id |
| `PROTOCOL_VERSION` | 6 → 7 |
| `MESSAGE_LEN` | 150 → 120 |
| `FRAME_PAYLOAD_LEN` | 142 → 112 |
| `MAX_PAYLOAD_CHARS` | 1,420 → 1,120 |
| `CodecStats.fits` | Unchanged meaning: data frames ≤ 10. Parity sits outside the budget |

### Reconstructor

| Name | Change |
|---|---|
| `decode_assets(core, path, frames)` | New. Every image in an input: `(assets, incomplete, notes)` |
| `decode_asset(...)` | Now a single-image wrapper; raises on multi-image input |
| `frame_protocol_version(frame)` | Returns the protocol implied by the magic |
| `MAGIC_PROTOCOL`, `PARITY_MAGIC`, `IMAGE_ID_LEN` | New bootstrap constants, verified against the codec at load |
| `DEFAULT_MESSAGE_LEN` | 150 → 120 |

---

## Tool changes

### Constructor

- **Export MCI** writes all k + 2 frames, one per line.
- **Preview Frames** lists them in transmission order, labelling P and Q.
- **Copy All Frames** copies all of them.
- The status bar shows parity separately: `10/10 frames +2 parity`.
- The build-integrity guard requires protocol 7, 120-character messages, the
  parity layer, and the sorter with 4-character ids.
- There is **no GUI toggle** for parity yet.

### Reconstructor

- Decodes every image in an input. One image is written as before; several are
  written as `<output>-<image id>.png`, and are not opened automatically.
- Reports, per image, what arrived and what was rebuilt:

  ```text
  Input: 27 frame(s) from channel.log, 3 image(s) decoded, 1 incomplete
  Incomplete: liKl: 3 data frame(s), 0 parity; total unknown until the final frame or a parity frame arrives
  Loaded: image ceBP: 8/10 data + 2/2 parity frame(s); rebuilt part(s) 2, 7 from parity
  ```

- Exit code 0 when at least one image decoded (incomplete sets go to stderr);
  2 when none did.
- Editable source files are no longer refused for an older `protocol_version`.

---

## Breaking changes

| What | Effect | Action |
|---|---|---|
| Protocol 7 header | v6 frames are not decoded; v6 receivers cannot decode v7 frames | Pair every Constructor, codec, and Reconstructor from this branch |
| 120-character messages | 21% less capacity per image | Dense artwork may no longer fit |
| Aflaq sample | No longer fits: 1,334 characters, needs 12 data frames | Now the over-budget test case |
| `harness.REFERENCE` | Atassi 1,380 → 1,083, Aflaq 1,398 → 1,334 (over budget), cart 638 unchanged | Already updated |
| Port fixtures | Regenerated for the new header and envelope | Re-verify the Kotlin port |

A v6 frame is not misread as anything else: under the v7 layout it simply fails
its image-id check and is reported as undecodable.

### Sample results at 120 characters

| Sample | Shapes | Payload | Data frames | Sent with parity | Local extent |
|---|---|---|---|---|---|
| Atassi portrait | 64 | 1,083 chars | 10 / 10 | 12 | 80 |
| Vector cart | 25 | 638 chars | 6 / 10 | 8 | 255 |
| Aflaq portrait | 97 | 1,334 chars | needs 12 | — | — |

Coarser local precision does not rescue the Aflaq portrait: even at extent 12
it is 1,267 characters, still over 1,120.

---

## Verification

Everything in this section was measured on this branch at 120-character
messages unless it says otherwise.

### Parity

| Test | Result |
|---|---|
| Every 1- and 2-message loss, samples that fit, shuffled with duplicates | 114 patterns, 0 wrong |
| Every 3-message loss | 276 / 276 refused |
| Final frame lost, across 110 final lengths (1–112) and 42 single-frame images | 566 decodes, 0 wrong |
| End to end through the CLI, two lines deleted from an `.mci` file | pixel-identical to a lossless receive (measured at 150 characters, before Stage 3) |

The parity scheme itself was first checked exhaustively in isolation: 9,000
erasure patterns across k = 1–10, all recovered exactly.

### Multi-image receive

| Test | Result |
|---|---|
| Atassi and cart interleaved, each losing 0–2 messages, with re-sends and chat, 200 streams | 200 / 200 fully decoded, 0 wrong, worst 83 ms per message |
| Real channel log through the CLI, 3 images with 2 messages lost each | 3 PNGs, pixel-identical to lossless receives (measured at 150 characters, before Stage 3) |

Collisions, forced by shrinking the id space to one or two values so that
every image shares an id:

| Colliding images, each losing 0–2 messages | Every image separated | Wrong decodes | Worst time |
|---|---|---|---|
| 2–4 small images, id space of 2, 120 streams | 120 / 120 | 0 | 65 ms per stream |
| 2–4 small images, id space of 1, 120 streams | 120 / 120 | 0 | 72 ms per stream |
| 2 full-size images (k = 8), 5 runs | 5 / 5 | 0 | 70 ms per message |
| 3 full-size images (k = 8), 5 runs | 5 / 5 | 0 | 529 ms per message |
| 4 full-size images (k = 8), 5 runs | 1 / 5 | 0 | 441 ms per message |

Four full-size images on one id exceed the search budget and stay pending,
flagged as conflicting. That is a limit, not a correctness failure.

### Suites

| Suite | Result |
|---|---|
| Constructor `--self-test` | PASS |
| Reconstructor `--self-test` | PASS, 10 checks |
| Entropy context symmetry | 3 / 3 cases symmetric |
| Integration | 41 / 41 |
| Port fixtures | 5 parity cases, 147 loss patterns; 8 sorter streams, 16 completions; 0 mismatches |
| Benchmark | payload matches `REFERENCE` |

Run them all with `python tests/run_all.py`.

### A test defect found and fixed on this branch

Moving to 120-character messages made the Constructor's precision-ladder test
image too large to fit. Its parity and ladder checks sat inside an
`if fits:` guard, so they were **silently skipped while the self-test still
printed PASS**. The image is now sized to be over budget at full precision and
to fit only after the ladder steps down, and the self-test fails outright if
either stops being true.

The same review removed the assumption that every sample fits: the integration
suite now checks that the Aflaq portrait is reported over budget and emits no
frames.

### Not verified

- **The GUI.** Export, Preview Frames, Copy All Frames, and the status bar were
  checked statically (names resolve, no shadowed definitions) but not run,
  because the development environment has no display.
- **Real radio.** Nothing here has been run over MeshCore. Loss behaviour is
  simulated by deleting messages.
- **Channel message length.** 120 characters is taken as given for the
  targeted MeshCore channel use; the codec has not been checked against
  firmware.

---

## Costs

| | |
|---|---|
| Messages per image | k data + 2 parity |
| Extra airtime | +20% on a 10-frame image, +33% on a 6-frame image |
| Parity encode time | under 1 ms |
| Worst-case recovery | about 100 ms, a 10-frame image with the final frame lost |
| Data payload | unchanged by parity or by the header change |

Whether parity is a net airtime saving depends on how often messages are
actually lost on the channel, which has not been measured. It replaces
retransmission; it does not reduce traffic.

---

## Known limitations and open decisions

**Open decision: the message cap.** Dense images such as the Aflaq portrait no
longer fit ten 120-character frames. The one-character descriptor can address
up to 15 data frames without a header change — the largest values used would
be 59 of 62, for data and parity alike. This was checked arithmetically, not
in code. Aflaq would then take 12 data plus 2 parity, 14 channel messages. It
is a mesh-load decision, so the cap stays at 10 on this branch.

**Not done on this branch:**

- Channel code and region scoping.
- Expiry policy for incomplete sets: the sorter offers `expire()`, the channel
  layer must decide when to call it.
- A GUI toggle for parity. `encode_image(..., parity=False)` exists.

**Inherent limits:**

- Parity rebuilds *lost* messages. A corrupted message is detected by the
  stream CRC-32 but cannot be located.
- Three or more lost messages per image cannot be recovered.
- Four or more full-size images sharing one id exceed the collision-search
  budget and stay pending, flagged as conflicting. They are never mis-decoded.
- The Atassi portrait sits at exactly 10 / 10 frames, with no headroom.

---

## Kotlin port checklist

The port verifies against fixtures generated from this reference; zero
mismatches is the bar.

- [ ] **Framing.kt — header.** `MCI`/`MCP` + 4-character id + 1-character
      descriptor. No version character. `MESSAGE_LEN` 120,
      `FRAME_PAYLOAD_LEN` 112, `MAX_PAYLOAD_CHARS` 1,120.
- [ ] **Image id.** `CRC32(raw) mod 62⁴`, 4 Base62 characters, checked against
      content after assembly.
- [ ] **Parity layer.** CRT split, GF(13) and GF(49) arithmetic, coefficient
      tables (assert them), pad symbol, final-frame length resolution.
- [ ] **ReceiveEngine — mirror `FrameSorter`.** Per-id sets, merging of
      identical frames, completion on the first provable message, bounded
      memory, no exceptions on bad input.
- [ ] **Completion signal.** A parity frame's descriptor carries k, so the
      receive engine can report progress against a known total before the final
      data frame arrives.
- [ ] **Airtime model.** The SX127x model and zero-hop guard should count the
      two parity messages.
- [ ] **Fixtures.** `tests/fixtures/parity_fixtures.json` (5 cases, 147 loss
      patterns) and `tests/fixtures/sorter_fixtures.json` (8 interleaved
      streams, with the arrival at which each image completes).

Regenerate fixtures after any wire change with
`python tools/make_parity_fixtures.py`; `tests/test_parity_fixtures.py` keeps
the reference locked to them.

---

## Merge checklist

- [ ] `python tests/run_all.py` passes on a machine with Tkinter.
- [ ] `python MCoreIMG-Constructor.py --version` reports protocol 7 and the
      v7.1 builds for codec and Constructor.
- [ ] GUI checked by hand: Export MCI writes k + 2 lines; Preview Frames labels
      P and Q; the status bar shows `+2 parity`.
- [ ] Reconstructor run on a saved multi-image channel log.
- [ ] Decision recorded on the message cap (stay at 10, or raise).
- [ ] Kotlin port re-verified against both fixture files, or the branch merged
      with the port explicitly marked as behind.
