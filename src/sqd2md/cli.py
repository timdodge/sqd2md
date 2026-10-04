"""Command line interface for sqd2md."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from sqd2md import __version__
from sqd2md.archive import Note, SqdError, read_note
from sqd2md.convert import Conversion, convert_note, note_title, notes_filename

NOTE_SUFFIXES = {".sqd", ".spd"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sqd2md",
        description="Unpack Carnet .sqd notes into Markdown.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  sqd2md note.sqd\n"
            "  sqd2md note.sqd -o note.md\n"
            "  sqd2md ~/Nextcloud/Documents/Carnet -o ./markdown\n"
            "  sqd2md note.sqd --stdout\n"
            "\n"
            "A .sqd file is a zip archive. A directory whose name ends in .sqd is\n"
            "the same note stored unpacked (folder notes, including the Markdown\n"
            "editor). Passing a folder converts every note under it. Carnet's\n"
            "quickdoc sync database is skipped.\n"
            "\n"
            "The .md files are meant to be copied into the Nextcloud Notes folder\n"
            "(Notes by default). The filename is the note title and subfolders are\n"
            "categories. Notes does not keep YAML front matter; pass --frontmatter\n"
            "only if you want it anyway."
        ),
    )
    parser.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help="a .sqd/.spd file, a .sqd folder, or a directory of notes",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Markdown file for one note, or a directory for one or more notes",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="print one note to stdout instead of writing the .md file",
    )
    matter = parser.add_mutually_exclusive_group()
    matter.add_argument(
        "--frontmatter",
        action="store_true",
        help=(
            "write YAML front matter instead of a Markdown details section. "
            "Nextcloud Notes does not understand it and may show or rewrite it"
        ),
    )
    matter.add_argument(
        "--no-frontmatter",
        action="store_true",
        help="omit YAML front matter (this is the default)",
    )
    parser.add_argument(
        "--no-assets",
        action="store_true",
        help="do not copy images, audio, or other attachments next to the Markdown",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="only print warnings and errors")
    parser.add_argument("--version", action="version", version=f"sqd2md {__version__}")
    args = parser.parse_args(argv)

    try:
        notes = _collect_notes(args.paths)
    except SqdError as exc:
        print(f"sqd2md: {exc}", file=sys.stderr)
        return 1
    if not notes:
        print("sqd2md: no .sqd notes found", file=sys.stderr)
        return 1
    if args.stdout and len(notes) != 1:
        print("sqd2md: --stdout converts a single note; pass one .sqd file", file=sys.stderr)
        return 1

    output_is_file = _output_is_file(args.output, len(notes))
    if output_is_file is None:
        print(
            "sqd2md: --output must be a directory when converting more than one note",
            file=sys.stderr,
        )
        return 1

    root = _conversion_root(args.paths)
    written = 0
    failed = 0
    used_destinations: set[Path] = set()
    for note_path in notes:
        try:
            try:
                loaded = read_note(note_path)
            except SqdError:
                raise
            except OSError as exc:
                raise SqdError(f"{note_path}: {exc}") from exc
            title = note_title(note_path, loaded.metadata)
            destination = _markdown_destination(
                note_path, root, args.output, output_is_file, title
            )
            destination = _allocate_destination(destination, used_destinations)
            result = _convert_path(
                note_path, destination, loaded, frontmatter=args.frontmatter
            )
        except SqdError as exc:
            print(f"sqd2md: {exc}", file=sys.stderr)
            failed += 1
            continue
        for warning in result.warnings:
            print(f"sqd2md: {note_path}: {warning}", file=sys.stderr)
        asset_dir = destination.parent / f".{destination.stem}.assets"
        if result.assets and not args.no_assets:
            try:
                _write_assets(asset_dir, result)
            except SqdError as exc:
                print(f"sqd2md: {exc}", file=sys.stderr)
                failed += 1
                continue
        if args.stdout:
            sys.stdout.write(result.markdown)
            if result.assets and not args.no_assets:
                print(
                    f"sqd2md: attachments written to {asset_dir} "
                    f"(save stdout as {destination} for the links to resolve)",
                    file=sys.stderr,
                )
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(result.markdown, encoding="utf-8")
            if result.modified is not None:
                stamp = result.modified.timestamp()
                os.utime(destination, (stamp, stamp))
            if not args.quiet:
                attachment_note = f" + {len(result.assets)} attachment(s)" if result.assets else ""
                print(f"{destination}{attachment_note}", file=sys.stderr)
        written += 1

    if not args.quiet and not args.stdout:
        noun = "note" if written == 1 else "notes"
        print(f"sqd2md: {written} {noun} written", file=sys.stderr)
    if failed and not args.quiet:
        print(f"sqd2md: {failed} failed", file=sys.stderr)
    return 1 if failed else 0


def _collect_notes(paths: list[Path]) -> list[Path]:
    found: list[Path] = []
    for path in paths:
        found.extend(_notes_under(path))
    # Preserve order while dropping duplicates (a file passed twice, or a file
    # that is also inside a passed directory).
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in found:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(path)
    return unique


def _notes_under(path: Path) -> list[Path]:
    if not path.exists():
        raise SqdError(f"{path} does not exist")
    if path.is_file() or _is_note_dir(path):
        return [path]
    if not path.is_dir():
        raise SqdError(f"{path} is not a note or a directory")
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        kept: list[str] = []
        for name in sorted(dirnames):
            if name == "quickdoc" or name.startswith("."):
                continue
            candidate = Path(dirpath) / name
            if candidate.suffix.lower() in NOTE_SUFFIXES:
                found.append(candidate)
                continue
            kept.append(name)
        dirnames[:] = kept
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            if Path(name).suffix.lower() in NOTE_SUFFIXES:
                found.append(Path(dirpath) / name)
    return found


def _is_note_dir(path: Path) -> bool:
    return path.is_dir() and path.suffix.lower() in NOTE_SUFFIXES


def _output_is_file(output: Path | None, note_count: int) -> bool | None:
    """Return whether ``--output`` names a file. ``None`` means the flag is invalid."""
    if output is None:
        return False
    if output.exists():
        if output.is_dir():
            return False
        if note_count > 1:
            return None
        return True
    if note_count > 1 and output.suffix.lower() == ".md":
        return None
    if note_count == 1 and output.suffix.lower() == ".md":
        return True
    return False


def _conversion_root(paths: list[Path]) -> Path:
    anchors: list[Path] = []
    for path in paths:
        if path.is_dir() and not _is_note_dir(path):
            anchors.append(path.resolve())
        else:
            anchors.append(path.resolve().parent)
    if len(anchors) == 1:
        return anchors[0]
    return Path(os.path.commonpath([str(anchor) for anchor in anchors]))


def _markdown_destination(
    note: Path,
    root: Path,
    output: Path | None,
    output_is_file: bool,
    title: str,
) -> Path:
    if output_is_file:
        assert output is not None
        return output
    filename = f"{notes_filename(title)}.md"
    if output is None:
        # Beside the source, only the file name changes. Parent folders are
        # the user's own layout, not Notes categories yet.
        return note.with_name(filename)
    try:
        relative = note.resolve().relative_to(root)
    except ValueError:
        relative = Path(note.name)
    return output / _notes_relative(relative, filename)


def _notes_relative(relative: Path, filename: str) -> Path:
    """Path under the output directory, with names Notes accepts as categories."""
    directories = [notes_filename(part) for part in relative.parts[:-1]]
    return Path(*directories, filename)


def _allocate_destination(destination: Path, used: set[Path]) -> Path:
    """Keep two notes from landing on the same file after name cleanup."""
    candidate = destination
    number = 2
    while candidate.resolve() in used:
        suffix = f" ({number})"
        keep = 100 - len(suffix)
        stem = destination.stem[:keep] if keep > 0 else destination.stem
        candidate = destination.with_name(f"{stem}{suffix}{destination.suffix}")
        number += 1
    used.add(candidate.resolve())
    return candidate


def _convert_path(path: Path, destination: Path, note: Note, *, frontmatter: bool) -> Conversion:
    if destination.resolve() == path.resolve():
        raise SqdError(f"refusing to overwrite the note itself: {path}")
    return convert_note(
        note,
        title=note_title(path, note.metadata),
        # A leading dot keeps Notes from listing the folder as a category.
        # Notes only displays images it stored under .attachments.<file id>/,
        # and that id does not exist until the note is uploaded.
        asset_dirname=f".{destination.stem}.assets",
        frontmatter=frontmatter,
    )


def _write_assets(asset_dir: Path, result: Conversion) -> None:
    root = asset_dir.resolve()
    for relative, data in result.assets:
        target = (asset_dir / relative).resolve()
        if os.path.commonpath([str(root), str(target)]) != str(root):
            raise SqdError(f"attachment path escapes the output directory: {relative}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


if __name__ == "__main__":
    raise SystemExit(main())
