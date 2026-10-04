"""Tests for unpacking Carnet .sqd notes."""

from __future__ import annotations

import io
import json
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

from sqd2md.archive import SqdError, read_note
from sqd2md.cli import main
from sqd2md.convert import clock_hhmm, convert_note, epoch_to_iso, note_title, notes_filename, yaml_string
from sqd2md.htmlmd import html_to_markdown, RenderContext

RICH_HTML = """\
<div id="text" style="height:100%;">
    <!-- be aware that THIS will be modified in java -->
    <!-- soft won't save note if contains donotsave345oL -->
    <div class="edit-zone" contenteditable="true">
        <div>Hello <b>world</b></div>
        <div><span style="font-style: italic;">note</span> with <span style="color: rgb(213, 0, 0); background-color: rgb(255, 235, 59);">color</span></div>
        <div><span style="font-weight: 700;">strong</span> and <u>under</u> and <s>gone</s></div>
        <div><br></div>
        <div>Line <a href="https://example.com/a b">link</a></div>
        <div>- not a list</div>
        <ul>
            <li>one</li>
            <li>two
                <ol>
                    <li>nested</li>
                </ol>
            </li>
        </ul>
        <img src="data/pic.png" alt="pic">
    </div>
    <div id="todolistabc" class="todo-list" contenteditable="false"></div>
</div>
<div id="floating"></div>
"""


def _metadata() -> dict:
    return {
        "creation_date": 1580000000000,
        "last_modification_date": 1580001000000,
        "keywords": ["work", "home"],
        "rating": 3,
        "color": "blue",
        "todolists": [
            {"id": "todolistabc", "todo": ["buy milk"], "done": ["walk"]},
            {"id": "todolistother", "todo": ["later"], "done": []},
        ],
        "reminders": [
            {
                "frequency": "once",
                "year": 2020,
                "month": 5,
                "dayOfMonth": 2,
                "time": 1590000000000,
            },
            {
                "frequency": "days-of-week",
                "days": ["wednesday", "monday"],
                "time": 58800000,
            },
        ],
        "urls": {
            "https://example.com": {"title": "Example", "description": "A site"},
            "https://plain.example": {},
        },
    }


def _write_zip(path: Path, files: dict[str, bytes | str]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in files.items():
            if isinstance(data, str):
                data = data.encode("utf-8")
            archive.writestr(name, data)


class HtmlTests(unittest.TestCase):
    def test_old_note_with_breaks(self) -> None:
        html = '<div id="text">Hello<br>World</div><div id="floating"></div>'
        self.assertEqual(html_to_markdown(html, RenderContext("", {})), "Hello\nWorld")

    def test_table_and_code(self) -> None:
        html = """
        <div id="text">
          <pre><code class="language-python">print("hi")
          </code></pre>
          <table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>
        </div>
        """
        rendered = html_to_markdown(html, RenderContext("", {}))
        self.assertIn('```python\nprint("hi")\n```', rendered)
        self.assertIn("| A | B |\n| --- | --- |\n| 1 | 2 |", rendered)

    def test_asterisks_in_text_are_literal(self) -> None:
        html = "<div id=\"text\"><div>cost is 2 * 3</div></div>"
        self.assertEqual(html_to_markdown(html, RenderContext("", {})), r"cost is 2 \* 3")

    def test_marks_match_nextcloud_notes(self) -> None:
        html = (
            '<div id="text"><div><u>under</u> '
            '<span style="background-color:#ffeb3b">mark</span> '
            '<span style="color:#d50000">red</span> '
            "a == b H<sub>2</sub>O</div></div>"
        )
        self.assertEqual(
            html_to_markdown(html, RenderContext("", {})),
            r"__under__ ==mark== red a \=\= b H2O",
        )

    def test_media_query_points_at_extracted_file(self) -> None:
        html = '<div id="text"><img src="getMedia?note=n.sqd&amp;media=data/pic.png" alt="pic"></div>'
        context = RenderContext("N.assets", {"data/pic.png": "pic.png"})
        self.assertEqual(html_to_markdown(html, context), "![pic](N.assets/pic.png)")
        self.assertEqual(context.referenced, {"data/pic.png"})


class ConvertTests(unittest.TestCase):
    def test_rich_zip_round_trip(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Shopping.sqd"
            _write_zip(
                path,
                {
                    "index.html": RICH_HTML,
                    "metadata.json": json.dumps(_metadata()),
                    "data/pic.png": b"\x89PNG",
                    "data/extra.png": b"\x89PNG-extra",
                    "data/preview_pic.jpg": b"thumb",
                    "data/voice.ogg": b"OggS",
                },
            )
            note = read_note(path)
            result = convert_note(note, title=note_title(path, note.metadata), asset_dirname="Shopping.assets")
            created = epoch_to_iso(1580000000000)
            modified = epoch_to_iso(1580001000000)
            once_clock = clock_hhmm(1590000000000)
            expected = f"""\
Hello **world**
*note* with ==color==
**strong** and __under__ and ~~gone~~

Line [link](<https://example.com/a b>)
\\- not a list

- one
- two
  1. nested

![pic](Shopping.assets/pic.png)

- [ ] buy milk
- [x] walk

- [ ] later

![extra](Shopping.assets/extra.png)

[voice.ogg](Shopping.assets/voice.ogg)

## Links

- [Example](https://example.com)
  A site

## Reminders

- once 2020-05-02 {once_clock}
- Monday, Wednesday at 16:20

## Details

- Created: {created}
- Modified: {modified}
- Keywords: work, home
- Color: blue
- Rating: 3
"""
            self.assertEqual(expected, result.markdown)
            self.assertEqual(result.modified, datetime.fromtimestamp(1580001000, timezone.utc))
            yaml = convert_note(
                note,
                title=note_title(path, note.metadata),
                asset_dirname="Shopping.assets",
                frontmatter=True,
            )
            self.assertTrue(yaml.markdown.startswith("---\ntitle: Shopping\n"))
            self.assertNotIn("## Details", yaml.markdown)
            self.assertNotIn("<u>", yaml.markdown)
            self.assertNotIn("<span", yaml.markdown)
            written = dict(result.assets)
            self.assertEqual(written["pic.png"], b"\x89PNG")
            self.assertEqual(written["extra.png"], b"\x89PNG-extra")
            self.assertEqual(written["voice.ogg"], b"OggS")
            self.assertNotIn("preview_pic.jpg", written)
            self.assertFalse(any("preview_" in name for name in written))

    def test_single_wrapper_directory_inside_zip(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Wrapped.sqd"
            _write_zip(path, {"note/index.html": "<div id=\"text\"><div>Inside</div></div>", "note/metadata.json": "{}"})
            note = read_note(path)
            result = convert_note(note, title="Wrapped", asset_dirname="Wrapped.assets", frontmatter=False)
            self.assertEqual("Inside\n", result.markdown)

    def test_folder_note_prefers_html_and_falls_back_to_markdown(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "Plain.sqd"
            (folder / "data").mkdir(parents=True)
            (folder / "note.md").write_text("already *markdown*\n", encoding="utf-8")
            (folder / "metadata.json").write_text(
                json.dumps({"creation_date": 1580000000000, "keywords": ["md"]}),
                encoding="utf-8",
            )
            (folder / "data" / "clip.ogg").write_bytes(b"OggS")
            note = read_note(folder)
            result = convert_note(note, title="Plain", asset_dirname="Plain.assets")
            self.assertIn("already *markdown*", result.markdown)
            self.assertIn("- Keywords: md", result.markdown)
            self.assertNotIn("---", result.markdown)
            self.assertIn("[clip.ogg](Plain.assets/clip.ogg)", result.markdown)

            (folder / "index.html").write_text("<div id=\"text\"><div>From html</div></div>", encoding="utf-8")
            note = read_note(folder)
            result = convert_note(note, title="Plain", asset_dirname="Plain.assets", frontmatter=False)
            created = epoch_to_iso(1580000000000)
            self.assertEqual(
                f"From html\n\n[clip.ogg](Plain.assets/clip.ogg)\n\n## Details\n\n- Created: {created}\n- Keywords: md\n",
                result.markdown,
            )

    def test_markdown_front_matter_is_left_alone(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "Kept.sqd"
            folder.mkdir()
            (folder / "note.md").write_text("---\ntitle: Mine\n---\n\nBody\n", encoding="utf-8")
            (folder / "metadata.json").write_text("{}", encoding="utf-8")
            note = read_note(folder)
            result = convert_note(note, title="Kept", asset_dirname="Kept.assets")
            self.assertEqual(result.markdown, "Body\n")
            self.assertTrue(any("front matter" in warning for warning in result.warnings))
            kept = convert_note(note, title="Kept", asset_dirname="Kept.assets", frontmatter=True)
            self.assertTrue(kept.markdown.startswith("---\ntitle: Mine\n"))
            self.assertTrue(any("front matter" in warning for warning in kept.warnings))

    def test_broken_metadata_still_converts(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Broken.sqd"
            _write_zip(path, {"index.html": "<div id=\"text\">Hi</div>", "metadata.json": "{not json"})
            note = read_note(path)
            self.assertEqual(note.metadata, {})
            self.assertTrue(note.warnings)
            result = convert_note(note, title="Broken", asset_dirname="Broken.assets", frontmatter=False)
            self.assertEqual("Hi\n", result.markdown)

    def test_not_a_zip(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nope.sqd"
            path.write_text("hello", encoding="utf-8")
            with self.assertRaises(SqdError):
                read_note(path)

    def test_epoch_and_clock(self) -> None:
        moment = datetime.fromtimestamp(1580000000, timezone.utc)
        self.assertEqual(epoch_to_iso(1580000000000), moment.strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.assertEqual(epoch_to_iso(1580000000), moment.strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.assertIsNone(epoch_to_iso(-1))
        self.assertEqual(clock_hhmm(58800000), "16:20")
        self.assertEqual(note_title(Path("untitled grocery.sqd"), {"title": "Grocery"}), "Grocery")
        self.assertEqual(note_title(Path("Shopping.sqd"), {"title": "Other"}), "Shopping")
        self.assertEqual(yaml_string("plain"), "plain")
        self.assertEqual(yaml_string("a: b"), '"a: b"')
        self.assertEqual(notes_filename("Q? meeting: notes"), "Q meeting notes")
        self.assertEqual(notes_filename("..."), "note")
        self.assertEqual(len(notes_filename("x" * 150)), 100)


class CliTests(unittest.TestCase):
    def test_directory_walk_skips_quickdoc(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / "Work"
            work.mkdir()
            _write_zip(
                work / "idea.sqd",
                {
                    "index.html": "<div id=\"text\"><div>An idea</div></div>",
                    "metadata.json": json.dumps(
                        {"color": "none", "rating": -1, "last_modification_date": 1580000000000}
                    ),
                },
            )
            secret = root / "quickdoc"
            secret.mkdir()
            _write_zip(secret / "hidden.sqd", {"index.html": "<div id=\"text\">nope</div>", "metadata.json": "{}"})
            folder = root / "List.sqd"
            folder.mkdir()
            (folder / "note.md").write_text("folder note\n", encoding="utf-8")
            (folder / "metadata.json").write_text("{}", encoding="utf-8")
            android = root / "voice.spd"
            _write_zip(android, {"index.html": "<div id=\"text\"><div>spoken</div></div>", "metadata.json": "{}"})

            out = root / "markdown"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main([str(root), "-o", str(out)])
            self.assertEqual(code, 0, stderr.getvalue())
            idea = (out / "Work" / "idea.md").read_text(encoding="utf-8")
            self.assertTrue(idea.startswith("An idea\n"))
            self.assertIn("## Details\n\n- Modified: ", idea)
            self.assertNotIn("---", idea)
            self.assertNotIn("color:", idea)
            self.assertNotIn("rating:", idea)
            self.assertAlmostEqual((out / "Work" / "idea.md").stat().st_mtime, 1580000000, delta=1)
            self.assertIn("folder note", (out / "List.md").read_text(encoding="utf-8"))
            self.assertIn("spoken", (out / "voice.md").read_text(encoding="utf-8"))
            self.assertFalse((out / "quickdoc").exists())
            self.assertIn("3 notes written", stderr.getvalue())

    def test_stdout_and_assets(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            note = root / "Pic.sqd"
            _write_zip(
                note,
                {
                    "index.html": '<div id="text"><div>See</div><img src="data/pic.png" alt="shot"></div>',
                    "metadata.json": "{}",
                    "data/pic.png": b"png",
                },
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main([str(note), "--stdout", "--no-frontmatter"])
            self.assertEqual(code, 0, stderr.getvalue())
            self.assertIn("See\n", stdout.getvalue())
            self.assertIn("![shot](.Pic.assets/pic.png)", stdout.getvalue())
            self.assertEqual((root / ".Pic.assets" / "pic.png").read_bytes(), b"png")
            self.assertFalse((root / "Pic.md").exists())

    def test_titles_are_filenames_notes_can_store(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_zip(
                root / "Q?.sqd",
                {"index.html": "<div id=\"text\"><div>Question</div></div>", "metadata.json": "{}"},
            )
            _write_zip(
                root / "untitled.sqd",
                {
                    "index.html": "<div id=\"text\"><div>Milk</div></div>",
                    "metadata.json": json.dumps({"title": "Grocery"}),
                },
            )
            out = root / "markdown"
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = main([str(root), "-o", str(out), "-q"])
            self.assertEqual(code, 0, stderr.getvalue())
            self.assertEqual((out / "Q.md").read_text(encoding="utf-8"), "Question\n")
            self.assertEqual((out / "Grocery.md").read_text(encoding="utf-8"), "Milk\n")
            self.assertFalse((out / "Q?.md").exists())
            self.assertFalse((out / "untitled.md").exists())

    def test_missing_path(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = main(["/tmp/does-not-exist-sqd2md.sqd"])
        self.assertEqual(code, 1)
        self.assertIn("does not exist", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
