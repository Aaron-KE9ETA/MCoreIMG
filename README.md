# MCoreIMG

**MCoreIMG is a compact vector-image instruction protocol for sending small, manually created pictures over MeshCore.**

Instead of transmitting raster image data, MCoreIMG sends a compressed sequence of drawing commands. A compatible reconstructor validates the received frames, rebuilds the command stream, and renders the original image on a fixed 720 × 480 canvas.

The protocol is designed around a strict transport target:

- No more than **five MeshCore text messages**
- No more than **150 ASCII characters per message**
- A **15-character control header** on each message
- Up to **135 Base91 payload characters** per message
- ACK and retransmission rather than forward-error correction or blind repetition

MCoreIMG is the successor to EMEIMG. It abandons EMEIMG's independent fixed-length drawing packets in favor of one stateful compressed bitstream.

> [!IMPORTANT]
> MCoreIMG is currently pre-alpha. The transport and bitstream formats may change. A constructor and reconstructor must use compatible protocol versions.

## Why MCoreIMG Exists

Mesh networks are good at moving compact text messages, not conventional image files. Even a tiny PNG normally exceeds the available message budget by a large margin.

MCoreIMG approaches the problem differently:

1. The sender draws an image from supported vector primitives.
2. The constructor stores those primitives in ordinary draw order.
3. The codec compresses the command stream using state, prediction, variable-length integers, and translated-repeat references.
4. The result is encoded into text-safe Base91.
5. The payload is split into one to five MeshCore messages.
6. The receiver validates and reconstructs the image locally.

The result is closer to sending a miniature drawing program than sending a picture file.

## Project Goals

- Fit useful simple artwork into five 150-character MeshCore messages.
- Preserve normal artistic draw order.
- Keep the source image editable in a human-readable format.
- Detect corruption before rendering.
- Permit targeted retransmission of a damaged or missing frame.
- Avoid requiring a human artist to manually optimize the compressed stream.
- Keep constructor and reconstructor behavior deterministic and compatible.

## Current Components

| File | Purpose |
|---|---|
| `MCoreIMG-Constructor.py` | Graphical vector editor, source loader, codec, frame generator, preview renderer, and PNG exporter |
| `MCoreIMG-Reconstructor.py` | Frame extractor, validator, bitstream decoder, command reconstructor, and PNG renderer |
| `*.mci.json` | Lossless, human-readable editable source |
| `*.mci` | MeshCore-ready transport frames, one frame per line |
| Legacy `*.emeimg` files | Older EMEIMG command files that the constructor may import for conversion |

## Requirements

### Arch Linux

```bash
sudo pacman -Syu python tk python-pillow
```

The constructor can run without Pillow for basic editing, but Pillow is required for PNG export. The reconstructor requires Pillow.

### Other Python Environments

Python 3.10 or newer is recommended.

```bash
python -m pip install pillow
```

Tkinter may need to be installed through the operating system's package manager.

## Quick Start

### Create an Image

```bash
python MCoreIMG-Constructor.py
```

Use the editor to select shapes, colors, coordinates, dimensions, orientation, and other shape-specific fields. Commands are stored as layers and rendered in list order.

Save the editable source as:

```text
example.mci.json
```

Export the MeshCore transport as:

```text
example.mci
```

The exported file contains one complete MCoreIMG frame per line.

### Run the Constructor Self-Test

```bash
python MCoreIMG-Constructor.py --self-test
```

The self-test checks codec round trips, corruption detection, source JSON round trips, and other regression-sensitive behavior.

### Reconstruct an Image

```bash
python MCoreIMG-Reconstructor.py example.mci
```

Specify the output filename:

```bash
python MCoreIMG-Reconstructor.py example.mci --output reconstructed.png
```

Print the decoded command list:

```bash
python MCoreIMG-Reconstructor.py example.mci --list-commands
```

Also produce constructor-compatible source JSON:

```bash
python MCoreIMG-Reconstructor.py example.mci --dump-json
```

When no input file is supplied, the reconstructor opens a graphical file chooser.

## Canvas and Rendering

- Canvas width: **720 pixels**
- Canvas height: **480 pixels**
- Background: **white**
- Coordinates begin at the upper-left corner.
- Valid X coordinates are `0..719`.
- Valid Y coordinates are `0..479`.
- Commands are rendered sequentially in source order.
- Later commands may cover earlier commands.
- Text is rendered with a hard-coded default size of `20` pixels.

Draw order is not separately transmitted. It is already represented by the order of commands in the decoded stream.

## Supported Opcodes

Opcode `0xF` is reserved by the compressed bitstream and is not a drawable shape.

| Opcode | Code | Shape | Important fields |
|---:|:---:|---|---|
| `0x0` | `0` | Text | `x`, `y`, `text` |
| `0x1` | `1` | Line | `x1`, `y1`, `x2`, `y2` |
| `0x2` | `2` | Rectangle | two corners, `fill` |
| `0x3` | `3` | Ellipse | center, horizontal radius, vertical radius, scale, fill |
| `0x4` | `4` | Triangle Outline | anchor, orientation, scale |
| `0x5` | `5` | Triangle Fill | anchor, orientation, scale |
| `0x6` | `6` | Arrow | anchor, orientation, scale |
| `0x7` | `7` | Star | center, radius, scale |
| `0x8` | `8` | Semicircle / Arc | center, radius, scale, start angle, arc degrees |
| `0x9` | `9` | Yagi Antenna | anchor, orientation, scale |
| `0xA` | `A` | Dish Antenna | anchor, orientation, scale |
| `0xB` | `B` | Radio Transceiver | anchor, orientation, scale |
| `0xC` | `C` | Radio Waves | center, radius, scale, start angle, arc degrees |
| `0xD` | `D` | Moon | anchor, scale, crater color |
| `0xE` | `E` | DoubleBox | two corners, divider percentage |

The macro-style shapes are reconstructed from known geometry. Only their anchor and adjustable parameters need to be transmitted.

## Human-Readable Source Format

A `.mci.json` file is the editable master copy of an image. It is not the over-the-air representation.

Example:

```json
{
  "format": "MCoreIMG-source",
  "version": 1,
  "protocol_version": 1,
  "canvas": {
    "width": 720,
    "height": 480
  },
  "metadata": {
    "grid": "EN60"
  },
  "commands": [
    {
      "opcode": 2,
      "shape": "2",
      "color": 5,
      "fields": {
        "x1": 40,
        "y1": 40,
        "x2": 300,
        "y2": 180,
        "fill": 0
      }
    },
    {
      "opcode": 0,
      "shape": "0",
      "color": 0,
      "fields": {
        "x": 70,
        "y": 80,
        "text": "MCOREIMG"
      }
    }
  ]
}
```

### Source Metadata

The Maidenhead grid locator is retained as source metadata for identification and project continuity. It is not currently included in the compressed MCoreIMG transport frames.

The source format should remain lossless even when the transport codec changes. A future constructor can therefore re-encode an older source file using a newer protocol version.

## Transport Profile

Each MeshCore frame is at most 150 characters:

```text
[15-character header][0 to 135 Base91 payload characters]
```

An image may use one through five frames.

### Header Layout

The fixed header is:

```text
MCI V III P T LL CCC F
```

The spaces above are explanatory only and are not transmitted.

| Position | Width | Encoding | Meaning |
|---:|---:|---|---|
| `0..2` | 3 | ASCII | Magic string `MCI` |
| `3` | 1 | Base62 | Protocol version |
| `4..6` | 3 | Base62 | Deterministic image ID |
| `7` | 1 | Base62 | Zero-based part index |
| `8` | 1 | Base62 | Total number of parts |
| `9..10` | 2 | Base62 | Payload character count |
| `11..13` | 3 | Base62 | CRC-16 of this frame's payload |
| `14` | 1 | Base62 | Flags |

The current encoder writes flags as zero. The field is reserved for compatible future use.

### Image ID

The three-character image ID is derived deterministically from the encoded image stream. It allows receivers to group frames belonging to the same image and reject accidental mixtures.

It is an identifier, not a cryptographic signature.

### Payload Alphabet

The compressed bytes are converted to a custom Base91 alphabet made from printable ASCII characters.

The following characters are excluded:

```text
"  '  \
```

Excluding them avoids common quoting and escaping problems in JSON, shells, logs, and chat transports.

## Compression Model

MCoreIMG compresses the drawing command stream before Base91 encoding.

### 1. Implicit Draw Order

Commands remain in artistic draw order. No extra draw-order field is transmitted.

This avoids the cost of sending an index for every shape and avoids forcing the artist to draw all objects of one opcode together.

### 2. Compact Opcodes

Drawable shapes use four-bit opcodes.

The codec can use a one-bit same-opcode indication when the relevant opcode context is already known instead of writing the full four-bit opcode again.

### 3. Opcode-Local State

Shape parameters are maintained in opcode-local state rather than one indiscriminate global parameter history.

For example, the most recently used star radius belongs to the star opcode's state. Drawing a radio or rectangle between two stars does not erase the useful star defaults.

This is important because natural artwork frequently alternates between object types. Opcode-local state improves compression without changing draw order.

Typical stateful fields include:

- Color
- Radius
- Horizontal and vertical radii
- Scale
- Orientation
- Fill
- Arc start and sweep
- Moon crater color
- DoubleBox divider percentage

A one-bit reuse/change flag indicates whether a value is unchanged. Changed values are then written using the appropriate fixed or variable-length representation.

### 4. Predictive Coordinates

The first usable coordinate context is encoded absolutely:

- X uses 10 bits.
- Y uses 9 bits.

Later coordinates may be encoded as signed deltas from a previous relevant point when that representation is shorter. The encoder chooses between delta and absolute forms.

For two-point commands, the second point may be represented relative to the first point.

### 5. ZigZag and Golomb-Rice Coding

Signed coordinate and translation deltas are mapped to non-negative integers using ZigZag ordering:

```text
0, -1, +1, -2, +2, ...
```

The mapped values are then encoded with Golomb-Rice coding. Small movements are therefore cheap, which matches the way repeated decorative objects and nearby geometry are commonly placed.

### 6. Unsigned Exp-Golomb Coding

Small non-negative values use unsigned Exp-Golomb coding where appropriate.

This is used for values such as:

- Command count
- Text length
- Radius minus one
- Scale minus one
- Other bounded shape parameters

Small values consume fewer bits without requiring a fixed-width field large enough for the maximum.

### 7. Nonadjacent Same-Opcode Repeat References

A translated-repeat command can refer to the most recent compatible command of the same opcode, even when unrelated commands appear between them.

Conceptually:

```text
Star
Radio
Dish
Star translated from previous Star
```

The second star can reuse the earlier star's shape, color, and non-coordinate fields while transmitting only a compact translation.

A repeat is valid only when:

- The referenced command has the same opcode.
- Required non-coordinate fields are compatible.
- Coordinate pairs differ by one consistent translation.
- The translated result remains valid on the canvas.

This preserves normal artistic layering while recovering much of the compression benefit that would otherwise require grouping identical shapes together.

### 8. Text Encoding

Text is uppercased and restricted to the protocol's six-bit text alphabet:

```text
 0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ-+./?:,()[]#_=@
```

The maximum text length is 31 characters.

Unsupported characters are normalized rather than transmitted as arbitrary Unicode.

### 9. Byte Packing and Base91

The bitstream is packed into bytes, protected by a stream CRC-32, and encoded as Base91 text. The encoded text is then split across the required number of MeshCore frames.

## Integrity and Retransmission

MCoreIMG assumes that reliability is provided through acknowledgement and selective retransmission.

### Per-Frame CRC-16

Every frame header includes a CRC-16 calculated over that frame's Base91 payload.

A receiver can identify a corrupted part immediately and request retransmission of only that part.

### Stream CRC-32

After all parts are collected and reassembled, the reconstructor Base91-decodes the complete payload and verifies the CRC-32 appended to the compressed stream.

This detects corruption or incorrect assembly that survives individual frame checks.

### Duplicate Handling

Identical retransmissions of the same frame part may be ignored.

Two frames claiming the same image ID and part index but containing different payloads are a conflict and must not be silently accepted.

### Missing Parts

The image cannot be decoded until all declared parts are present.

Frames may arrive out of order because the part index determines assembly order.

## Receiver Behavior

A compatible receiver should:

1. Ignore unrelated MeshCore messages.
2. Search incoming text for a complete `MCI` frame.
3. Read the declared payload length from the header.
4. Validate the frame length and Base91 alphabet.
5. Validate the frame CRC-16.
6. Group frames by image ID and total-part count.
7. Ignore identical duplicate retransmissions.
8. Reject conflicting duplicates.
9. Wait for every required part.
10. Reassemble payloads by part index.
11. Base91-decode the assembled payload.
12. Validate the stream CRC-32.
13. Decode commands in order.
14. Render commands in order on a 720 × 480 canvas.

The reference reconstructor can also extract an MCoreIMG frame from a log line that contains a timestamp, sender name, or other text before the frame.

## Compatibility Rules

Constructor and reconstructor implementations must agree on all of the following:

- Protocol version
- Canvas dimensions
- Opcode assignments
- Shape geometry
- Color table and color indexes
- Text alphabet
- Stateful-field behavior
- Coordinate prediction rules
- Rice parameters
- Exp-Golomb interpretation
- Repeat-reference semantics
- Base91 alphabet
- CRC algorithms
- Frame-header layout

Changing any item that affects decoding should require a protocol-version change unless backward compatibility is explicitly implemented.

Source-format versioning and compressed-protocol versioning are separate concerns.

## Capacity

The absolute text payload ceiling is:

```text
5 frames × 135 payload characters = 675 Base91 characters
```

The practical number of drawable commands varies greatly.

Images compress best when they contain:

- Repeated colors
- Repeated shape parameters
- Nearby coordinates
- Repeated translated objects
- Macro shapes
- Small scales and radii
- Short text

Images compress poorly when every command changes opcode, color, dimensions, and location unpredictably.

The constructor displays live codec statistics and refuses transport export when an image exceeds the five-frame profile.

## Design Guidance

For better compression without deliberately drawing in an unnatural order:

- Reuse a small color palette.
- Duplicate and move existing objects when appropriate.
- Keep repeated shapes geometrically identical.
- Use macro shapes instead of rebuilding them from many lines.
- Prefer nearby placement when the composition permits it.
- Keep text short.
- Watch the live frame count while editing.

Do not reorder layers solely to improve compression unless the visual result remains correct. Opcode-local state and same-opcode repeat references exist specifically to reduce that pressure.

## Legacy EMEIMG Relationship

EMEIMG used fixed 13-character drawing packets with explicit command-order indexes. That design was easy to inspect manually but spent characters repeatedly transmitting structure and had a hard shape-count limit tied to its order field.

MCoreIMG changes the model:

| EMEIMG | MCoreIMG |
|---|---|
| Independent fixed-length commands | One compressed stateful stream |
| Base36-oriented fields | Bit-level coding plus Base91 transport |
| Explicit draw-order character | Stream order is draw order |
| Repeated values retransmitted | Stateful reuse flags |
| Mostly absolute fields | Absolute or predicted values |
| Adjacent packet model | Nonadjacent same-opcode references |
| Packet repetition for resilience | ACK and selective retransmission |
| Up to 36 indexed commands | Command count limited mainly by compressed capacity |

Legacy EMEIMG files are source material for conversion, not wire-compatible MCoreIMG messages.

## Security and Privacy

MCoreIMG is an image representation and framing protocol. It does not provide its own encryption, authentication, or sender verification.

When transmitted through MeshCore, privacy and sender identity depend on the surrounding MeshCore configuration and application behavior.

CRC values detect accidental corruption. They do not prevent intentional modification.

## Current Limitations

- Maximum of five frames under the current transport profile
- Fixed 720 × 480 canvas
- Fixed opcode and color tables
- Restricted uppercase text alphabet
- No arbitrary fonts
- No raster-image embedding
- No alpha channel
- No gradients
- No animation
- No built-in forward-error correction
- No cryptographic authentication
- Protocol is still pre-alpha

## Development Notes

Before changing codec behavior:

1. Update the constructor and reconstructor together.
2. Increase the protocol version when old streams would decode differently.
3. Add or update a round-trip test.
4. Test frame corruption and missing-frame errors.
5. Test duplicate and conflicting duplicate behavior.
6. Test source JSON save and reload.
7. Test PNG rendering parity.
8. Test a realistic image that alternates opcodes.
9. Test nonadjacent same-opcode translated repeats.
10. Confirm the exported stream still fits the intended MeshCore profile.

A useful minimum compatibility test is:

```text
commands
  -> encode
  -> frame
  -> parse frames
  -> assemble
  -> decode
  -> compare every reconstructed command
```

## Status

MCoreIMG is an experimental low-bandwidth image protocol and editor. It is intended for practical experimentation with transmitting simple vector graphics through very constrained text-message channels.

Expect the format to evolve while compression methods, mobile integration, acknowledgement behavior, and real MeshCore transport are tested.
