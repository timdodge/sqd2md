# sqd2md

Convert [Carnet](https://github.com/CarnetApp/CarnetNextcloud) notes from the `.sqd` format into Markdown.

Carnet stores each note as a zip archive named `.sqd` (on Android the same archive is sometimes `.spd`). The archive holds:

- `index.html` — the rich-text body
- `note.md` — the body when the Markdown editor was used instead
- `metadata.json` — created/modified dates, keywords, note color, rating, to-do lists, reminders, link previews
- `data/` — images, audio, and other attachments (`data/preview_*` files are thumbnails and are skipped)

A note can also be a folder whose name ends in `.sqd`, with those same files inside. That is how Carnet stores folder notes.

```bash
sqd2md note.sqd                  # writes note.md next to the note
sqd2md note.sqd -o note.md
sqd2md ~/Nextcloud/Documents/Carnet -o ./markdown
sqd2md note.sqd --stdout
```

`Carnet/quickdoc`, the app's sync database, is skipped when you point sqd2md at a folder of notes.

Copy the Markdown into your Nextcloud Notes folder (`Notes`, unless you changed it). Notes treats each `.md` file as one note: the filename is the title, and subfolders are categories. Both `.md` and `.txt` are picked up. Running the tool again overwrites the Markdown it wrote.

The files are written for that import, not as a general Carnet archive:

- No YAML front matter. Notes, including the Android app, treats a leading `---` block as note text and can rewrite it. Created and modified dates, keywords, color, and rating are a `## Details` list instead. The file's modified time is the Carnet modification date, which is the date Notes shows. Pass `--frontmatter` if you want the YAML block anyway.
- Markup is limited to what the Notes editor keeps: `**bold**`, `*italic*`, `__underline__` (Notes reads a double underscore as underline, not bold), `~~strikethrough~~`, `==highlight==`, lists, task lists, tables, quotes, and fenced code. Text color, subscript, and superscript become plain words, because Notes does not render raw HTML.
- Characters Notes strips from a title (`* | / \ : " < > ?`, and a leading dot) are removed from the Markdown file name. When `-o` is a directory, the category folders are cleaned the same way. An untitled Carnet note is renamed to its stored title. Two notes that land on the same name get a ` (2)` suffix.
- Attachments are copied to a hidden directory named `.<note>.assets`, so Notes does not list it as a category, and linked from the note. Notes only displays pictures it stored itself, under `.attachments.<file id>/`, and that id does not exist until the note is on the server. An imported picture may show as unresolved until you insert it again; the file is still next to the note.

```bash
pip install -e .
sqd2md --help
```

From a checkout, without installing:

```bash
./sqd2md --help
```

Requires Python 3.9 or newer. The standard library is enough; there are no third-party dependencies.

Rich text is converted approximately. Headings, links, lists, tables, and code blocks are kept. The note filename is the title, matching Carnet, except that a stored title replaces a filename that starts with `untitled`.
