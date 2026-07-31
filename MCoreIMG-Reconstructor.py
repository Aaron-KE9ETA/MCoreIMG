#!/usr/bin/env python3
from __future__ import annotations
"""
MCoreIMG SVG Reconstructor — protocol-aware alpha build
=======================================================

Reconstructs protocol-v2 and protocol-v3 MCoreIMG SVG/vector transports.
The matching Constructor is loaded as the codec/rendering core so every shape,
path, compression, palette, alpha, and rendering feature stays synchronized.

Protocol 3 synchronization includes:
* RGB565+A4 palette entries.
* RGBA PNG output and source-over alpha compositing.
* Ten-message MeshCore transport envelope.
* SVG paths, open-subpath filling, nested transforms, predictive coordinates,
  opcode-local state, and nonadjacent translated-repeat references.

Put this file beside MCoreIMG-Constructor.py (or a versioned SVG Constructor),
or use --core to specify it explicitly.
"""

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any, Iterable, Optional, Sequence

RECONSTRUCTOR_BUILD = "2026.07.31-svg-v3.3-alpha-protocol-aware-FIXED"
SUPPORTED_PROTOCOL_VERSIONS = (2, 3)
PREFERRED_PROTOCOL_VERSION = 3
BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

PREFERRED_CORE_FILENAMES = (
    "MCoreIMG-SVG-Constructor-v3.3-ALPHA-ROUNDTRIP.py",
    "MCoreIMG-SVG-Constructor.py",
    "MCoreIMG-Constructor.py",
)
CORE_GLOB_PATTERNS = (
    "MCoreIMG-SVG-Constructor*.py",
    "MCoreIMG-Constructor*.py",
)
FRAME_TOKEN_RE = re.compile(r"MCI[!-~]{12,147}")


class ReconstructorError(RuntimeError):
    pass


def decode_base62_digit(ch: str) -> int:
    if len(ch) != 1 or ch not in BASE62:
        raise ReconstructorError(f"Invalid Base62 protocol character: {ch!r}")
    return BASE62.index(ch)


def frame_protocol_version(frame: str) -> int:
    frame = frame.strip()
    if len(frame) < 4 or not frame.startswith("MCI"):
        raise ReconstructorError("Cannot determine protocol version from malformed MCI frame.")
    return decode_base62_digit(frame[3])


def _unique_paths(paths: Iterable[Path]) -> list[Path]:
    seen: set[Path] = set()
    result: list[Path] = []
    for path in paths:
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            resolved = path.expanduser().absolute()
        if resolved not in seen:
            seen.add(resolved)
            result.append(resolved)
    return result


def _candidate_core_paths(explicit: Optional[str]) -> list[Path]:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))

    script_path = Path(__file__).resolve()
    directories = _unique_paths((script_path.parent, Path.cwd()))
    for directory in directories:
        for filename in PREFERRED_CORE_FILENAMES:
            candidates.append(directory / filename)
        for pattern in CORE_GLOB_PATTERNS:
            candidates.extend(sorted(directory.glob(pattern)))

    return [
        path for path in _unique_paths(candidates)
        if path != script_path and "reconstructor" not in path.name.lower()
    ]


def _import_core(path: Path) -> ModuleType:
    module_name = f"_mcoreimg_svg_core_{abs(hash(path.resolve()))}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError("Python could not create an import specification.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def load_constructor_core(
    explicit: Optional[str] = None,
    required_protocol: Optional[int] = None,
) -> tuple[ModuleType, Path]:
    """Load a matching Constructor core; protocol 3 is preferred by default."""
    failures: list[str] = []
    accepted: list[tuple[int, int, ModuleType, Path]] = []

    for order, path in enumerate(_candidate_core_paths(explicit)):
        if not path.is_file():
            continue
        try:
            module = _import_core(path)
        except Exception as exc:
            failures.append(f"{path}: import failed: {exc}")
            continue

        protocol = getattr(module, "PROTOCOL_VERSION", None)
        if not isinstance(protocol, int):
            failures.append(f"{path}: missing integer PROTOCOL_VERSION")
            continue
        if protocol not in SUPPORTED_PROTOCOL_VERSIONS:
            failures.append(
                f"{path}: protocol {protocol}; supported protocols are "
                f"{', '.join(map(str, SUPPORTED_PROTOCOL_VERSIONS))}"
            )
            continue
        if required_protocol is not None and protocol != required_protocol:
            failures.append(
                f"{path}: protocol {protocol}; input requires protocol {required_protocol}"
            )
            continue

        required_functions = ("decode_frames", "render_to_pillow")
        missing = [name for name in required_functions if not callable(getattr(module, name, None))]
        if missing:
            failures.append(f"{path}: missing required function(s): {', '.join(missing)}")
            continue

        accepted.append((protocol, -order, module, path.resolve()))

    if accepted:
        accepted.sort(key=lambda item: (item[0], item[1]), reverse=True)
        _protocol, _preference, module, path = accepted[0]
        return module, path

    searched = "\n".join(f"  - {path}" for path in _candidate_core_paths(explicit))
    rejected = "\n".join(f"  - {failure}" for failure in failures)
    target = f"protocol {required_protocol}" if required_protocol is not None else "protocol 3 or 2"
    message = (
        f"A compatible MCoreIMG SVG Constructor core for {target} was not found.\n\n"
        "Place this reconstructor beside MCoreIMG-Constructor.py, or pass:\n"
        "  --core /path/to/MCoreIMG-Constructor.py\n\n"
        f"Searched:\n{searched}"
    )
    if rejected:
        message += f"\n\nRejected candidates:\n{rejected}"
    raise ReconstructorError(message)


def choose_input_file() -> Optional[Path]:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as exc:
        raise ReconstructorError("No input file was supplied and Tkinter is unavailable.") from exc

    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
    except tk.TclError:
        pass
    filename = filedialog.askopenfilename(
        title="Select MCoreIMG transport or SVG source",
        filetypes=[
            ("MCoreIMG transport", "*.mci *.txt"),
            ("MCoreIMG SVG source", "*.mci.json *.json"),
            ("SVG source", "*.svg *.svgz"),
            ("All files", "*.*"),
        ],
    )
    root.destroy()
    return Path(filename) if filename else None


def _unique_strings(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def extract_frames(text: str) -> list[str]:
    frames: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("MCI") and not any(ch.isspace() for ch in line):
            frames.append(line)
        else:
            frames.extend(match.group(0) for match in FRAME_TOKEN_RE.finditer(line))

    frames = _unique_strings(frames)
    if not frames:
        raise ReconstructorError("No MCoreIMG frames beginning with 'MCI' were found.")

    versions = {frame_protocol_version(frame) for frame in frames}
    if len(versions) != 1:
        raise ReconstructorError(
            "Input contains mixed MCoreIMG protocol versions: "
            + ", ".join(map(str, sorted(versions)))
        )
    version = next(iter(versions))
    if version not in SUPPORTED_PROTOCOL_VERSIONS:
        raise ReconstructorError(
            f"Input uses protocol {version}; this reconstructor supports protocols "
            f"{', '.join(map(str, SUPPORTED_PROTOCOL_VERSIONS))}."
        )
    return frames


def read_transport_frames(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="ascii")
    except UnicodeDecodeError:
        text = path.read_text(encoding="utf-8")
    return extract_frames(text)


def is_editable_json(path: Path) -> bool:
    suffixes = [suffix.lower() for suffix in path.suffixes]
    return suffixes[-2:] == [".mci", ".json"] or path.suffix.lower() == ".json"


def inspect_source_protocol(path: Path) -> Optional[int]:
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
        protocol = int(protocol)
    except (TypeError, ValueError) as exc:
        raise ReconstructorError(f"Invalid source protocol_version: {protocol!r}") from exc
    if protocol not in SUPPORTED_PROTOCOL_VERSIONS:
        raise ReconstructorError(f"Unsupported source protocol version: {protocol}")
    return protocol


def prepare_input(path: Path) -> tuple[Optional[list[str]], Optional[int], str]:
    if is_editable_json(path):
        return None, inspect_source_protocol(path), "editable source"
    if path.suffix.lower() in {".svg", ".svgz"}:
        return None, None, "SVG source"
    frames = read_transport_frames(path)
    return frames, frame_protocol_version(frames[0]), "transport"


def load_commands(
    core: ModuleType,
    input_path: Path,
    prepared_frames: Optional[Sequence[str]] = None,
) -> tuple[list[Any], str]:
    if is_editable_json(input_path) or input_path.suffix.lower() in {".svg", ".svgz"}:
        load_source = getattr(core, "load_source", None)
        if not callable(load_source):
            raise ReconstructorError("The selected Constructor cannot load SVG/source files.")
        try:
            document = load_source(input_path)
        except Exception as exc:
            raise ReconstructorError(f"Source loading failed: {exc}") from exc
        transformed = getattr(document, "transformed_commands", None)
        commands = transformed() if callable(transformed) else list(getattr(document, "commands", []))
        return list(commands), "editable/SVG source"

    frames = list(prepared_frames) if prepared_frames is not None else read_transport_frames(input_path)
    max_messages = int(getattr(core, "MAX_MESSAGES", 10))
    if len(frames) > max_messages:
        raise ReconstructorError(
            f"Found {len(frames)} frames, but protocol {getattr(core, 'PROTOCOL_VERSION', '?')} "
            f"allows at most {max_messages}."
        )
    try:
        commands = core.decode_frames(frames)
    except Exception as exc:
        raise ReconstructorError(f"Transport decoding failed: {exc}") from exc
    return list(commands), f"{len(frames)} transport frame(s)"


def command_to_json(command: Any) -> dict[str, Any]:
    method = getattr(command, "to_json", None)
    if callable(method):
        value = method()
        if isinstance(value, dict):
            return value
    style = getattr(command, "style", None)
    return {
        "opcode": getattr(command, "opcode", None),
        "style": {
            "fill": getattr(style, "fill", None),
            "stroke": getattr(style, "stroke", None),
            "stroke_width": getattr(style, "stroke_width", 1),
            "fill_rule": getattr(style, "fill_rule", "nonzero"),
        },
        "geom": getattr(command, "geom", {}),
        "label": getattr(command, "label", ""),
        "visible": getattr(command, "visible", True),
    }


def default_output_path(input_path: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = input_path.name
    stem = name[:-9] if name.lower().endswith(".mci.json") else input_path.stem
    return input_path.with_name(f"{stem}-reconstructed-{timestamp}.png")


def dump_source_json(
    core: ModuleType,
    commands: Sequence[Any],
    input_path: Path,
    output_path: Path,
) -> Path:
    payload = {
        "format": getattr(core, "SOURCE_FORMAT", "MCoreIMG-SVG-source"),
        "version": int(getattr(core, "SOURCE_VERSION", 1)),
        "protocol_version": int(getattr(core, "PROTOCOL_VERSION", PREFERRED_PROTOCOL_VERSION)),
        "canvas": {
            "width": int(getattr(core, "CANVAS_W", 720)),
            "height": int(getattr(core, "CANVAS_H", 480)),
        },
        "source_name": input_path.stem,
        "transform": {"scale": 1.0, "offset_x": 0.0, "offset_y": 0.0},
        "commands": [command_to_json(command) for command in commands],
        "warnings": [
            "Reconstructed from MCoreIMG transport; original SVG authoring metadata "
            "and non-transmitted labels are unavailable."
        ],
    }
    output_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return output_path


def open_output(path: Path) -> None:
    try:
        if sys.platform.startswith("linux"):
            subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]
    except Exception:
        pass


def alpha_nibble(color: Optional[str]) -> Optional[int]:
    if not color:
        return None
    text = str(color).strip().lstrip("#")
    if len(text) == 8:
        try:
            return round(int(text[6:8], 16) * 15 / 255)
        except ValueError:
            return None
    return 15


def alpha_color_count(commands: Sequence[Any]) -> int:
    colors: set[str] = set()
    for command in commands:
        style = getattr(command, "style", None)
        for color in (getattr(style, "fill", None), getattr(style, "stroke", None)):
            nibble = alpha_nibble(color)
            if color and nibble is not None and nibble < 15:
                colors.add(str(color).upper())
    return len(colors)


def list_commands(core: ModuleType, commands: Sequence[Any]) -> None:
    names = getattr(core, "OP_NAMES", {})
    for index, command in enumerate(commands):
        opcode = getattr(command, "opcode", None)
        name = names.get(opcode, f"Opcode {opcode}")
        style = getattr(command, "style", None)
        fill = getattr(style, "fill", None)
        stroke = getattr(style, "stroke", None)
        width = getattr(style, "stroke_width", None)
        print(
            f"{index:04d}  {name:<10} "
            f"fill={fill!s:<10} A4={alpha_nibble(fill)!s:<2} "
            f"stroke={stroke!s:<10} A4={alpha_nibble(stroke)!s:<2} "
            f"width={width!s:<4} geom={getattr(command, 'geom', {})}"
        )


def run_self_test(core: ModuleType) -> None:
    core_test = getattr(core, "run_self_test", None)
    if callable(core_test):
        core_test()

    sample_document = getattr(core, "sample_document", None)
    quantize_command = getattr(core, "quantize_command", None)
    encode_image = getattr(core, "encode_image", None)
    if not all(callable(v) for v in (sample_document, quantize_command, encode_image)):
        raise ReconstructorError("The selected Constructor lacks round-trip self-test helpers.")

    document = sample_document()
    source = [quantize_command(command) for command in getattr(document, "commands", [])]
    encoded = encode_image(source)
    decoded = core.decode_frames(encoded.frames)
    image = core.render_to_pillow(decoded)
    expected_size = (int(getattr(core, "CANVAS_W", 720)), int(getattr(core, "CANVAS_H", 480)))
    try:
        if image.size != expected_size:
            raise ReconstructorError(f"Unexpected rendered image size: {image.size}")
        protocol = int(getattr(core, "PROTOCOL_VERSION", 0))
        if protocol >= 3 and image.mode != "RGBA":
            raise ReconstructorError(f"Protocol 3 renderer returned {image.mode}; expected RGBA.")
    finally:
        image.close()
    if len(decoded) != len(source):
        raise ReconstructorError(f"Round trip changed command count: {len(source)} -> {len(decoded)}")
    print("MCoreIMG SVG Reconstructor self-test: PASS")
    print(
        f"Protocol {getattr(core, 'PROTOCOL_VERSION', '?')} | {len(decoded)} commands | "
        f"{len(encoded.frames)} frame(s) | Constructor build "
        f"{getattr(core, 'CONSTRUCTOR_BUILD', 'unknown')}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reconstruct protocol-v2/v3 MCoreIMG SVG/vector frames."
    )
    parser.add_argument("input", nargs="?", help="Input .mci, text, .mci.json, .svg, or .svgz file")
    parser.add_argument("-o", "--output", help="Output PNG path")
    parser.add_argument("--core", help="Path to the matching MCoreIMG Constructor Python file")
    parser.add_argument(
        "--protocol", type=int, choices=SUPPORTED_PROTOCOL_VERSIONS,
        help="Force protocol 2 or 3 for self-test or ambiguous source files",
    )
    parser.add_argument("--dump-json", action="store_true", help="Write Constructor-compatible source JSON")
    parser.add_argument("--list-commands", action="store_true", help="Print decoded commands and A4 alpha")
    parser.add_argument("--no-open", action="store_true", help="Do not open the reconstructed PNG")
    parser.add_argument("--self-test", action="store_true", help="Run codec/rendering round-trip tests")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.self_test:
            core, core_path = load_constructor_core(args.core, args.protocol)
            print(f"Codec core: {core_path}")
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
        if args.protocol is not None and detected_protocol is not None and args.protocol != detected_protocol:
            raise ReconstructorError(
                f"Input uses protocol {detected_protocol}, but --protocol requested {args.protocol}."
            )
        required_protocol = args.protocol or detected_protocol
        core, core_path = load_constructor_core(args.core, required_protocol)
        core_protocol = int(getattr(core, "PROTOCOL_VERSION", -1))

        commands, source_description = load_commands(core, input_path, frames)
        if not commands:
            raise ReconstructorError("The input decoded successfully but contains no commands.")
        if args.list_commands:
            list_commands(core, commands)

        output_path = Path(args.output).expanduser().resolve() if args.output else default_output_path(input_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            image = core.render_to_pillow(commands)
            image.save(output_path)
            image_mode = image.mode
            image.close()
        except Exception as exc:
            raise ReconstructorError(f"PNG rendering failed: {exc}") from exc

        print(f"MCoreIMG SVG Reconstructor {RECONSTRUCTOR_BUILD}")
        print(f"Codec core: {core_path}")
        print(f"Constructor build: {getattr(core, 'CONSTRUCTOR_BUILD', 'unknown')}")
        print(f"Protocol: {core_protocol}")
        print(f"Loaded: {source_description}")
        print(f"Decoded commands: {len(commands)}")
        if core_protocol >= 3:
            print(f"Palette/rendering: RGB565+A4 source-over alpha ({alpha_color_count(commands)} alpha color(s))")
        else:
            print("Palette/rendering: RGB565 opaque")
        print(f"PNG mode: {image_mode}")
        print(f"Image reconstruction complete: {output_path}")

        if args.dump_json:
            json_path = output_path.with_suffix(".mci.json")
            dump_source_json(core, commands, input_path, json_path)
            print(f"Decoded source JSON: {json_path}")
        if not args.no_open:
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
