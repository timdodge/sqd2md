"""Turn one loaded Carnet note into Markdown plus attachment bytes."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqd2md.archive import Note
from sqd2md.htmlmd import (
    RenderContext,
    escape_text,
    html_to_markdown,
    md_destination,
    todo_markdown,
)

IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".bmp",
    ".svg",
    ".heic",
    ".heif",
    ".avif",
    ".tif",
    ".tiff",
}

_DAY_ORDER = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


@dataclass
class Conversion:
    markdown: str
    assets: list[tuple[str, bytes]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Carnet's last modification time, when the metadata has one. The caller
    # stamps the written file with it so Nextcloud Notes shows that date.
    modified: datetime | None = None


def convert_note(
    note: Note,
    *,
    title: str,
    asset_dirname: str,
    frontmatter: bool = False,
) -> Conversion:
    """Render ``note`` as Markdown that Nextcloud Notes can import.

    The default note is a ``.md`` file whose name is the title: no YAML front
    matter (the Notes editor and the Android app rewrite a leading ``---``
    block), and inline markup limited to what the Notes editor keeps. Dates,
    keywords, color, and rating go in a ``## Details`` section. Pass
    ``frontmatter=True`` to emit YAML instead of that section.

    ``asset_dirname`` is the directory name used in links, relative to the
    Markdown file (for example ``.Shopping.assets``). Attachment bytes are
    returned separately so the caller can write them.
    """
    warnings = list(note.warnings)
    asset_map = _asset_map(note.files)
    todos = _index_todos(note.metadata)
    placed: set[str] = set()
    referenced: set[str] = set()
    preserve_existing_frontmatter = False

    if note.html is not None and note.html.strip():
        context = RenderContext(
            asset_dirname=asset_dirname,
            asset_map=asset_map,
            todolists=todos,
        )
        body = html_to_markdown(note.html, context)
        placed = context.placed_todos
        referenced = context.referenced
    elif note.markdown is not None:
        body = _normalize_newlines(note.markdown).strip("\n")
        if body.startswith("---"):
            if frontmatter:
                preserve_existing_frontmatter = True
                warnings.append(
                    "note.md already starts with a front matter block; "
                    "Carnet metadata was not added in front of it"
                )
            else:
                stripped, removed = _strip_leading_front_matter(body)
                if removed:
                    body = stripped
                    warnings.append(
                        "removed a leading front matter block so Nextcloud Notes will not rewrite it"
                    )
    else:
        body = ""
        warnings.append("note has no index.html or note.md")

    sections = [body] if body.strip() else []
    unplaced = _unplaced_todos(note.metadata, placed)
    if unplaced:
        sections.append(unplaced)
    gallery = _gallery(asset_map, referenced, asset_dirname)
    if gallery:
        sections.append(gallery)
    links = _links_markdown(note.metadata.get("urls"))
    if links:
        sections.append(links)
    reminders = _reminders_markdown(note.metadata.get("reminders"))
    if reminders:
        sections.append(reminders)
    if not frontmatter:
        details = _details_markdown(note.metadata)
        if details:
            sections.append(details)

    text = "\n\n".join(section.strip("\n") for section in sections if section.strip())
    if frontmatter and not preserve_existing_frontmatter:
        matter = _front_matter(title, note.metadata)
        text = f"{matter}\n\n{text}" if text else matter
    if text and not text.endswith("\n"):
        text += "\n"
    if not text:
        text = "\n"

    assets = [(relative, note.files[key]) for key, relative in asset_map.items()]
    return Conversion(
        markdown=text,
        assets=assets,
        warnings=warnings,
        modified=epoch_to_datetime(note.metadata.get("last_modification_date")),
    )


# Characters Nextcloud Notes strips from a title because they are illegal in a
# file name (NoteUtil::sanitisePath). The filename is the note title.
_NOTES_ILLEGAL = re.compile(r'[*|/\\:"<>?]')


def notes_filename(name: str) -> str:
    """File or folder name Nextcloud Notes will keep as a title or category.

    Drops ``* | / \\ : " < > ?``, leading dots and spaces, and anything past
    100 characters. An empty result becomes ``note``.
    """
    cleaned = _NOTES_ILLEGAL.sub("", name)
    cleaned = re.sub(r"^[\.\s]+", "", cleaned).strip()
    cleaned = re.sub(r"\s", " ", cleaned)
    if not cleaned:
        return "note"
    return cleaned[:100]


def note_title(path: Path, metadata: dict) -> str:
    """Title Carnet shows: the filename, unless an untitled note has a stored title."""
    stem = path.stem
    stored = metadata.get("title")
    if isinstance(stored, str) and stored.strip() and stem.lower().startswith("untitled"):
        return stored.strip()
    return stem


def epoch_to_datetime(value: object) -> datetime | None:
    """Decode a Carnet timestamp.

    Dates are usually milliseconds since the Unix epoch. Values that are
    clearly seconds (a 10-digit timestamp) are accepted too.
    """
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if number >= 1e11:
        number /= 1000.0
    elif number < 1e9:
        return None
    try:
        return datetime.fromtimestamp(number, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def epoch_to_iso(value: object) -> str | None:
    moment = epoch_to_datetime(value)
    if moment is None:
        return None
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def clock_hhmm(value: object) -> str | None:
    """Format a reminder clock time.

    A full timestamp uses its UTC clock, which is how Carnet displays it.
    Editing the clock stores milliseconds since midnight instead.
    """
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    if number >= 1e9:
        moment = epoch_to_datetime(number)
        if moment is None:
            return None
        return f"{moment.hour:02d}:{moment.minute:02d}"
    if number > 24 * 3600:
        number /= 1000.0
    seconds = int(number) % (24 * 3600)
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    return f"{hours:02d}:{minutes:02d}"


def _asset_map(files: dict[str, bytes]) -> dict[str, str]:
    used: set[str] = set()
    mapping: dict[str, str] = {}
    for key in sorted(files):
        relative = key[len("data/") :] if key.startswith("data/") else key
        if not relative or relative.endswith("/"):
            continue
        mapping[key] = _unique_relative(relative, used)
    return mapping


def _unique_relative(relative: str, used: set[str]) -> str:
    if relative not in used:
        used.add(relative)
        return relative
    path = Path(relative)
    index = 2
    while True:
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}").as_posix()
        if candidate not in used:
            used.add(candidate)
            return candidate
        index += 1


def _gallery(
    asset_map: dict[str, str],
    referenced: set[str],
    asset_dirname: str,
) -> str:
    lines: list[str] = []
    images: list[str] = []
    others: list[str] = []
    for key in sorted(asset_map):
        if key in referenced:
            continue
        relative = asset_map[key]
        if Path(relative).suffix.lower() in IMAGE_EXTENSIONS:
            images.append(relative)
        else:
            others.append(relative)
    for relative in images:
        alt = escape_text(Path(relative).stem)
        lines.append(f"![{alt}]({_asset_target(asset_dirname, relative)})")
    for relative in others:
        label = escape_text(Path(relative).name)
        lines.append(f"[{label}]({_asset_target(asset_dirname, relative)})")
    return "\n\n".join(lines)


def _asset_target(asset_dirname: str, relative: str) -> str:
    path = f"{asset_dirname}/{relative}" if asset_dirname else relative
    return md_destination(path)


def _index_todos(metadata: dict) -> dict[str, dict]:
    lists = metadata.get("todolists")
    if not isinstance(lists, list):
        return {}
    indexed: dict[str, dict] = {}
    for item in lists:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            indexed[item["id"]] = item
    return indexed


def _unplaced_todos(metadata: dict, placed: set[str]) -> str:
    lists = metadata.get("todolists")
    if not isinstance(lists, list):
        return ""
    blocks: list[str] = []
    for item in lists:
        if not isinstance(item, dict):
            continue
        ident = item.get("id")
        if isinstance(ident, str) and ident in placed:
            continue
        rendered = todo_markdown(item)
        if rendered:
            blocks.append(rendered)
    return "\n".join(blocks)


def _links_markdown(urls: object) -> str:
    if not isinstance(urls, dict):
        return ""
    lines: list[str] = []
    for url, info in urls.items():
        if not isinstance(url, str) or not url.strip():
            continue
        if not isinstance(info, dict):
            info = {}
        title = str(info.get("title") or "").strip()
        description = str(info.get("description") or "").strip()
        if not title and not description:
            continue
        lines.append(f"- [{escape_text(title or url)}]({md_destination(url)})")
        if description:
            lines.append(f"  {escape_text(description)}")
    if not lines:
        return ""
    return "## Links\n\n" + "\n".join(lines)


def _reminders_markdown(reminders: object) -> str:
    if not isinstance(reminders, list):
        return ""
    lines = [_format_reminder(item) for item in reminders if isinstance(item, dict)]
    lines = [line for line in lines if line]
    if not lines:
        return ""
    return "## Reminders\n\n" + "\n".join(lines)


def _format_reminder(reminder: dict) -> str:
    frequency = str(reminder.get("frequency") or "once")
    clock = clock_hhmm(reminder.get("time")) if reminder.get("time") not in (None, "") else None
    if frequency == "days-of-week":
        days = reminder.get("days") if isinstance(reminder.get("days"), list) else []
        wanted = {str(day).lower() for day in days}
        ordered = [day for day in _DAY_ORDER if day in wanted]
        for day in days:
            if str(day).lower() not in set(ordered):
                ordered.append(str(day))
        label = ", ".join(day.capitalize() for day in ordered) or "weekly"
        return f"- {label} at {clock}" if clock else f"- {label}"
    date = _reminder_date(reminder)
    bits = [frequency.replace("-", " ")]
    if date:
        bits.append(date)
    if clock:
        bits.append(clock)
    return "- " + " ".join(bits)


def _reminder_date(reminder: dict) -> str:
    try:
        year = int(reminder["year"])
        month = int(reminder["month"])
        day = int(reminder["dayOfMonth"])
    except (KeyError, TypeError, ValueError):
        return ""
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return ""
    return f"{year:04d}-{month:02d}-{day:02d}"


_FRONT_MATTER = re.compile(r"\A---[ \t]*\n.*?\n---[ \t]*(\n|\Z)", re.DOTALL)


def _strip_leading_front_matter(body: str) -> tuple[str, bool]:
    """Drop a YAML block at the start of a Markdown note. Returns the rest."""
    match = _FRONT_MATTER.match(body)
    if match is None:
        return body, False
    return body[match.end() :].strip("\n"), True


def _details_markdown(metadata: dict) -> str:
    """Metadata Notes has no field for, as a plain list at the end of the note."""
    lines: list[str] = []
    created = epoch_to_iso(metadata.get("creation_date"))
    if created:
        lines.append(f"- Created: {created}")
    modified = epoch_to_iso(metadata.get("last_modification_date"))
    if modified:
        lines.append(f"- Modified: {modified}")
    custom = epoch_to_iso(metadata.get("custom_date"))
    if custom:
        lines.append(f"- Custom date: {custom}")
    words = _keywords(metadata.get("keywords"))
    if words:
        lines.append("- Keywords: " + ", ".join(escape_text(word) for word in words))
    color = metadata.get("color")
    if isinstance(color, str) and color.strip() and color.strip().lower() != "none":
        lines.append(f"- Color: {escape_text(color.strip())}")
    rating = _rating(metadata.get("rating"))
    if rating is not None:
        lines.append(f"- Rating: {rating}")
    if not lines:
        return ""
    return "## Details\n\n" + "\n".join(lines)


def _front_matter(title: str, metadata: dict) -> str:
    lines = ["---", f"title: {yaml_string(title)}"]
    for key, field_name in (
        ("created", "creation_date"),
        ("modified", "last_modification_date"),
        ("custom_date", "custom_date"),
    ):
        rendered = epoch_to_iso(metadata.get(field_name))
        if rendered:
            lines.append(f"{key}: {rendered}")
    words = _keywords(metadata.get("keywords"))
    if words:
        lines.append("keywords:")
        for word in words:
            lines.append(f"  - {yaml_string(word)}")
    color = metadata.get("color")
    if isinstance(color, str) and color.strip() and color.strip().lower() != "none":
        lines.append(f"color: {yaml_string(color.strip())}")
    rating = _rating(metadata.get("rating"))
    if rating is not None:
        lines.append(f"rating: {rating}")
    lines.append("---")
    return "\n".join(lines)


def _keywords(value: object) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    words: list[str] = []
    for item in value:
        if item is None:
            continue
        text = str(item).strip()
        if text:
            words.append(text)
    return words


def _rating(value: object) -> int | float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not 0 < number <= 5:
        return None
    if number.is_integer():
        return int(number)
    return number


def yaml_string(value: str) -> str:
    """Quote a string so it is a single safe YAML scalar."""
    if value == "":
        return '""'
    special = set(":#{}[]&*!|>%@`'\",\\\n\r\t")
    needs_quotes = (
        any(character in special for character in value)
        or value[0] in "-?&*!|>%@`'\""
        or value != value.strip()
        or value.lower() in {"null", "true", "false", "yes", "no", "~", "n", "y"}
        or re.match(r"^-?\d+(\.\d+)?$", value) is not None
    )
    if not needs_quotes:
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return f'"{escaped}"'


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")
