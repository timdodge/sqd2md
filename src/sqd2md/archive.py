"""Read a Carnet note.

A note is either a zip archive (the usual ``.sqd`` file, sometimes ``.spd`` on
Android) or a directory whose name ends in ``.sqd``. Both hold the same files:

* ``index.html`` — rich-text body from the editor
* ``note.md`` — body when the experimental Markdown editor was used
* ``metadata.json`` — dates, keywords, color, rating, to-do lists, reminders
* ``data/`` — images, audio, and other attachments
* ``data/preview_*`` — generated thumbnails, not part of the note
"""

from __future__ import annotations

import json
import os
import zipfile
from dataclasses import dataclass, field
from pathlib import Path


class SqdError(Exception):
    """The path is not a readable Carnet note."""


@dataclass
class Note:
    """One unpacked note, still in Carnet's own structure."""

    html: str | None
    markdown: str | None
    metadata: dict
    files: dict[str, bytes] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


_ROOT_FILES = {"index.html", "note.md", "metadata.json"}


def read_note(path: Path) -> Note:
    """Load a ``.sqd`` / ``.spd`` zip or a ``.sqd`` directory."""
    path = Path(path)
    if path.is_dir():
        files = _read_tree(path)
    elif path.is_file():
        files = _read_zip(path)
    else:
        raise SqdError(f"{path} does not exist")
    return _note_from_files(files)


def _read_zip(path: Path) -> dict[str, bytes]:
    if not zipfile.is_zipfile(path):
        raise SqdError(f"{path} is not a Carnet note (expected a zip archive or a .sqd folder)")
    files: dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                name = _safe_name(info.filename)
                if name is None or name.endswith("/"):
                    continue
                try:
                    files[name] = archive.read(info)
                except RuntimeError as exc:
                    raise SqdError(
                        f"{path} looks password-protected; Carnet notes are not encrypted, "
                        "but this zip could not be read"
                    ) from exc
    except zipfile.BadZipFile as exc:
        raise SqdError(f"{path} is not a valid zip archive") from exc
    return files


def _read_tree(root: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "__MACOSX"]
        for filename in filenames:
            if filename.startswith("."):
                continue
            full = Path(dirpath) / filename
            if not full.is_file():
                continue
            rel = full.relative_to(root).as_posix()
            files[rel] = full.read_bytes()
    return files


def _safe_name(name: str) -> str | None:
    name = name.replace("\\", "/").lstrip("/")
    parts: list[str] = []
    for part in name.split("/"):
        if part in ("", "."):
            continue
        if part == ".." or part.startswith(".") or part == "__MACOSX":
            return None
        parts.append(part)
    if not parts:
        return None
    return "/".join(parts)


def _note_from_files(files: dict[str, bytes]) -> Note:
    prefix = _content_prefix(files)
    warnings: list[str] = []

    def take(relative: str) -> bytes | None:
        return files.get(prefix + relative)

    html_bytes = take("index.html")
    markdown_bytes = take("note.md")
    metadata, meta_warnings = _parse_metadata(take("metadata.json"))
    warnings.extend(meta_warnings)

    kept: dict[str, bytes] = {}
    prefix_len = len(prefix)
    for name, data in files.items():
        if prefix and not name.startswith(prefix):
            continue
        relative = name[prefix_len:]
        if relative in _ROOT_FILES or relative.endswith("/") or not relative:
            continue
        if _is_preview(relative):
            continue
        kept[relative] = data

    html = _decode(html_bytes) if html_bytes is not None else None
    markdown = _decode(markdown_bytes) if markdown_bytes is not None else None
    # The Nextcloud reader prefers index.html whenever it is present.
    if html is not None and html.strip():
        markdown = None
    elif markdown is None:
        html = html if html is not None else None
    else:
        html = None

    return Note(html=html, markdown=markdown, metadata=metadata, files=kept, warnings=warnings)


def _content_prefix(files: dict[str, bytes]) -> str:
    """Return ``''`` when the note sits at the archive root.

    Some exports wrap the note in a single top-level directory. Accept that
    layout when it is unambiguous.
    """
    if any(name in _ROOT_FILES for name in files):
        return ""
    prefixes: set[str] = set()
    for name in files:
        parts = name.split("/")
        if len(parts) == 2 and parts[1] in _ROOT_FILES:
            prefixes.add(parts[0])
    if len(prefixes) == 1:
        return prefixes.pop() + "/"
    return ""


def _is_preview(relative: str) -> bool:
    """Thumbnails live under ``data/`` and are named ``preview_*``."""
    if not relative.startswith("data/"):
        return False
    return Path(relative).name.startswith("preview_")


def _parse_metadata(data: bytes | None) -> tuple[dict, list[str]]:
    if data is None or not data.strip():
        return {}, []
    try:
        parsed = json.loads(_decode(data))
    except json.JSONDecodeError as exc:
        return {}, [f"metadata.json is not valid JSON ({exc.msg})"]
    if not isinstance(parsed, dict):
        return {}, ["metadata.json is not a JSON object"]
    return parsed, []


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")
