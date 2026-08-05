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

RECONSTRUCTOR_BUILD = "2026.08.05-reconstructor-v6.0-SLIMHEADER"

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
DEFAULT_FRAME_HEADER_LEN = 8
DEFAULT_MESSAGE_LEN = 150


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
    """Read the one-character protocol version from an MCI frame.

    The transport header is authoritative for selecting a Constructor core.
    Filename and build-string heuristics are used only to rank cores that have
    already declared the required protocol.
    """
    frame = frame.strip()
    if len(frame) < 4 or not frame.startswith(FRAME_MAGIC):
        raise ReconstructorError("Cannot determine protocol from malformed MCI frame.")
    return decode_base62(frame[3])


def _frame_length_at(text: str, start: int) -> Optional[int]:
    """Calculate a complete frame length from the 8-character protocol-6 header.

    Protocol 6 dropped the payload-length field, because chunking fills every
    frame except the last: a non-final frame is always exactly MESSAGE_LEN
    characters, and the final frame simply runs to the end of the line.

    Returning ``None`` means the text at ``start`` is not safely parseable,
    which keeps ordinary chat text beginning with "MCI" from being consumed.
    """
    if text[start:start + 3] != FRAME_MAGIC:
        return None
    if len(text) - start < DEFAULT_FRAME_HEADER_LEN:
        return None
    header = text[start:start + DEFAULT_FRAME_HEADER_LEN]
    try:
        decode_base62(header[3])          # protocol
        part = decode_base62(header[7])   # index + final flag + coding mode
    except ReconstructorError:
        return None
    if part >= 40:
        return None
    final = (part % 20) >= 10
    if not final:
        total = DEFAULT_MESSAGE_LEN
        return total if start + total <= len(text) else None
    # Final frame: consume the rest of the line, bounded by the envelope.
    total = len(text) - start
    if total < DEFAULT_FRAME_HEADER_LEN or total > DEFAULT_MESSAGE_LEN:
        return None
    return total


def extract_frames(text: str) -> list[str]:
    """Extract unique complete MCI frames from exports or copied chat transcripts.

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
            start = line.find(FRAME_MAGIC, cursor)
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
        if not found and line.startswith(FRAME_MAGIC) and not any(char.isspace() for char in line):
            frames.append(line)

    unique: list[str] = []
    seen: set[str] = set()
    for frame in frames:
        if frame not in seen:
            seen.add(frame)
            unique.append(frame)
    if not unique:
        raise ReconstructorError("No MCoreIMG frames beginning with 'MCI' were found.")

    versions = {frame_protocol_version(frame) for frame in unique}
    if len(versions) != 1:
        raise ReconstructorError(
            "Input contains mixed MCoreIMG protocol versions: "
            + ", ".join(map(str, sorted(versions)))
        )
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


def decode_asset(core: Core, path: Path, frames: Optional[list]) -> DecodedAsset:
    """Decode transport frames, or load an editable/SVG source file."""
    if frames is not None:
        try:
            commands = core.compression.decode_frames(frames)
        except core.compression.MCIError as exc:
            raise ReconstructorError(f"Frame decoding failed: {exc}") from exc
        return DecodedAsset(
            commands=list(commands),
            source_description=f"{len(frames)} transport frame(s) from {path.name}",
            protocol=core.protocol,
            frames=list(frames),
        )

    ctor = core.require_constructor()
    try:
        document = ctor.load_source(path)
    except Exception as exc:
        raise ReconstructorError(f"Cannot load source file {path.name}: {exc}") from exc
    commands = document.transformed_commands()
    return DecodedAsset(
        commands=list(commands),
        source_description=f"source document {path.name} ({len(commands)} command(s))",
        protocol=core.protocol,
        document=document,
    )


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

    print(f"MCoreIMG Reconstructor self-test: PASS ({checks} checks)")
    print(f"protocol={core.protocol} frames={len(encoded.frames)} commands={len(decoded)}")


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

        required = args.protocol if args.protocol is not None else detected_protocol
        if required is not None and required != core.protocol:
            raise ReconstructorError(
                f"Input needs protocol {required}, but the codec beside this file is "
                f"protocol {core.protocol}.\n"
                f"  Codec: {core.compression_path}\n"
                "Pair this Reconstructor with the matching codec, or pass --compression.")

        asset = decode_asset(core, input_path, frames)

        print(f"MCoreIMG Reconstructor {RECONSTRUCTOR_BUILD}")
        _print_core(core)
        print(f"Loaded: {asset.source_description}")
        print(f"Decoded commands: {len(asset.commands)}")

        if args.list_commands:
            list_commands(asset, core.compression.OP_NAMES)

        output_path = None
        if not args.no_render:
            output_path = (Path(args.output).expanduser().resolve()
                           if args.output else default_output_path(input_path))
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

        if args.dump_json:
            base = output_path if output_path else input_path
            json_path = base.with_suffix(".mci.json")
            dump_source_json(core, asset, input_path, json_path)
            print(f"Decoded source JSON: {json_path}")

        if output_path and not args.no_open:
            open_output(output_path)
        return 0

    except ReconstructorError as exc:
        print(f"Reconstruction failed:\n{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Reconstruction cancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
