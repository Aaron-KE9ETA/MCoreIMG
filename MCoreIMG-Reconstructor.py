#!/usr/bin/env python3
"""
MCoreIMG Reconstructor — transport frames back into an image
============================================================

This program turns MCoreIMG transport frames, or an editable source file, back
into a raster image.  It contains **no codec of its own**: decoding is done by
``MCoreIMG-compression.py`` and rendering by ``MCoreIMG-Constructor.py``, so
the receiver can never drift from the transmitter.

    MCoreIMG-model.py           <- opcodes, model, geometry
    MCoreIMG-compression.py     <- bitstream codec and MeshCore framing
    MCoreIMG-Constructor.py     <- SVG import, rendering, editor GUI
    MCoreIMG-Reconstructor.py   <- you are here: decode, render, export

Only the codec is loaded directly; it pulls in the model itself.

HOW THIS DIFFERS FROM THE EARLIER ADAPTER
-----------------------------------------

Previous builds could not simply import the codec, because the Constructor was
a single file with several historical layers and no stable public API.  To cope
with that, this program carried roughly seven hundred lines of reflection: it
scanned candidate files, ranked them by a feature score, searched modules and
classes for anything that looked like a decoder or a renderer, and then tried
several calling conventions until one bound successfully.

The codec is now an importable module with an explicit ``__all__``.  All of
that discovery machinery is therefore gone.  The behaviour it was protecting
against — a receiver paired with the wrong transmitter — is now caught directly
by comparing protocol numbers and reporting both build strings.

DATA FLOW
---------

Transport path::

    input text/.mci
        -> extract and validate complete MCI frames
        -> check the protocol number in the frame header
        -> mci.decode_frames(frames)          (CRC + envelope validation)
        -> ctor.render_to_pillow(commands)
        -> save PNG, optionally export editable JSON

Source path::

    .mci.json / .json / .svg / .svgz
        -> ctor.load_source(path)
        -> document.transformed_commands()
        -> ctor.render_to_pillow(commands)

EXIT CODES
----------

0 success, 1 interactive chooser cancelled, 2 user-facing reconstruction or
compatibility error, 130 interrupted.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any, Iterable, Mapping, Optional, Sequence

RECONSTRUCTOR_BUILD = "2026.10.17-reconstructor-v7.1-CHANNEL120"

COMPRESSION_FILENAME = "MCoreIMG-compression.py"
CONSTRUCTOR_FILENAME = "MCoreIMG-Constructor.py"


class ReconstructorError(RuntimeError):
    """Raised for user-facing input, pairing, or reconstruction failures."""


# ---------------------------------------------------------------------------
# Bootstrap frame constants
# ---------------------------------------------------------------------------
#
# Frame text has to be scanned to discover its protocol number *before* a codec
# is chosen, so these few values cannot come from the codec module. They are
# checked against it in load_core(), which means they can be wrong for exactly
# one function call and never silently.

BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
FRAME_MAGIC = "MCI"
PARITY_MAGIC = "MCP"
# Protocol 7 dropped the header version field: the magic strings name the frame
# format. A future format changes the magic, and this constant with it.
MAGIC_PROTOCOL = 7
IMAGE_ID_LEN = 4
DEFAULT_FRAME_HEADER_LEN = 8
DEFAULT_MESSAGE_LEN = 120


# ===========================================================================
# Module loading
# ===========================================================================
#
# Both siblings use hyphenated filenames, which are not legal Python
# identifiers, so they are loaded by path. Import order matters: the codec must
# be importable on its own, and the Constructor imports it in turn.


def _load_by_path(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ReconstructorError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _find_sibling(filename: str, explicit: Optional[str]) -> Path:
    candidates = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.append(Path(__file__).resolve().parent / filename)
    candidates.append(Path.cwd() / filename)
    for path in candidates:
        if path.is_file():
            return path.resolve()
    searched = "\n".join(f"  - {item}" for item in candidates)
    raise ReconstructorError(
        f"Cannot find {filename}.\n\n"
        f"Place it beside this file, or pass the matching option.\n\n"
        f"Searched:\n{searched}"
    )


@dataclass
class Core:
    """The loaded codec/renderer pair and their identity strings."""

    compression: ModuleType
    constructor: Optional[ModuleType]
    compression_path: Path
    constructor_path: Optional[Path]

    @property
    def model(self) -> ModuleType:
        """The model module, loaded transitively by the codec."""
        return self.compression.model

    @property
    def protocol(self) -> int:
        return int(self.compression.PROTOCOL_VERSION)

    def require_constructor(self) -> ModuleType:
        """Return the Constructor module or explain why rendering is impossible."""
        if self.constructor is None:
            raise ReconstructorError(
                "Rendering needs MCoreIMG-Constructor.py, which could not be imported.\n"
                "Decoding and JSON export still work with --no-render."
            )
        return self.constructor


def load_core(compression: Optional[str] = None,
              constructor: Optional[str] = None,
              need_renderer: bool = True) -> Core:
    """Load the codec, and the Constructor when rendering is required.

    The Constructor is optional so that a headless receiver can decode frames
    and dump JSON without a Tk installation.
    """
    comp_path = _find_sibling(COMPRESSION_FILENAME, compression)
    try:
        comp = _load_by_path("mcoreimg_compression", comp_path)
    except Exception as exc:
        raise ReconstructorError(f"{comp_path}: cannot import codec: {exc}") from exc

    for attr in ("PROTOCOL_VERSION", "decode_frames", "encode_image", "model"):
        if not hasattr(comp, attr):
            raise ReconstructorError(
                f"{comp_path} is not a usable MCoreIMG codec (missing {attr})."
            )

    # The bootstrap frame constants above are used before this point. Confirm
    # they agree with the codec so a profile change cannot pass unnoticed.
    for name, local in (("FRAME_MAGIC", FRAME_MAGIC),
                        ("PARITY_MAGIC", PARITY_MAGIC),
                        ("PROTOCOL_VERSION", MAGIC_PROTOCOL),
                        ("IMAGE_ID_LEN", IMAGE_ID_LEN),
                        ("BASE62", BASE62),
                        ("FRAME_HEADER_LEN", DEFAULT_FRAME_HEADER_LEN),
                        ("MESSAGE_LEN", DEFAULT_MESSAGE_LEN)):
        actual = getattr(comp, name, None)
        if actual is not None and actual != local:
            raise ReconstructorError(
                f"This Reconstructor assumes {name}={local!r} while scanning frame "
                f"text, but the codec at {comp_path} uses {actual!r}.\n"
                "The transport profile changed; update the Reconstructor to match."
            )

    ctor = None
    ctor_path: Optional[Path] = None
    try:
        ctor_path = _find_sibling(CONSTRUCTOR_FILENAME, constructor)
        ctor = _load_by_path("mcoreimg_constructor", ctor_path)
    except Exception as exc:
        if need_renderer:
            raise ReconstructorError(
                f"Cannot import the Constructor renderer: {exc}\n"
                "Rendering requires it; use --no-render to decode only."
            ) from exc
        ctor = None

    if ctor is not None and int(getattr(ctor, "PROTOCOL_VERSION", -1)) != int(comp.PROTOCOL_VERSION):
        raise ReconstructorError(
            "Mismatched pair: the Constructor and the codec disagree about the protocol.\n"
            f"  {ctor_path}: protocol {getattr(ctor, 'PROTOCOL_VERSION', '?')}\n"
            f"  {comp_path}: protocol {comp.PROTOCOL_VERSION}"
        )

    return Core(comp, ctor, comp_path, ctor_path)


@dataclass
class DecodedAsset:
    """Decoded commands plus a human description of where they came from."""

    commands: list
    source_description: str
    protocol: int
    frames: Optional[list] = None
    document: Any = None
    recovered: Optional[list] = None
    image_id: Optional[str] = None


# ==========================================================================
# Frame text handling
# ==========================================================================

# =============================================================================
# Transport header primitives
# =============================================================================
def decode_base62(text: str) -> int:
    """Decode the protocol's big-endian Base62 integer representation.

    Frame header fields use the alphabet in ``BASE62``.  This helper is kept
    intentionally strict so malformed headers fail before core discovery or
    decoder invocation can produce misleading compatibility errors.
    """
    value = 0
    if not text:
        raise ReconstructorError("Cannot decode an empty Base62 value.")
    for char in text:
        if char not in BASE62:
            raise ReconstructorError(f"Invalid Base62 character: {char!r}")
        value = value * 62 + BASE62.index(char)
    return value


def frame_protocol_version(frame: str) -> int:
    """Return the protocol a frame belongs to.

    Since protocol 7 the magic string itself names the frame format, so a
    recognised magic implies MAGIC_PROTOCOL.
    """
    frame = frame.strip()
    if not frame.startswith((FRAME_MAGIC, PARITY_MAGIC)):
        raise ReconstructorError("Not an MCoreIMG frame.")
    return MAGIC_PROTOCOL


def _frame_length_at(text: str, start: int) -> Optional[int]:
    """Calculate a complete frame length from an 8-character protocol-7 header.

    Data frames ("MCI"): chunking fills every frame except the last, so a
    non-final frame is exactly MESSAGE_LEN characters and the final frame runs
    to the end of the line.

    Parity frames ("MCP"): the descriptor carries the data frame count k. With
    k > 1 parity is as long as a full data frame, so the frame is exactly
    MESSAGE_LEN; with k = 1 it matches the single data frame and runs to the
    end of the line.

    Returning ``None`` means the text at ``start`` is not safely parseable,
    which keeps ordinary chat text beginning with "MCI" from being consumed.
    """
    magic = text[start:start + 3]
    if magic not in (FRAME_MAGIC, PARITY_MAGIC):
        return None
    if len(text) - start < DEFAULT_FRAME_HEADER_LEN:
        return None
    header = text[start:start + DEFAULT_FRAME_HEADER_LEN]
    try:
        decode_base62(header[3:3 + IMAGE_ID_LEN])   # image id
        descriptor = decode_base62(header[-1])
    except ReconstructorError:
        return None
    if descriptor >= 40:
        return None
    if magic == PARITY_MAGIC:
        runs_to_end = (descriptor % 20) // 2 + 1 == 1
    else:
        runs_to_end = (descriptor % 20) >= 10   # final data frame
    if not runs_to_end:
        total = DEFAULT_MESSAGE_LEN
        return total if start + total <= len(text) else None
    total = len(text) - start
    if total < DEFAULT_FRAME_HEADER_LEN or total > DEFAULT_MESSAGE_LEN:
        return None
    return total


def _next_magic(line: str, cursor: int) -> int:
    hits = [i for i in (line.find(FRAME_MAGIC, cursor), line.find(PARITY_MAGIC, cursor)) if i >= 0]
    return min(hits) if hits else -1


def extract_frames(text: str) -> list[str]:
    """Extract unique complete data and parity frames from exports or transcripts.

    The frames may belong to any number of images; sorting them into sets is
    the codec's FrameSorter's job, not this scanner's.

    Non-final frames are fixed length, so they can be sliced out of a line
    containing several.  A final frame runs to the end of its line.  A
    one-frame-per-line fallback covers anything the primary scan misses.
    Mixed protocol versions are rejected because they cannot form one image.
    """
    frames: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        cursor = 0
        found = False
        while True:
            start = _next_magic(line, cursor)
            if start < 0:
                break
            length = _frame_length_at(line, start)
            if length is not None and start + length <= len(line):
                candidate = line[start:start + length]
                if all(33 <= ord(char) <= 126 for char in candidate):
                    frames.append(candidate)
                    cursor = start + length
                    found = True
                    continue
            cursor = start + 3

        # Fallback for development frame variants whose payload-length field
        # moved while retaining one frame per line.
        if (not found and line.startswith((FRAME_MAGIC, PARITY_MAGIC))
                and not any(char.isspace() for char in line)):
            frames.append(line)

    unique: list[str] = []
    seen: set[str] = set()
    for frame in frames:
        if frame not in seen:
            seen.add(frame)
            unique.append(frame)
    if not unique:
        raise ReconstructorError("No MCoreIMG frames beginning with 'MCI' or 'MCP' were found.")
    return unique


def read_transport_frames(path: Path) -> list[str]:
    """Read a transport file and extract its MCI frames.

    ASCII is expected for transport, with UTF-8 fallback so copied text files
    containing harmless surrounding Unicode remain usable.
    """
    try:
        text = path.read_text(encoding="ascii")
    except UnicodeDecodeError:
        text = path.read_text(encoding="utf-8")
    return extract_frames(text)

# ==========================================================================
# Input classification
# ==========================================================================

def is_editable_json(path: Path) -> bool:
    """Return whether a path should be treated as editable MCoreIMG JSON."""
    suffixes = [suffix.lower() for suffix in path.suffixes]
    return suffixes[-2:] == [".mci", ".json"] or path.suffix.lower() == ".json"


def inspect_source_protocol(path: Path) -> Optional[int]:
    """Read only enough source JSON to determine its requested protocol.

    Source files may omit ``protocol_version``; in that case automatic discovery
    selects the newest compatible core.  Present values are validated against
    the single Base62 protocol-version field.
    """
    if not is_editable_json(path):
        return None
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ReconstructorError(f"Could not inspect source JSON: {exc}") from exc
    protocol = obj.get("protocol_version")
    if protocol is None:
        return None
    try:
        protocol_int = int(protocol)
    except (TypeError, ValueError) as exc:
        raise ReconstructorError(f"Invalid source protocol_version: {protocol!r}") from exc
    if not (0 <= protocol_int < len(BASE62)):
        raise ReconstructorError(f"Unsupported source protocol version: {protocol_int}")
    return protocol_int


def prepare_input(path: Path) -> tuple[Optional[list[str]], Optional[int], str]:
    """Classify an input and perform protocol detection before core discovery.

    Transport inputs are parsed once here so the same verified frame list can
    be passed into decoding later.  Source/SVG inputs are deferred to the
    Constructor loader.
    """
    if is_editable_json(path):
        return None, inspect_source_protocol(path), "editable source"
    if path.suffix.lower() in {".svg", ".svgz"}:
        return None, None, "SVG source"
    frames = read_transport_frames(path)
    return frames, frame_protocol_version(frames[0]), "transport"


# =============================================================================
# Input selection, frame extraction, and protocol detection
# =============================================================================
def choose_input_file() -> Optional[Path]:
    """Open a Tk file chooser when the CLI input path is omitted.

    Tkinter remains optional for headless/explicit-path use.  The hidden root is
    destroyed immediately after selection to avoid leaving a background window.
    """
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as exc:
        raise ReconstructorError("No input was supplied and Tkinter is unavailable.") from exc

    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
    except tk.TclError:
        pass
    filename = filedialog.askopenfilename(
        title="Select MCoreIMG transport or source",
        filetypes=[
            ("MCoreIMG transport", "*.mci *.txt"),
            ("MCoreIMG source", "*.mci.json *.json"),
            ("SVG source", "*.svg *.svgz"),
            ("All files", "*.*"),
        ],
    )
    root.destroy()
    return Path(filename) if filename else None


def default_output_path(input_path: Path) -> Path:
    """Create a collision-resistant timestamped PNG path beside the input."""
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = input_path.name
    stem = name[:-9] if name.lower().endswith(".mci.json") else input_path.stem
    return input_path.with_name(f"{stem}-reconstructed-{timestamp}.png")


def open_output(path: Path) -> None:
    """Ask the desktop to open the completed PNG without affecting success status.

    Image reconstruction is already complete when this function runs, so desktop
    integration failures are intentionally ignored.
    """
    try:
        if sys.platform.startswith("linux"):
            subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]
    except Exception:
        pass

# ==========================================================================
# JSON export
# ==========================================================================

# =============================================================================
# Best-effort editable JSON recovery
# =============================================================================
# Rendering can be lossless even when authoring metadata cannot be recovered.
def _jsonify(value: Any, seen: Optional[set[int]] = None) -> Any:
    """Convert arbitrary Constructor objects into JSON-safe diagnostic data.

    Dataclasses, mappings, containers, ``to_json`` methods, and public instance
    attributes are supported.  Cycle detection produces an explicit marker
    rather than recursing forever through parent/back-reference relationships.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if seen is None:
        seen = set()
    identity = id(value)
    if identity in seen:
        return "<recursive-reference>"
    seen.add(identity)

    if isinstance(value, Mapping):
        return {str(key): _jsonify(item, seen) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonify(item, seen) for item in value]
    if is_dataclass(value):
        return _jsonify(asdict(value), seen)
    method = getattr(value, "to_json", None)
    if callable(method):
        try:
            return _jsonify(method(), seen)
        except TypeError:
            pass
    if hasattr(value, "__dict__"):
        return {
            key: _jsonify(item, seen)
            for key, item in vars(value).items()
            if not key.startswith("_") and not callable(item)
        }
    return str(value)


def command_to_json(command: Any) -> dict[str, Any]:
    """Serialize one command-like object into a JSON mapping."""
    value = _jsonify(command)
    if isinstance(value, dict):
        return value
    return {"value": value}

# ===========================================================================
# Decode, render, export
# ===========================================================================


def decode_assets(core: Core, path: Path, frames: Optional[list]):
    """Decode every image in the input.

    A transport input may hold frames from any number of images, interleaved,
    as a channel log would. They are sorted into per-image sets by image id and
    each complete set is decoded independently. Returns (assets, incomplete),
    where incomplete lists the sets that could not be completed.
    """
    if frames is not None:
        mci = core.compression
        images, incomplete, sorter = mci.sort_and_decode(frames)
        assets = []
        present = {f.strip() for f in frames}
        for image in images:
            # Report what the input held for this image, not just what the
            # sorter needed: it completes as soon as it can, so parity frames
            # later in the input arrive as duplicates of a finished image.
            received = image.k - len(image.recovered)
            parity_held = sum(1 for f in mci.build_parity_frames(image.frames) if f in present)
            description = (f"image {image.image_id}: {received}/{image.k} data + "
                           f"{parity_held}/{mci.PARITY_FRAMES} parity frame(s)")
            if image.recovered:
                parts = ", ".join(str(j + 1) for j in image.recovered)
                description += f"; rebuilt part(s) {parts} from parity"
            assets.append(DecodedAsset(
                commands=list(image.commands),
                source_description=description,
                protocol=core.protocol,
                frames=list(image.frames),
                recovered=list(image.recovered),
                image_id=image.image_id,
            ))
        notes = []
        if sorter.rejected:
            notes.append(f"{sorter.rejected} frame-like line(s) could not be parsed and were skipped")
        if sorter.duplicates:
            notes.append(f"{sorter.duplicates} duplicate frame(s) merged")
        return assets, incomplete, notes

    ctor = core.require_constructor()
    try:
        document = ctor.load_source(path)
    except Exception as exc:
        raise ReconstructorError(f"Cannot load source file {path.name}: {exc}") from exc
    commands = document.transformed_commands()
    asset = DecodedAsset(
        commands=list(commands),
        source_description=f"source document {path.name} ({len(commands)} command(s))",
        protocol=core.protocol,
        document=document,
    )
    return [asset], [], []


def decode_asset(core: Core, path: Path, frames: Optional[list]) -> DecodedAsset:
    """Decode a single-image input, failing clearly if it holds anything else."""
    assets, incomplete, _notes = decode_assets(core, path, frames)
    if len(assets) == 1 and not incomplete:
        return assets[0]
    if not assets:
        detail = "; ".join(status.describe() for status in incomplete) or "no complete image"
        raise ReconstructorError(f"Frame decoding failed: {detail}")
    raise ReconstructorError(
        f"Input holds {len(assets)} decodable image(s) and {len(incomplete)} incomplete set(s); "
        "use decode_assets for multi-image input.")


def render_asset(core: Core, asset: DecodedAsset):
    """Render decoded commands with the Constructor's authoritative renderer."""
    ctor = core.require_constructor()
    image = ctor.render_to_pillow(asset.commands)
    if image is None:
        raise ReconstructorError(
            "Rendering returned no image. Pillow is required:\n"
            "  python -m pip install Pillow"
        )
    return image


def list_commands(asset: DecodedAsset, names: dict) -> None:
    """Print a readable listing of the decoded command stream."""
    if not asset.commands:
        print("No commands were decoded.")
        return
    print(f"Decoded {len(asset.commands)} command(s):")
    for index, command in enumerate(asset.commands):
        label = names.get(getattr(command, "opcode", None), "Unknown")
        style = getattr(command, "style", None)
        fill = getattr(style, "fill", None)
        stroke = getattr(style, "stroke", None)
        parts = [f"  [{index:3d}] {label}"]
        if fill:
            parts.append(f"fill={fill}")
        if stroke:
            parts.append(f"stroke={stroke} width={getattr(style, 'stroke_width', '?')}")
        group = getattr(command, "editor_group", None)
        if group is not None:
            parts.append(f"group={group}")
        print(" ".join(parts))


def dump_source_json(core: Core, asset: DecodedAsset, input_path: Path,
                     output_path: Path) -> Path:
    """Write a Constructor-compatible editable source document."""
    ctor = core.constructor
    commands = [command_to_json(c) for c in asset.commands]
    payload = {
        "format": getattr(ctor, "SOURCE_FORMAT", "MCoreIMG-SVG-source"),
        "version": getattr(ctor, "SOURCE_VERSION", core.protocol),
        "protocol_version": core.protocol,
        "canvas": {"width": core.compression.CANVAS_W, "height": core.compression.CANVAS_H},
        "source_name": input_path.stem,
        "transform": {"scale": 1.0, "offset_x": 0.0, "offset_y": 0.0},
        "commands": commands,
        "warnings": [],
        "reconstructed": {
            "by": RECONSTRUCTOR_BUILD,
            "at": datetime.now().isoformat(timespec="seconds"),
            "from": input_path.name,
            "codec": getattr(core.compression, "COMPRESSION_BUILD", "unknown"),
        },
    }
    output_path.write_text(json.dumps(_jsonify(payload), indent=2) + "\n", encoding="utf-8")
    return output_path


# ===========================================================================
# Self-test
# ===========================================================================


def run_self_test(core: Core) -> None:
    """Round-trip the codec and renderer through the loaded modules."""
    mci = core.compression
    checks = 0

    # 1. Frame round trip of a simple document.
    style = mci.PaintStyle("#FF0000", "#000000", 2.0)
    commands = [
        mci.VectorCommand(mci.OP_RECT, style, {"x": 10, "y": 10, "w": 120, "h": 80}),
        mci.VectorCommand(mci.OP_ELLIPSE, style, {"cx": 200, "cy": 120, "rx": 40, "ry": 30}),
    ]
    encoded = mci.encode_image(commands)
    decoded = mci.decode_frames(encoded.frames)
    assert len(decoded) == len(commands), "command count changed across the round trip"
    checks += 1

    # 2. Frame extraction from surrounding chat noise.
    noisy = "log line\n" + "\n".join(encoded.frames) + "\ntrailing text\n"
    recovered = extract_frames(noisy)
    assert recovered == list(encoded.frames), "frame extraction did not recover the frame set"
    checks += 1

    # 3. Corruption must be rejected, not silently rendered.
    damaged = list(encoded.frames)
    body = damaged[0][mci.FRAME_HEADER_LEN:]
    if body:
        swapped = "!" if body[0] != "!" else "#"
        damaged[0] = damaged[0][:mci.FRAME_HEADER_LEN] + swapped + body[1:]
        try:
            mci.decode_frames(damaged)
        except mci.MCIError:
            checks += 1
        else:
            raise AssertionError("a corrupted frame decoded without error")
    else:
        checks += 1

    # 4. Missing parts must be rejected.
    if len(encoded.frames) > 1:
        try:
            mci.decode_frames(list(encoded.frames)[:-1])
        except mci.MCIError:
            checks += 1
        else:
            raise AssertionError("an incomplete frame set decoded without error")
    else:
        checks += 1

    # 5. Protocol number in the header must match the codec.
    assert frame_protocol_version(encoded.frames[0]) == core.protocol
    checks += 1

    # 6. Rendering, when a Constructor is present.
    if core.constructor is not None:
        image = render_asset(core, DecodedAsset(decoded, "self-test", core.protocol))
        assert image.size == (mci.CANVAS_W, mci.CANVAS_H), "rendered size is not the canvas size"
        checks += 1

    # 7. RAID 6 parity: any one or two of the k + 2 messages may be lost.
    import itertools
    import random
    rng = random.Random(6)
    shapes = [
        mci.VectorCommand(mci.OP_RECT, mci.PaintStyle(rng.choice(["#204060", "#C0A080", "#FF0000"]), None, 1.0),
                          {"x": rng.randrange(600), "y": rng.randrange(400),
                           "w": rng.randrange(2, 90), "h": rng.randrange(2, 70)})
        for _ in range(60)
    ]
    big = mci.encode_image(shapes)
    assert big.stats.fits and len(big.frames) >= 3, "parity self-test image should span several frames"
    assert len(big.parity_frames) == mci.PARITY_FRAMES, "encoder did not emit parity frames"
    sent = big.transmit_frames
    expected = [c.to_json() for c in mci.decode_frames(big.frames)]
    for lost in itertools.chain(itertools.combinations(range(len(sent)), 1),
                                itertools.combinations(range(len(sent)), 2)):
        received = [f for i, f in enumerate(sent) if i not in lost]
        rng.shuffle(received)
        got = [c.to_json() for c in mci.decode_frames(received)]
        assert got == expected, f"parity recovery failed with messages {lost} lost"
    checks += 1

    # 8. Three losses exceed RAID 6 and must be refused, never mis-decoded.
    for lost in itertools.combinations(range(len(sent)), 3):
        try:
            mci.decode_frames([f for i, f in enumerate(sent) if i not in lost])
        except mci.MCIError:
            continue
        raise AssertionError(f"decoded with three messages {lost} lost")
    checks += 1

    # 9. Parity frames are found in pasted text, including several per line.
    pasted = "chat noise MCI not a frame\n" + "".join(sent[:3]) + "\n" + "\n".join(sent[3:]) + "\n"
    assert sorted(extract_frames(pasted)) == sorted(sent), "frame scan missed data or parity frames"
    checks += 1

    # 10. Several images interleaved on one channel decode separately.
    others = [mci.encode_image(shapes[i::3]) for i in range(3)]
    channel = []
    for index, other in enumerate(others):
        sent_other = other.transmit_frames
        channel += [f for i, f in enumerate(sent_other) if i != index]  # each loses one message
    channel += channel[:2]                                               # and re-sends happen
    rng.shuffle(channel)
    images, incomplete, _sorter = mci.sort_and_decode(channel + ["chat noise", "MCI short"])
    assert not incomplete, "an interleaved image was left incomplete"
    by_id = {image.image_id: [c.to_json() for c in image.commands] for image in images}
    assert len(by_id) == len(others), "interleaved images were not kept apart"
    for other in others:
        assert by_id[other.image_id] == [c.to_json() for c in mci.decode_frames(other.frames)], \
            "an interleaved image decoded wrongly"
    checks += 1

    print(f"MCoreIMG Reconstructor self-test: PASS ({checks} checks)")
    print(f"protocol={core.protocol} frames={len(encoded.frames)} commands={len(decoded)} "
          f"parity_patterns={len(sent) + len(sent) * (len(sent) - 1) // 2}")


# ===========================================================================
# Command line
# ===========================================================================


def build_parser() -> argparse.ArgumentParser:
    """Define the command-line interface without performing any I/O."""
    parser = argparse.ArgumentParser(
        description="Reconstruct MCoreIMG transports using the paired codec and renderer.")
    parser.add_argument("input", nargs="?",
                        help="Input .mci, text, .mci.json, .json, .svg, or .svgz file")
    parser.add_argument("-o", "--output", help="Output PNG path")
    parser.add_argument("--compression", metavar="PATH",
                        help=f"Path to {COMPRESSION_FILENAME}")
    parser.add_argument("--constructor", metavar="PATH",
                        help=f"Path to {CONSTRUCTOR_FILENAME}")
    parser.add_argument("--protocol", type=int,
                        help="Require a specific protocol number")
    parser.add_argument("--dump-json", action="store_true",
                        help="Write a Constructor-compatible source JSON")
    parser.add_argument("--list-commands", action="store_true",
                        help="Print the decoded command stream")
    parser.add_argument("--no-render", action="store_true",
                        help="Decode only; do not produce a PNG")
    parser.add_argument("--no-open", action="store_true",
                        help="Do not automatically open the PNG")
    parser.add_argument("--self-test", action="store_true",
                        help="Run codec and rendering round-trip tests")
    parser.add_argument("--show-core", action="store_true",
                        help="Print the loaded modules and exit")
    return parser


def _print_core(core: Core) -> None:
    """Print the loaded pair so a mismatch is obvious in any bug report."""
    mci = core.compression
    print(f"Codec:       {core.compression_path}")
    print(f"             {getattr(mci, 'COMPRESSION_BUILD', 'unknown')}")
    if core.constructor is not None:
        print(f"Constructor: {core.constructor_path}")
        print(f"             {getattr(core.constructor, 'CONSTRUCTOR_BUILD', 'unknown')}")
        signature = getattr(core.constructor, "FEATURE_SIGNATURE", "")
        if signature:
            print(f"Features:    {signature}")
    else:
        print("Constructor: not loaded (decode-only mode)")
    print(f"Model:       {getattr(core.model, '__file__', 'unknown')}")
    print(f"             {getattr(core.model, 'MODEL_BUILD', 'unknown')}")
    print(f"Protocol:    {core.protocol}")
    print(f"Envelope:    {mci.MAX_MESSAGES} message(s) x {mci.MESSAGE_LEN} characters")


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run CLI orchestration and convert expected failures into exit codes."""
    args = build_parser().parse_args(argv)
    try:
        if args.self_test or args.show_core:
            core = load_core(args.compression, args.constructor,
                             need_renderer=not args.no_render)
            _print_core(core)
            if args.protocol is not None and args.protocol != core.protocol:
                raise ReconstructorError(
                    f"Loaded codec is protocol {core.protocol}, "
                    f"but --protocol requested {args.protocol}.")
            if args.show_core and not args.self_test:
                return 0
            run_self_test(core)
            return 0

        input_path = Path(args.input).expanduser() if args.input else choose_input_file()
        if input_path is None:
            print("No file selected.")
            return 1
        input_path = input_path.resolve()
        if not input_path.is_file():
            raise ReconstructorError(f"Input file does not exist: {input_path}")

        frames, detected_protocol, _kind = prepare_input(input_path)
        if args.protocol is not None and detected_protocol is not None \
                and args.protocol != detected_protocol:
            raise ReconstructorError(
                f"Input uses protocol {detected_protocol}, "
                f"but --protocol requested {args.protocol}.")

        core = load_core(args.compression, args.constructor,
                         need_renderer=not args.no_render)

        # Only transport frames are bound to a wire protocol. An editable source
        # file is commands in JSON; its protocol_version records where it came
        # from and does not stop it rendering under a newer codec.
        required = args.protocol if args.protocol is not None else detected_protocol
        if frames is None and args.protocol is None:
            required = None
        if required is not None and required != core.protocol:
            raise ReconstructorError(
                f"Input needs protocol {required}, but the codec beside this file is "
                f"protocol {core.protocol}.\n"
                f"  Codec: {core.compression_path}\n"
                "Pair this Reconstructor with the matching codec, or pass --compression.")

        assets, incomplete, notes = decode_assets(core, input_path, frames)

        print(f"MCoreIMG Reconstructor {RECONSTRUCTOR_BUILD}")
        _print_core(core)
        if frames is not None:
            print(f"Input: {len(frames)} frame(s) from {input_path.name}, "
                  f"{len(assets)} image(s) decoded, {len(incomplete)} incomplete")
        for note in notes:
            print(f"Note: {note}")
        for status in incomplete:
            print(f"Incomplete: {status.describe()}", file=sys.stderr)
        if not assets:
            raise ReconstructorError("No complete image could be decoded from the input.")

        many = len(assets) > 1
        base_output = (Path(args.output).expanduser().resolve()
                       if args.output else default_output_path(input_path))
        written = []
        for asset in assets:
            print(f"Loaded: {asset.source_description}")
            print(f"Decoded commands: {len(asset.commands)}")
            if args.list_commands:
                list_commands(asset, core.compression.OP_NAMES)

            # Several images share one base name, told apart by image id.
            output_path = base_output
            if many:
                output_path = base_output.with_name(f"{base_output.stem}-{asset.image_id}{base_output.suffix}")
            if not args.no_render:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                image = render_asset(core, asset)
                try:
                    image.save(output_path)
                    size, mode = image.size, image.mode
                finally:
                    close = getattr(image, "close", None)
                    if callable(close):
                        close()
                print(f"PNG: {size[0]}x{size[1]} {mode}")
                print(f"Image reconstruction complete: {output_path}")
                written.append(output_path)

            if args.dump_json:
                base = output_path if not args.no_render else (
                    input_path.with_name(f"{input_path.stem}-{asset.image_id}") if many else input_path)
                json_path = base.with_suffix(".mci.json")
                dump_source_json(core, asset, input_path, json_path)
                print(f"Decoded source JSON: {json_path}")

        # Opening one viewer is helpful; opening one per image in a channel log is not.
        if len(written) == 1 and not args.no_open:
            open_output(written[0])
        elif len(written) > 1 and not args.no_open:
            print(f"Wrote {len(written)} images; not opening them automatically.")
        return 0

    except ReconstructorError as exc:
        print(f"Reconstruction failed:\n{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Reconstruction cancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
