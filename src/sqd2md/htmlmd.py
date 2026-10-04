"""Convert Carnet's contenteditable HTML into Markdown.

The editor (``document.execCommand`` with CSS styling) saves a fragment shaped
like this::

    <div id="text">
      <div class="edit-zone"> ...lines, each usually its own div... </div>
      <div id="todolist…" class="todo-list"></div>
    </div>
    <div id="floating"></div>

To-do item text is not in the HTML. The saved list element is an empty
placeholder; the items live in ``metadata.json``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote

VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}

_BLOCK_TAGS = {
    "address",
    "article",
    "blockquote",
    "div",
    "dl",
    "fieldset",
    "figcaption",
    "figure",
    "footer",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hr",
    "li",
    "main",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "tbody",
    "td",
    "tfoot",
    "th",
    "thead",
    "tr",
    "ul",
}

_SKIP_TAGS = {"script", "style", "head", "noscript"}

_NAMED_FOREGROUND_SKIP = {"black", "#000", "#000000"}
_NAMED_BACKGROUND_SKIP = {"white", "#fff", "#ffffff", "transparent"}


@dataclass
class Element:
    tag: str
    attrs: dict[str, str]
    children: list[Node] = field(default_factory=list)


@dataclass
class Text:
    data: str


Node = Element | Text


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Element("document", {})
        self.stack: list[Element] = [self.root]
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self._skip:
            if tag in {"script", "style"}:
                self._skip += 1
            return
        element = Element(tag, {key.lower(): value or "" for key, value in attrs})
        self.stack[-1].children.append(element)
        if tag in VOID_TAGS:
            return
        self.stack.append(element)
        if tag in {"script", "style"}:
            self._skip = 1

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._skip:
            if tag in {"script", "style"}:
                self._skip -= 1
                if self._skip == 0 and len(self.stack) > 1 and self.stack[-1].tag == tag:
                    self.stack.pop()
            return
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._skip or not data:
            return
        self.stack[-1].children.append(Text(data))

    def handle_comment(self, data: str) -> None:
        return


def parse_html(html: str) -> Element:
    builder = _Builder()
    builder.feed(html)
    builder.close()
    return builder.root


@dataclass
class Piece:
    text: str
    kind: str  # "inline", "line", or "block"


@dataclass
class RenderContext:
    """Asset and to-do state shared while rendering one note."""

    asset_dirname: str
    asset_map: dict[str, str]
    referenced: set[str] = field(default_factory=set)
    todolists: dict[str, dict] = field(default_factory=dict)
    placed_todos: set[str] = field(default_factory=set)


def html_to_markdown(html: str, ctx: RenderContext) -> str:
    html = html.replace("\r\n", "\n").replace("\r", "\n")
    root = parse_html(html)
    chosen = _find_id(root, "text") or _find_tag(root, "body") or root
    renderer = _Renderer(ctx)
    pieces = renderer.render_node(chosen)
    return join_pieces(pieces)


class _Renderer:
    def __init__(self, ctx: RenderContext) -> None:
        self.ctx = ctx

    def render_node(self, node: Node) -> list[Piece]:
        if isinstance(node, Text):
            return [Piece(escape_text(node.data), "inline")]
        if node.tag in _SKIP_TAGS:
            return []
        if node.attrs.get("id") == "floating":
            return []
        if _is_todo(node):
            body = todo_markdown(self._todo_for(node))
            self._mark_placed(node)
            if not body:
                return []
            return [Piece(body, "block")]
        if node.tag in {"ul", "ol"}:
            rendered = self.render_list(node, ordered=node.tag == "ol")
            return [Piece(rendered, "block")] if rendered else []
        if node.tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            level = int(node.tag[1])
            text = self.inline_text(node).replace("\n", " ").strip()
            if not text:
                return []
            return [Piece(f"{'#' * level} {text}", "block")]
        if node.tag == "blockquote":
            return [Piece(self.render_quote(node), "block")]
        if node.tag == "pre":
            return [Piece(self.render_pre(node), "block")]
        if node.tag == "table":
            rendered = self.render_table(node)
            return [Piece(rendered, "block")] if rendered else []
        if node.tag == "hr":
            return [Piece("---", "block")]
        if node.tag in {"div", "p", "section", "article", "header", "footer", "main", "figure", "center", "body", "document", "li"}:
            return self.render_divish(node)
        if node.tag == "br":
            return [Piece("\n", "inline")]
        return [Piece(self.render_inline_element(node), "inline")]

    def render_divish(self, node: Element) -> list[Piece]:
        pieces: list[Piece] = []
        for child in node.children:
            if isinstance(child, Element) and child.attrs.get("id") == "floating":
                continue
            pieces.extend(self.render_node(child))
        if not pieces:
            return [Piece("", "line")]
        if all(piece.kind == "inline" for piece in pieces):
            piece = inline_to_piece("".join(piece.text for piece in pieces))
            return [piece if piece is not None else Piece("", "line")]
        return coalesce(pieces) or [Piece("", "line")]

    def render_list(self, node: Element, ordered: bool) -> str:
        items = [child for child in node.children if isinstance(child, Element) and child.tag == "li"]
        if not items:
            return join_pieces(self.render_divish(node))
        try:
            start = int(node.attrs.get("start") or "1")
        except ValueError:
            start = 1
        lines: list[str] = []
        for offset, item in enumerate(items):
            content = self.render_li_lines(item)
            marker = f"{start + offset}. " if ordered else "- "
            pad = " " * len(marker)
            lines.append(marker + content[0])
            for extra in content[1:]:
                lines.append(pad + extra if extra else "")
        return "\n".join(lines)

    def render_li_lines(self, item: Element) -> list[str]:
        pieces = coalesce(
            [piece for child in item.children for piece in self.render_node(child)]
        )
        lines: list[str] = []
        for piece in pieces:
            if piece.kind == "inline":
                converted = inline_to_piece(piece.text)
                if converted is None:
                    continue
                lines.extend(converted.text.split("\n"))
            elif piece.text == "":
                lines.append("")
            else:
                lines.extend(piece.text.split("\n"))
        while lines and lines[0] == "":
            lines.pop(0)
        while lines and lines[-1] == "":
            lines.pop()
        return lines or [""]

    def render_quote(self, node: Element) -> str:
        inner = join_pieces(self.render_divish(node))
        if not inner:
            return ">"
        quoted = []
        for line in inner.split("\n"):
            quoted.append("> " + line if line else ">")
        return "\n".join(quoted)

    def render_pre(self, node: Element) -> str:
        text = raw_text(node).replace("\xa0", " ").replace("\u200b", "")
        lines = text.split("\n")
        # HTML often wraps the code in a newline, and indents the closing tag.
        if lines and lines[0] == "":
            lines.pop(0)
        if lines and lines[-1].strip() == "":
            lines.pop()
        text = "\n".join(lines)
        fence = "```"
        while fence in text:
            fence += "`"
        language = _code_language(node)
        return f"{fence}{language}\n{text}\n{fence}"

    def render_table(self, node: Element) -> str:
        rows: list[list[str]] = []

        def walk(current: Element) -> None:
            for child in current.children:
                if not isinstance(child, Element):
                    continue
                if child.tag == "tr":
                    cells = []
                    for cell in child.children:
                        if isinstance(cell, Element) and cell.tag in {"td", "th"}:
                            text = self.inline_text(cell).replace("\n", " ").replace("|", "\\|").strip()
                            cells.append(text)
                    if cells:
                        rows.append(cells)
                else:
                    walk(child)

        walk(node)
        if not rows:
            return ""
        width = max(len(row) for row in rows)
        padded = [row + [""] * (width - len(row)) for row in rows]

        def format_row(row: list[str]) -> str:
            return "| " + " | ".join(row) + " |"

        lines = [format_row(padded[0]), "| " + " | ".join("---" for _ in padded[0]) + " |"]
        lines.extend(format_row(row) for row in padded[1:])
        return "\n".join(lines)

    def inline_text(self, node: Element) -> str:
        piece = inline_to_piece("".join(self.inline_fragment(child) for child in node.children))
        return "" if piece is None else piece.text

    def inline_fragment(self, node: Node) -> str:
        if isinstance(node, Text):
            return escape_text(node.data)
        if node.tag in _SKIP_TAGS or node.attrs.get("id") == "floating":
            return ""
        if node.tag == "br":
            return "\n"
        if _is_todo(node) or node.tag in _BLOCK_TAGS or node.tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            return join_pieces(self.render_node(node))
        return self.render_inline_element(node)

    def render_inline_element(self, node: Element) -> str:
        if node.tag == "a":
            return self.render_link(node)
        if node.tag == "img":
            return self.render_image(node)
        if node.tag == "br":
            return "\n"
        if node.tag == "code":
            return code_span(raw_text(node))
        inner = "".join(self.inline_fragment(child) for child in node.children)
        return apply_marks(node, inner)

    def render_link(self, node: Element) -> str:
        href = (node.attrs.get("href") or "").strip()
        text = self.inline_text(node).replace("\n", " ").strip()
        if not href:
            return text
        if not text:
            text = escape_text(href)
        return f"[{text}]({md_destination(href)})"

    def render_image(self, node: Element) -> str:
        src = (node.attrs.get("src") or "").strip()
        alt = escape_text(node.attrs.get("alt") or "").replace("\n", " ").strip()
        if not src:
            return alt
        if src.startswith("data:"):
            return f"![{alt}]({src})"
        key = archive_key(src)
        if key and key in self.ctx.asset_map:
            self.ctx.referenced.add(key)
            relative = self.ctx.asset_map[key]
            target = f"{self.ctx.asset_dirname}/{relative}" if self.ctx.asset_dirname else relative
            label = alt or escape_text(Path(relative).stem)
            return f"![{label}]({md_destination(target)})"
        if src.startswith(("http://", "https://")):
            return f"![{alt}]({md_destination(src)})"
        name = Path(unquote(src.split("?", 1)[0])).name
        if not name:
            return alt
        label = alt or escape_text(name)
        return f"![{label}]({md_destination(name)})"

    def _todo_for(self, node: Element) -> dict | None:
        ident = node.attrs.get("id") or ""
        if ident and ident in self.ctx.todolists:
            return self.ctx.todolists[ident]
        return None

    def _mark_placed(self, node: Element) -> None:
        ident = node.attrs.get("id") or ""
        if ident:
            self.ctx.placed_todos.add(ident)


def coalesce(pieces: list[Piece]) -> list[Piece]:
    output: list[Piece] = []
    buffer: list[str] = []

    def flush() -> None:
        if not buffer:
            return
        piece = inline_to_piece("".join(buffer))
        buffer.clear()
        if piece is not None:
            output.append(piece)

    for piece in pieces:
        if piece.kind == "inline":
            buffer.append(piece.text)
            continue
        flush()
        output.append(piece)
    flush()
    return output


def inline_to_piece(text: str) -> Piece | None:
    text = text.replace("\xa0", " ").replace("\u200b", "").replace("\ufeff", "")
    parts = [part.strip() for part in text.split("\n")]
    while parts and parts[0] == "":
        parts.pop(0)
    while parts and parts[-1] == "":
        parts.pop()
    if not parts:
        return None
    return Piece("\n".join(parts), "line")


def join_pieces(pieces: list[Piece]) -> str:
    lines: list[str] = []
    for piece in coalesce(pieces):
        if piece.kind == "block":
            if lines and lines[-1] != "":
                lines.append("")
            lines.extend(piece.text.split("\n"))
            if lines and lines[-1] != "":
                lines.append("")
            continue
        if piece.kind == "inline":
            converted = inline_to_piece(piece.text)
            if converted is None:
                continue
            piece = converted
        parts = piece.text.split("\n") if piece.text != "" else [""]
        for part in parts:
            lines.append(shield_line(part.rstrip()))
    while lines and lines[0] == "":
        lines.pop(0)
    while lines and lines[-1] == "":
        lines.pop()
    collapsed: list[str] = []
    for line in lines:
        if line == "" and len(collapsed) >= 2 and collapsed[-1] == "" and collapsed[-2] == "":
            continue
        collapsed.append(line)
    return "\n".join(collapsed)


def shield_line(line: str) -> str:
    """Keep a plain text line from being read as a Markdown block marker."""
    if re.match(r"^(?:#{1,6} |[-+*] |\d+\. |>)", line):
        return "\\" + line
    return line


def escape_text(text: str) -> str:
    text = text.replace("\xa0", " ").replace("\u200b", "").replace("\ufeff", "")
    text = re.sub(r"[\\`*_\[\]<>~|]", lambda match: "\\" + match.group(0), text)
    # Nextcloud Text treats "==" as highlight. Escape pairs so a literal
    # double-equals is not highlighted on import.
    return text.replace("==", r"\=\=")


def apply_marks(node: Element, inner: str) -> str:
    """Inline marks using the syntax the Nextcloud Notes editor keeps.

    Notes renders through the Text app, which parses CommonMark with HTML
    disabled. ``__`` is underline there (not bold), ``==`` is highlight, and
    a raw ``<span>`` or ``<u>`` is shown as text. Text color, subscript, and
    superscript have no markup Notes will render, so only the words remain.
    """
    style = _parse_style(node.attrs.get("style", ""))
    decoration = f"{style.get('text-decoration', '')} {style.get('text-decoration-line', '')}".lower()
    tag = node.tag
    if tag in {"b", "strong"} or _weight_is_bold(style.get("font-weight", "")):
        inner = wrap("**", inner)
    if tag in {"i", "em"} or style.get("font-style", "").lower() in {"italic", "oblique"}:
        inner = wrap("*", inner)
    if tag in {"s", "strike", "del"} or "line-through" in decoration:
        inner = wrap("~~", inner)
    if tag == "u" or re.search(r"(?<![-\w])underline(?![-\w])", decoration):
        inner = wrap("__", inner)
    background = style.get("background-color") or style.get("background")
    background = _normalize_color(background, foreground=False)
    if tag == "mark" or (background and inner.strip()):
        inner = wrap("==", inner)
    return inner


def wrap(marker: str, text: str) -> str:
    lead, core, trail = split_ws(text)
    if core == "":
        return text
    return f"{lead}{marker}{core}{marker}{trail}"


def split_ws(text: str) -> tuple[str, str, str]:
    match = re.match(r"^(\s*)(.*?)(\s*)$", text, flags=re.S)
    if match is None:
        return "", text, ""
    return match.group(1), match.group(2), match.group(3)


def code_span(text: str) -> str:
    text = text.replace("\xa0", " ").replace("\n", " ")
    if "`" not in text:
        return f"`{text}`"
    length = 1
    while "`" * length in text:
        length += 1
    fence = "`" * length
    return f"{fence} {text} {fence}"


def raw_text(node: Element) -> str:
    parts: list[str] = []

    def walk(current: Node) -> None:
        if isinstance(current, Text):
            parts.append(current.data)
            return
        if current.tag == "br":
            parts.append("\n")
            return
        for child in current.children:
            walk(child)

    for child in node.children:
        walk(child)
    return "".join(parts)


def todo_markdown(todo: dict | None) -> str:
    if not isinstance(todo, dict):
        return ""
    lines: list[str] = []
    for item in _string_list(todo.get("todo")):
        lines.append(f"- [ ] {escape_text(_one_line(item))}")
    for item in _string_list(todo.get("done")):
        lines.append(f"- [x] {escape_text(_one_line(item))}")
    return "\n".join(lines)


def md_destination(target: str) -> str:
    """Format a link target, quoting it when CommonMark would split on it."""
    if target.startswith("data:"):
        return target
    if re.search(r"[\s<>()\\]", target):
        return "<" + target.replace(">", "%3E") + ">"
    return target


def archive_key(src: str) -> str | None:
    """Map an ``<img src>`` back to a path inside the note, such as ``data/pic.png``."""
    src = src.strip()
    if not src or src.startswith(("data:", "http://", "https://", "mailto:")):
        return None
    if "media=" in src:
        query = src.split("media=", 1)[1].split("&", 1)[0]
        src = unquote(query)
    else:
        src = unquote(src.split("?", 1)[0])
    src = src.replace("\\", "/")
    while src.startswith("./"):
        src = src[2:]
    src = src.lstrip("/")
    if src.startswith("data/"):
        return src
    if src and "/" not in src:
        return "data/" + src
    return None


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _string_list(value: object) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, list):
        return []
    return [item for item in (str(entry) for entry in value) if item.strip()]


def _is_todo(node: Element) -> bool:
    classes = {token.lower() for token in node.attrs.get("class", "").split()}
    ident = node.attrs.get("id", "")
    return "todo-list" in classes or ident.startswith("todolist")


def _find_id(node: Element, ident: str) -> Element | None:
    if node.attrs.get("id") == ident:
        return node
    for child in node.children:
        if isinstance(child, Element):
            found = _find_id(child, ident)
            if found is not None:
                return found
    return None


def _find_tag(node: Element, tag: str) -> Element | None:
    if node.tag == tag:
        return node
    for child in node.children:
        if isinstance(child, Element):
            found = _find_tag(child, tag)
            if found is not None:
                return found
    return None


def _code_language(node: Element) -> str:
    classes = node.attrs.get("class", "")
    for child in node.children:
        if isinstance(child, Element) and child.tag == "code":
            classes += " " + child.attrs.get("class", "")
    match = re.search(r"(?:^|\s)language-([A-Za-z0-9_+-]+)", classes)
    return match.group(1) if match else ""


def _parse_style(style: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for part in style.split(";"):
        if ":" not in part:
            continue
        key, value = part.split(":", 1)
        parsed[key.strip().lower()] = value.strip()
    return parsed


def _weight_is_bold(value: str) -> bool:
    token = value.strip().lower()
    if token in {"bold", "bolder"}:
        return True
    try:
        return float(token) >= 600
    except ValueError:
        return False


def _normalize_color(value: str | None, foreground: bool) -> str | None:
    if not value:
        return None
    token = value.strip().lower()
    if token in {"none", "inherit", "initial", "unset", "currentcolor", "transparent", "windowtext", "canvastext"}:
        return None
    if "url(" in token or "gradient" in token:
        return None
    alpha = re.match(r"rgba\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*,\s*([0-9.]+)\s*\)", token)
    if alpha is not None and float(alpha.group(1)) == 0:
        return None
    rgb = re.match(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", token)
    if rgb is not None:
        channels = tuple(max(0, min(255, int(channel))) for channel in rgb.groups())
        normalized = "#{:02x}{:02x}{:02x}".format(*channels)
    elif re.match(r"^#[0-9a-f]{3}$", token):
        normalized = "#" + "".join(channel * 2 for channel in token[1:])
    elif re.match(r"^#[0-9a-f]{6}$", token):
        normalized = token
    elif re.match(r"^[a-z]+$", token):
        normalized = token
    else:
        return None
    if foreground and normalized in _NAMED_FOREGROUND_SKIP:
        return None
    if not foreground and normalized in _NAMED_BACKGROUND_SKIP:
        return None
    return normalized
