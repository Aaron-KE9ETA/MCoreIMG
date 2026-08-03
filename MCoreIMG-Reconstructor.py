#!/usr/bin/env python3
from __future__ import annotations
"""
MCoreIMG Reconstructor — current Constructor compatibility adapter
=================================================================

Reconstructs MCoreIMG transport by loading the matching Constructor as the
codec and rendering authority. The current Constructor is protocol 5 / source
version 5 and uses local-space SVG groups, hybrid legacy primitives, translated
group copies, RGB565+A4 alpha, and the ten-message MeshCore envelope.

Compatibility highlights
------------------------
* Prefers the current v5.1 local-space hybrid Constructor.
* Detects the transport protocol directly from each MCI frame header.
* Delegates protocol-v5 local-space groups and group-copy expansion to the
  matching Constructor decoder, preserving draw order and displayed geometry.
* Supports compact primitives and automatic primitive-versus-vector choices.
* Preserves alpha by using the Constructor's authoritative Pillow renderer.
* Supports canonical and versioned Constructor filenames.
* Retains compatibility with older protocol-v2/v3/v4 Constructor cores.
* Loads .mci, copied frame text, .mci.json, .json, .svg, and .svgz inputs.
* Can export a best-effort editable source with --dump-json.

Keep this file beside MCoreIMG-Constructor.py / MCoreIMG-SVG-Constructor.py, or
pass the exact Constructor path with --core.
"""

import argparse
import importlib.util
import inspect
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional, Sequence

RECONSTRUCTOR_BUILD = "2026.08.02-v5.1-localspace-hybrid-sync"
PREFERRED_PROTOCOL_VERSION = 5
BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
FRAME_MAGIC = "MCI"
DEFAULT_FRAME_HEADER_LEN = 15
DEFAULT_MESSAGE_LEN = 150

PREFERRED_CORE_FILENAMES = (
    "MCoreIMG-SVG-Constructor-v5.1-LOCALSPACE-HYBRID-VERIFIED.py",
    "MCoreIMG-Constructor-v5.1-LOCALSPACE-HYBRID-VERIFIED.py",
    "MCoreIMG-SVG-Constructor-v5.1.py",
    "MCoreIMG-Constructor-v5.1.py",
    "MCoreIMG-SVG-Constructor.py",
    "MCoreIMG-Constructor.py",
    "MCoreIMG-Constructor-v4.3.py",
    "MCoreIMG-SVG-Constructor-v4.3.py",
    "MCoreIMG-SVG-Constructor-v4.0-HYBRID.py",
    "MCoreIMG-SVG-Constructor-v3.3-ALPHA-ROUNDTRIP.py",
)
CORE_GLOB_PATTERNS = (
    "MCoreIMG-Constructor*.py",
    "MCoreIMG-SVG-Constructor*.py",
    "*MCoreIMG*Constructor*.py",
)

DECODER_NAMES = (
    "decode_frames",
    "decode_transport_frames",
    "decode_message_frames",
    "decode_mci_frames",
    "decode_transport",
    "decode_image",
    "reconstruct_frames",
    "decode",
)
HIGH_LEVEL_RENDER_NAMES = (
    "reconstruct_to_pillow",
    "decode_frames_to_pillow",
    "render_frames_to_pillow",
    "reconstruct_transport_to_pillow",
    "decode_and_render",
)
RENDERER_NAMES = (
    "render_to_pillow",
    "render_commands_to_pillow",
    "render_scene_to_pillow",
    "render_document_to_pillow",
    "render_scene",
    "render_document",
    "render_image",
    "rasterize_scene",
    "rasterize",
    "to_pillow",
)
SOURCE_LOADER_NAMES = (
    "load_source",
    "load_document",
    "import_source",
    "open_source",
    "load_file",
    "import_svg",
)
ENCODER_NAMES = (
    "encode_image",
    "encode_document",
    "encode_scene",
    "encode_transport",
    "encode_frames",
)
OWNER_NAMES = (
    "codec",
    "transport_codec",
    "decoder",
    "renderer",
    "reconstructor",
    "Codec",
    "TransportCodec",
    "MCoreIMGCodec",
    "Decoder",
    "Renderer",
    "Reconstructor",
)
COMMAND_ATTRS = (
    "commands",
    "draw_commands",
    "render_commands",
    "display_list",
    "operations",
    "ops",
)
DOCUMENT_ATTRS = (
    "document",
    "scene",
    "asset",
    "drawing",
    "model",
)
CHILD_ATTRS = (
    "children",
    "items",
    "nodes",
    "layers",
    "members",
)


class ReconstructorError(RuntimeError):
    pass


@dataclass
class CallableRef:
    owner: Any
    name: str
    func: Callable[..., Any]

    @property
    def label(self) -> str:
        owner_name = getattr(self.owner, "__name__", type(self.owner).__name__)
        return f"{owner_name}.{self.name}"


@dataclass
class CoreInfo:
    module: ModuleType
    path: Path
    protocol: int
    source_version: int
    build: str
    feature_signature: str
    constructor_version: tuple[int, int]
    decoder: Optional[CallableRef]
    renderer: Optional[CallableRef]
    high_level_renderer: Optional[CallableRef]
    source_loader: Optional[CallableRef]
    encoder: Optional[CallableRef]


@dataclass
class DecodedAsset:
    raw: Any
    commands: list[Any]
    document: Any = None
    metadata: Any = None
    frames: Optional[list[str]] = None
    source_description: str = ""


def decode_base62(text: str) -> int:
    value = 0
    if not text:
        raise ReconstructorError("Cannot decode an empty Base62 value.")
    for char in text:
        if char not in BASE62:
            raise ReconstructorError(f"Invalid Base62 character: {char!r}")
        value = value * 62 + BASE62.index(char)
    return value


def frame_protocol_version(frame: str) -> int:
    frame = frame.strip()
    if len(frame) < 4 or not frame.startswith(FRAME_MAGIC):
        raise ReconstructorError("Cannot determine protocol from malformed MCI frame.")
    return decode_base62(frame[3])


def _unique_paths(paths: Iterable[Path]) -> list[Path]:
    seen: set[Path] = set()
    result: list[Path] = []
    for path in paths:
        try:
            normalized = path.expanduser().resolve()
        except OSError:
            normalized = path.expanduser().absolute()
        if normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _candidate_core_paths(explicit: Optional[str]) -> list[Path]:
    # --core is an explicit compatibility override and must not silently lose
    # to another versioned Constructor found in the same directory.
    if explicit:
        return _unique_paths((Path(explicit),))

    candidates: list[Path] = []
    script_path = Path(__file__).resolve()
    directories = _unique_paths((script_path.parent, Path.cwd()))
    for directory in directories:
        for filename in PREFERRED_CORE_FILENAMES:
            candidates.append(directory / filename)
        for pattern in CORE_GLOB_PATTERNS:
            candidates.extend(sorted(directory.glob(pattern)))

    return [
        path
        for path in _unique_paths(candidates)
        if path != script_path and "reconstructor" not in path.name.lower()
    ]


def _import_core(path: Path) -> ModuleType:
    module_name = f"_mcoreimg_constructor_{abs(hash(path.resolve()))}"
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


def _constructor_version(module: ModuleType, path: Path) -> tuple[int, int]:
    explicit_names = (
        "CONSTRUCTOR_VERSION",
        "APP_VERSION",
        "VERSION",
        "MCOREIMG_VERSION",
    )
    candidates: list[str] = [path.name, str(getattr(module, "CONSTRUCTOR_BUILD", ""))]
    for name in explicit_names:
        value = getattr(module, name, None)
        if value is not None:
            candidates.append(str(value))

    found: list[tuple[int, int]] = []
    for text in candidates:
        for major, minor in re.findall(r"(?i)(?:^|[^A-Za-z0-9])v(\d+)(?:[._-](\d+))?", text):
            found.append((int(major), int(minor or 0)))
        # Explicit version constants sometimes contain just "4.3".
        if text in candidates[2:]:
            match = re.fullmatch(r"\s*(\d+)(?:[._-](\d+))?\s*", text)
            if match:
                found.append((int(match.group(1)), int(match.group(2) or 0)))

    protocol = getattr(module, "PROTOCOL_VERSION", 0)
    fallback = (int(protocol), 0) if isinstance(protocol, int) else (0, 0)
    return max(found, default=fallback)


def _can_construct_without_arguments(cls: type[Any]) -> bool:
    try:
        signature = inspect.signature(cls)
    except (TypeError, ValueError):
        return False
    for parameter in signature.parameters.values():
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            continue
        if parameter.default is parameter.empty:
            return False
    return True


def _owners(module: ModuleType) -> list[Any]:
    owners: list[Any] = [module]
    seen: set[int] = {id(module)}
    for name in OWNER_NAMES:
        value = getattr(module, name, None)
        if value is None:
            continue
        candidate = value
        if inspect.isclass(value) and _can_construct_without_arguments(value):
            try:
                candidate = value()
            except Exception:
                candidate = value
        if id(candidate) not in seen:
            seen.add(id(candidate))
            owners.append(candidate)
    return owners


def _find_callable(module: ModuleType, names: Sequence[str]) -> Optional[CallableRef]:
    for owner in _owners(module):
        for name in names:
            value = getattr(owner, name, None)
            if callable(value):
                return CallableRef(owner, name, value)
    return None


def _build_core_info(module: ModuleType, path: Path) -> CoreInfo:
    protocol = getattr(module, "PROTOCOL_VERSION", None)
    if not isinstance(protocol, int) or not (0 <= protocol < len(BASE62)):
        raise ReconstructorError("missing or invalid integer PROTOCOL_VERSION")
    return CoreInfo(
        module=module,
        path=path.resolve(),
        protocol=protocol,
        source_version=int(getattr(module, "SOURCE_VERSION", 1)),
        build=str(getattr(module, "CONSTRUCTOR_BUILD", "unknown")),
        feature_signature=str(getattr(module, "FEATURE_SIGNATURE", "")),
        constructor_version=_constructor_version(module, path),
        decoder=_find_callable(module, DECODER_NAMES),
        renderer=_find_callable(module, RENDERER_NAMES),
        high_level_renderer=_find_callable(module, HIGH_LEVEL_RENDER_NAMES),
        source_loader=_find_callable(module, SOURCE_LOADER_NAMES),
        encoder=_find_callable(module, ENCODER_NAMES),
    )


CURRENT_V5_FEATURES = frozenset({
    "PROTO5", "LOCALSPACE", "HYBRID", "PRIMITIVES", "GROUPCOPY", "ALPHA", "10MSG",
})


def _feature_tokens(info: CoreInfo) -> set[str]:
    return {token.strip().upper() for token in info.feature_signature.split("|") if token.strip()}


def _current_feature_score(info: CoreInfo) -> int:
    tokens = _feature_tokens(info)
    return len(CURRENT_V5_FEATURES.intersection(tokens))


def _core_rank(info: CoreInfo, order: int) -> tuple[int, int, int, int, int]:
    major, minor = info.constructor_version
    canonical_bonus = int(info.path.name in PREFERRED_CORE_FILENAMES)
    try:
        modified_ns = info.path.stat().st_mtime_ns
    except OSError:
        modified_ns = 0
    return (
        info.protocol,
        _current_feature_score(info),
        major * 1000 + minor,
        canonical_bonus,
        modified_ns - order,
    )


def load_constructor_core(
    explicit: Optional[str] = None,
    required_protocol: Optional[int] = None,
) -> CoreInfo:
    failures: list[str] = []
    accepted: list[tuple[tuple[int, int, int, int, int], CoreInfo]] = []

    for order, path in enumerate(_candidate_core_paths(explicit)):
        if not path.is_file():
            continue
        try:
            module = _import_core(path)
            info = _build_core_info(module, path)
        except Exception as exc:
            failures.append(f"{path}: import/inspection failed: {exc}")
            continue

        if required_protocol is not None and info.protocol != required_protocol:
            failures.append(
                f"{path}: protocol {info.protocol}; input requires protocol {required_protocol}"
            )
            continue
        if info.decoder is None and info.high_level_renderer is None:
            failures.append(f"{path}: no supported transport decoder API")
            continue
        if info.renderer is None and info.high_level_renderer is None:
            failures.append(f"{path}: no supported Pillow renderer API")
            continue

        # Protocol 5's current transport contract is identified by this feature
        # signature. Older protocol-5 experiments remain loadable, but a fully
        # matching v5.1 build ranks above them automatically.
        accepted.append((_core_rank(info, order), info))

    if accepted:
        accepted.sort(key=lambda item: item[0], reverse=True)
        return accepted[0][1]

    searched = "\n".join(f"  - {path}" for path in _candidate_core_paths(explicit))
    rejected = "\n".join(f"  - {failure}" for failure in failures)
    target = f"protocol {required_protocol}" if required_protocol is not None else "the newest compatible protocol"
    message = (
        f"A compatible MCoreIMG Constructor core for {target} was not found.\n\n"
        "Place this reconstructor beside the v4.3 constructor, or pass:\n"
        "  --core /path/to/MCoreIMG-Constructor.py\n\n"
        f"Searched:\n{searched}"
    )
    if rejected:
        message += f"\n\nRejected candidates:\n{rejected}"
    raise ReconstructorError(message)


def _signature_accepts(func: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]) -> Optional[bool]:
    try:
        inspect.signature(func).bind(*args, **kwargs)
    except (TypeError, ValueError):
        return None
    return True


def _call_variants(
    ref: CallableRef,
    variants: Sequence[tuple[tuple[Any, ...], dict[str, Any]]],
    purpose: str,
) -> Any:
    mismatch_errors: list[str] = []
    for args, kwargs in variants:
        accepts = _signature_accepts(ref.func, args, kwargs)
        if accepts is None:
            # Signature unavailable: try the call, but only treat a direct
            # TypeError as an argument mismatch.
            try:
                return ref.func(*args, **kwargs)
            except TypeError as exc:
                mismatch_errors.append(str(exc))
                continue
        try:
            return ref.func(*args, **kwargs)
        except TypeError:
            # Binding succeeded, so this TypeError came from inside the codec.
            raise
    detail = f" Last mismatch: {mismatch_errors[-1]}" if mismatch_errors else ""
    raise ReconstructorError(f"Could not invoke {ref.label} for {purpose}.{detail}")


def _is_image(value: Any) -> bool:
    return hasattr(value, "save") and hasattr(value, "size") and hasattr(value, "mode")


def _extract_image(value: Any) -> Any:
    if _is_image(value):
        return value
    if isinstance(value, Mapping):
        for key in ("image", "pillow", "png", "rendered"):
            candidate = value.get(key)
            if _is_image(candidate):
                return candidate
    if isinstance(value, (tuple, list)):
        for item in value:
            image = _extract_image(item)
            if image is not None:
                return image
    for attr in ("image", "pillow_image", "rendered_image", "png"):
        candidate = getattr(value, attr, None)
        if _is_image(candidate):
            return candidate
    return None


def _as_sequence(value: Any) -> Optional[list[Any]]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return None


def _commands_from_object(value: Any, seen: Optional[set[int]] = None) -> list[Any]:
    if value is None:
        return []
    if seen is None:
        seen = set()
    identity = id(value)
    if identity in seen:
        return []
    seen.add(identity)

    if isinstance(value, Mapping):
        for key in COMMAND_ATTRS:
            sequence = _as_sequence(value.get(key))
            if sequence is not None:
                return sequence
        for key in DOCUMENT_ATTRS:
            if key in value:
                commands = _commands_from_object(value[key], seen)
                if commands:
                    return commands
        return []

    for method_name in (
        "transport_commands",
        "compiled_commands",
        "resolved_commands",
        "transformed_commands",
        "render_commands",
        "visible_commands",
    ):
        method = getattr(value, method_name, None)
        if callable(method):
            try:
                sequence = _as_sequence(method())
            except TypeError:
                continue
            if sequence is not None:
                return sequence

    for attr in COMMAND_ATTRS:
        sequence = _as_sequence(getattr(value, attr, None))
        if sequence is not None:
            return sequence
    for attr in DOCUMENT_ATTRS:
        child = getattr(value, attr, None)
        if child is not None:
            commands = _commands_from_object(child, seen)
            if commands:
                return commands

    sequence = _as_sequence(value)
    return sequence or []


def _document_from_object(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, Mapping):
        for key in DOCUMENT_ATTRS:
            if value.get(key) is not None:
                return value[key]
        if any(key in value for key in COMMAND_ATTRS):
            return value
    for attr in DOCUMENT_ATTRS:
        candidate = getattr(value, attr, None)
        if candidate is not None:
            return candidate
    if any(hasattr(value, attr) for attr in COMMAND_ATTRS):
        return value
    return None


def _metadata_from_object(value: Any) -> Any:
    if isinstance(value, Mapping):
        for key in ("metadata", "meta", "stats", "header", "palette"):
            if key in value:
                return value[key]
    for attr in ("metadata", "meta", "stats", "header", "palette"):
        candidate = getattr(value, attr, None)
        if candidate is not None:
            return candidate
    return None


def _normalize_asset(
    raw: Any,
    *,
    frames: Optional[Sequence[str]] = None,
    source_description: str = "",
) -> DecodedAsset:
    # Tuples commonly mean (commands, palette/stats/metadata).  Do not treat
    # the tuple itself as two drawable commands.
    if isinstance(raw, tuple):
        document = None
        commands: list[Any] = []
        for item in raw:
            if document is None:
                document = _document_from_object(item)
            candidate_commands = _commands_from_object(item)
            if candidate_commands:
                commands = candidate_commands
                break
    else:
        document = _document_from_object(raw)
        commands = _commands_from_object(raw)

    return DecodedAsset(
        raw=raw,
        commands=list(commands),
        document=document,
        metadata=_metadata_from_object(raw),
        frames=list(frames) if frames is not None else None,
        source_description=source_description,
    )


def decode_with_core(core: CoreInfo, frames: Sequence[str]) -> DecodedAsset:
    if core.decoder is None:
        # A high-level renderer may decode directly; preserve frames as the raw
        # object so rendering can still proceed.
        return DecodedAsset(
            raw=list(frames),
            commands=[],
            frames=list(frames),
            source_description=f"{len(frames)} transport frame(s)",
        )

    frame_list = list(frames)
    frame_text = "\n".join(frame_list)
    raw = _call_variants(
        core.decoder,
        (
            ((frame_list,), {}),
            ((tuple(frame_list),), {}),
            ((frame_text,), {}),
            ((), {"frames": frame_list}),
            ((), {"transport_frames": frame_list}),
            ((), {"messages": frame_list}),
            ((), {"text": frame_text}),
        ),
        "transport decoding",
    )
    return _normalize_asset(
        raw,
        frames=frame_list,
        source_description=f"{len(frame_list)} transport frame(s)",
    )


def _render_variants(core: CoreInfo, asset: DecodedAsset) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
    variants: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    seen: set[int] = set()

    def add_positional(value: Any) -> None:
        if value is None or id(value) in seen:
            return
        seen.add(id(value))
        variants.append(((value,), {}))

    renderer_name = core.renderer.name.lower() if core.renderer else ""
    parameter_names: list[str] = []
    if core.renderer is not None:
        try:
            parameter_names = [name.lower() for name in inspect.signature(core.renderer.func).parameters]
        except (TypeError, ValueError):
            pass
    hint = " ".join((renderer_name, *parameter_names))

    # Pick the semantically strongest first argument.  This avoids handing a
    # legacy render_commands function a (commands, metadata) tuple, while still
    # preserving a complete scene/result for scene-aware renderers.
    if "scene" in hint or "document" in hint:
        add_positional(asset.document)
        add_positional(asset.raw)
        if asset.commands:
            add_positional(asset.commands)
    elif "command" in hint or isinstance(asset.raw, tuple):
        if asset.commands:
            add_positional(asset.commands)
        add_positional(asset.document)
        add_positional(asset.raw)
    else:
        add_positional(asset.raw)
        add_positional(asset.document)
        if asset.commands:
            add_positional(asset.commands)

    if asset.document is not None:
        variants.append(((), {"scene": asset.document}))
        variants.append(((), {"document": asset.document}))
    if asset.raw is not None:
        variants.extend(
            (
                ((), {"decoded": asset.raw}),
                ((), {"result": asset.raw}),
                ((), {"scene": asset.raw}),
            )
        )
    if asset.commands:
        variants.append(((), {"commands": asset.commands}))
    return variants


def render_with_core(core: CoreInfo, asset: DecodedAsset) -> Any:
    if core.high_level_renderer is not None and asset.frames:
        frames = list(asset.frames)
        text = "\n".join(frames)
        raw = _call_variants(
            core.high_level_renderer,
            (
                ((frames,), {}),
                ((text,), {}),
                ((), {"frames": frames}),
                ((), {"messages": frames}),
                ((), {"text": text}),
            ),
            "direct frame reconstruction",
        )
        image = _extract_image(raw)
        if image is not None:
            return image

    if core.renderer is None:
        raise ReconstructorError("The selected Constructor has no supported Pillow renderer API.")

    raw = _call_variants(core.renderer, _render_variants(core, asset), "image rendering")
    image = _extract_image(raw)
    if image is None:
        raise ReconstructorError(
            f"{core.renderer.label} returned {type(raw).__name__}, not a Pillow-compatible image."
        )
    return image


def choose_input_file() -> Optional[Path]:
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


def _frame_length_at(text: str, start: int) -> Optional[int]:
    if text[start:start + 3] != FRAME_MAGIC:
        return None
    if len(text) - start < DEFAULT_FRAME_HEADER_LEN:
        return None
    header = text[start:start + DEFAULT_FRAME_HEADER_LEN]
    try:
        payload_length = decode_base62(header[9:11])
    except ReconstructorError:
        return None
    total = DEFAULT_FRAME_HEADER_LEN + payload_length
    if total < DEFAULT_FRAME_HEADER_LEN or total > DEFAULT_MESSAGE_LEN:
        return None
    return total


def extract_frames(text: str) -> list[str]:
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
        protocol_int = int(protocol)
    except (TypeError, ValueError) as exc:
        raise ReconstructorError(f"Invalid source protocol_version: {protocol!r}") from exc
    if not (0 <= protocol_int < len(BASE62)):
        raise ReconstructorError(f"Unsupported source protocol version: {protocol_int}")
    return protocol_int


def prepare_input(path: Path) -> tuple[Optional[list[str]], Optional[int], str]:
    if is_editable_json(path):
        return None, inspect_source_protocol(path), "editable source"
    if path.suffix.lower() in {".svg", ".svgz"}:
        return None, None, "SVG source"
    frames = read_transport_frames(path)
    return frames, frame_protocol_version(frames[0]), "transport"


def load_source_with_core(core: CoreInfo, path: Path) -> DecodedAsset:
    if core.source_loader is None:
        raise ReconstructorError("The selected Constructor cannot load SVG/source files.")
    raw = _call_variants(
        core.source_loader,
        (
            ((path,), {}),
            ((str(path),), {}),
            ((), {"path": path}),
            ((), {"filename": str(path)}),
            ((), {"source_path": path}),
        ),
        "source loading",
    )
    return _normalize_asset(raw, source_description="editable/SVG/hybrid source")


def load_asset(
    core: CoreInfo,
    input_path: Path,
    prepared_frames: Optional[Sequence[str]],
) -> DecodedAsset:
    if is_editable_json(input_path) or input_path.suffix.lower() in {".svg", ".svgz"}:
        return load_source_with_core(core, input_path)

    frames = list(prepared_frames) if prepared_frames is not None else read_transport_frames(input_path)
    max_messages = int(getattr(core.module, "MAX_MESSAGES", 10))
    if len(frames) > max_messages:
        raise ReconstructorError(
            f"Found {len(frames)} frames, but protocol {core.protocol} allows at most {max_messages}."
        )
    return decode_with_core(core, frames)


def _jsonify(value: Any, seen: Optional[set[int]] = None) -> Any:
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
    value = _jsonify(command)
    if isinstance(value, dict):
        return value
    return {"value": value}


def _asset_json_payload(core: CoreInfo, asset: DecodedAsset, input_path: Path) -> dict[str, Any]:
    for candidate in (asset.document, asset.raw):
        if candidate is None:
            continue
        method = getattr(candidate, "to_json", None)
        if callable(method):
            try:
                value = method()
            except TypeError:
                continue
            if isinstance(value, dict):
                payload = _jsonify(value)
                if isinstance(payload, dict):
                    payload.setdefault("protocol_version", core.protocol)
                    payload.setdefault("version", core.source_version)
                    return payload
        if isinstance(candidate, Mapping) and any(key in candidate for key in COMMAND_ATTRS):
            payload = _jsonify(candidate)
            if isinstance(payload, dict):
                payload.setdefault("protocol_version", core.protocol)
                payload.setdefault("version", core.source_version)
                return payload

    return {
        "format": getattr(core.module, "SOURCE_FORMAT", "MCoreIMG-source"),
        "version": core.source_version,
        "protocol_version": core.protocol,
        "canvas": {
            "width": int(getattr(core.module, "CANVAS_W", 720)),
            "height": int(getattr(core.module, "CANVAS_H", 480)),
        },
        "source_name": input_path.stem,
        "transform": {"scale": 1.0, "offset_x": 0.0, "offset_y": 0.0},
        "commands": [command_to_json(command) for command in asset.commands],
        "warnings": [
            "Reconstructed from transport; non-transmitted SVG authoring metadata is unavailable.",
            "Protocol-local group definitions and repeat records are expanded during decoding; editable grouping is preserved only when the Constructor decoder exposes it."
        ],
    }


def dump_source_json(core: CoreInfo, asset: DecodedAsset, input_path: Path, output_path: Path) -> Path:
    payload = _asset_json_payload(core, asset, input_path)
    output_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return output_path


def default_output_path(input_path: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = input_path.name
    stem = name[:-9] if name.lower().endswith(".mci.json") else input_path.stem
    return input_path.with_name(f"{stem}-reconstructed-{timestamp}.png")


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


def _walk_commands(value: Any, seen: Optional[set[int]] = None, depth: int = 0) -> Iterator[tuple[int, Any]]:
    if value is None:
        return
    if seen is None:
        seen = set()
    identity = id(value)
    if identity in seen:
        return
    seen.add(identity)

    sequence = _as_sequence(value)
    if sequence is not None:
        for item in sequence:
            yield from _walk_commands(item, seen, depth)
        return

    children: Optional[list[Any]] = None
    if isinstance(value, Mapping):
        for key in CHILD_ATTRS:
            children = _as_sequence(value.get(key))
            if children is not None:
                break
    else:
        for attr in CHILD_ATTRS:
            children = _as_sequence(getattr(value, attr, None))
            if children is not None:
                break

    has_command_shape = isinstance(value, Mapping) and any(
        key in value for key in ("opcode", "kind", "type", "shape", "geom", "fields")
    )
    has_command_shape = has_command_shape or any(
        hasattr(value, attr) for attr in ("opcode", "kind", "command_kind", "geom", "fields")
    )
    if has_command_shape or children is None:
        yield depth, value
    if children is not None:
        for child in children:
            yield from _walk_commands(child, seen, depth + 1)


def _value_attr(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _command_name(core: CoreInfo, command: Any) -> str:
    opcode = _value_attr(command, "opcode")
    for table_name in ("OP_NAMES", "PRIMITIVE_NAMES", "COMMAND_NAMES", "SHAPE_NAMES"):
        table = getattr(core.module, table_name, None)
        if isinstance(table, Mapping) and opcode in table:
            return str(table[opcode])
    for attr in ("type", "kind", "command_kind", "shape", "name"):
        value = _value_attr(command, attr)
        if value not in (None, ""):
            return str(value)
    return f"Opcode {opcode}"


def list_commands(core: CoreInfo, asset: DecodedAsset) -> None:
    roots: Any = asset.commands if asset.commands else (asset.document or asset.raw)
    count = 0
    for count, (depth, command) in enumerate(_walk_commands(roots), start=1):
        opcode = _value_attr(command, "opcode")
        detail = None
        for attr in ("geom", "fields", "params", "payload", "reference", "translation"):
            value = _value_attr(command, attr)
            if value not in (None, {}):
                detail = value
                break
        if detail is None:
            detail = command_to_json(command)
        print(f"{count - 1:04d}  {'  ' * depth}{_command_name(core, command):<22} op={opcode!s:<3} data={detail}")
    if count == 0:
        print("Decoded scene contains no directly enumerable command list; rendering will use the preserved scene object.")


def _extract_frames_from_encoded(value: Any) -> Optional[list[str]]:
    if isinstance(value, str):
        try:
            return extract_frames(value)
        except ReconstructorError:
            return None
    if isinstance(value, Mapping):
        for key in ("frames", "messages", "transport_frames"):
            sequence = _as_sequence(value.get(key))
            if sequence is not None and all(isinstance(item, str) for item in sequence):
                return list(sequence)
    for attr in ("frames", "messages", "transport_frames"):
        sequence = _as_sequence(getattr(value, attr, None))
        if sequence is not None and all(isinstance(item, str) for item in sequence):
            return list(sequence)
    sequence = _as_sequence(value)
    if sequence is not None and sequence and all(isinstance(item, str) for item in sequence):
        return list(sequence)
    if isinstance(value, tuple):
        for item in value:
            frames = _extract_frames_from_encoded(item)
            if frames:
                return frames
    return None


def run_self_test(core: CoreInfo) -> None:
    core_test = _find_callable(core.module, ("run_self_test", "self_test", "codec_self_test"))
    core_test_ran = False
    if core_test is not None:
        _call_variants(core_test, (((), {}),), "Constructor self-test")
        core_test_ran = True

    sample_ref = _find_callable(core.module, ("sample_document", "sample_scene", "build_sample_document"))
    if sample_ref is None or core.encoder is None:
        if core_test_ran:
            print("MCoreIMG Reconstructor self-test: PASS (Constructor self-test completed)")
            return
        raise ReconstructorError("The selected Constructor lacks round-trip self-test helpers.")

    sample = _call_variants(sample_ref, (((), {}),), "sample document creation")
    commands = _commands_from_object(sample)

    # The current v5.1 encode_image API accepts a command sequence, not the
    # VectorDocument wrapper. Prefer commands when the encoder name/signature
    # says so; retain document/scene variants for older and future cores.
    encoder_hint = core.encoder.name.lower()
    try:
        encoder_hint += " " + " ".join(
            name.lower() for name in inspect.signature(core.encoder.func).parameters
        )
    except (TypeError, ValueError):
        pass
    encoder_variants: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    if commands and ("image" in encoder_hint or "command" in encoder_hint):
        encoder_variants.extend((((commands,), {}), ((), {"commands": commands})))
        encoder_variants.append(((sample,), {}))
    else:
        encoder_variants.append(((sample,), {}))
        if commands:
            encoder_variants.extend((((commands,), {}), ((), {"commands": commands})))
    encoder_variants.extend((((), {"document": sample}), ((), {"scene": sample})))
    encoded = _call_variants(core.encoder, encoder_variants, "round-trip encoding")
    frames = _extract_frames_from_encoded(encoded)
    if not frames:
        raise ReconstructorError("Constructor encoder did not return recognizable transport frames.")

    asset = decode_with_core(core, frames)
    image = render_with_core(core, asset)
    expected = (
        int(getattr(core.module, "CANVAS_W", 720)),
        int(getattr(core.module, "CANVAS_H", 480)),
    )
    try:
        if tuple(image.size) != expected:
            raise ReconstructorError(f"Unexpected rendered image size: {image.size}; expected {expected}.")
    finally:
        close = getattr(image, "close", None)
        if callable(close):
            close()

    print("MCoreIMG Reconstructor self-test: PASS")
    print(
        f"Protocol {core.protocol} | Constructor v{core.constructor_version[0]}.{core.constructor_version[1]} | "
        f"{len(frames)} frame(s) | build {core.build}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reconstruct MCoreIMG transports using the matching current Constructor codec and renderer."
    )
    parser.add_argument("input", nargs="?", help="Input .mci, text, .mci.json, .json, .svg, or .svgz file")
    parser.add_argument("-o", "--output", help="Output PNG path")
    parser.add_argument("--core", help="Path to the matching MCoreIMG Constructor Python file")
    parser.add_argument("--protocol", type=int, help="Force a protocol for source files or self-test")
    parser.add_argument("--dump-json", action="store_true", help="Write a best-effort Constructor-compatible source JSON")
    parser.add_argument("--list-commands", action="store_true", help="Print decoded scene/command contents")
    parser.add_argument("--no-open", action="store_true", help="Do not automatically open the PNG")
    parser.add_argument("--self-test", action="store_true", help="Run Constructor codec/rendering round-trip tests")
    parser.add_argument("--show-core", action="store_true", help="Print the selected Constructor APIs and exit")
    return parser


def _print_core(core: CoreInfo) -> None:
    print(f"Codec core: {core.path}")
    print(f"Constructor build: {core.build}")
    print(f"Constructor version: v{core.constructor_version[0]}.{core.constructor_version[1]}")
    print(f"Protocol: {core.protocol}")
    print(f"Source version: {core.source_version}")
    if core.feature_signature:
        print(f"Features: {core.feature_signature}")
    print(
        "Envelope: "
        f"{getattr(core.module, 'MAX_MESSAGES', '?')} message(s) × "
        f"{getattr(core.module, 'MESSAGE_LEN', '?')} characters"
    )
    print(f"Decoder API: {core.decoder.label if core.decoder else 'direct-render only'}")
    print(f"Renderer API: {core.renderer.label if core.renderer else 'direct frame renderer'}")
    if core.high_level_renderer:
        print(f"Direct frame renderer: {core.high_level_renderer.label}")
    if core.source_loader:
        print(f"Source loader: {core.source_loader.label}")
    if core.protocol == 5:
        missing = sorted(CURRENT_V5_FEATURES.difference(_feature_tokens(core)))
        if missing:
            print(
                "Compatibility warning: protocol-5 core does not advertise current v5.1 features: "
                + ", ".join(missing)
            )


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.protocol is not None and not (0 <= args.protocol < len(BASE62)):
            raise ReconstructorError(f"Protocol must be between 0 and {len(BASE62) - 1}.")

        if args.self_test or args.show_core:
            core = load_constructor_core(args.core, args.protocol)
            _print_core(core)
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
        if args.protocol is not None and detected_protocol is not None and args.protocol != detected_protocol:
            raise ReconstructorError(
                f"Input uses protocol {detected_protocol}, but --protocol requested {args.protocol}."
            )
        required_protocol = args.protocol if args.protocol is not None else detected_protocol
        core = load_constructor_core(args.core, required_protocol)
        asset = load_asset(core, input_path, frames)

        if args.list_commands:
            list_commands(core, asset)

        output_path = Path(args.output).expanduser().resolve() if args.output else default_output_path(input_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        image = render_with_core(core, asset)
        try:
            image.save(output_path)
            image_mode = image.mode
            image_size = image.size
        finally:
            close = getattr(image, "close", None)
            if callable(close):
                close()

        print(f"MCoreIMG Reconstructor {RECONSTRUCTOR_BUILD}")
        _print_core(core)
        print(f"Loaded: {asset.source_description}")
        if asset.commands:
            print(f"Decoded commands: {len(asset.commands)}")
        else:
            print("Decoded representation: preserved Constructor scene/document object")
        print(f"PNG: {image_size[0]}x{image_size[1]} {image_mode}")
        print(f"Image reconstruction complete: {output_path}")

        if args.dump_json:
            json_path = output_path.with_suffix(".mci.json")
            dump_source_json(core, asset, input_path, json_path)
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
