"""What one release publishes of its user notes: the `release-notes.json` asset and the start of its page.

The notes come from the same two definitions as the cell image's notes file: `scripts/user_notes.py`
gives each note to the first release that contains it, and `src/exomem/release_notes_format.py` is the
file format. This script adds no second assignment and no second serialiser. Two subcommands:

  file  Writes the notes file of one release: the entry `generate` gives that release alone, or an
        empty list when the release has no notes. A reader then reads a missing file as notes
        missing, never as no notes.
  page  Reads that file and the release page's body, and writes a body that starts with a
        "What's new" section: each note's title, then its sentence. A body that has the section, or
        a release with no notes, comes back unchanged, so a rerun adds no second copy.

`file` needs the full history and the version tags, as `generate` does, and imports what it imports
(markdown-it-py and PyYAML read the note shape). `page` needs only the standard library.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

import user_notes  # the sibling script: a script run by path finds its own directory first

#: The first line of a release page's section. A comment shows nothing on the page, and a rerun finds
#: its own section by it, so no code reads the heading's words.
PAGE_MARKER = "<!-- evermatter-whats-new -->"


def release_document(repo: Path, version: str) -> dict:
    """The notes file of release `version`: its own entry, or an empty list when it has no notes.

    `repo` is checked out at the release's tag, so `generate` ranks that release first among those
    with notes and its 20-release limit never cuts it off. Older releases are dropped here.
    """
    fmt = user_notes._notes_format()
    everything = user_notes.generate(repo, version)  # it refuses a version that is not X.Y.Z, and a shallow clone
    own = fmt.version_text(fmt.parse_version(version))
    return {**everything, "releases": [entry for entry in everything["releases"] if entry["version"] == own]}


def whats_new_section(release) -> str:
    """The section for one release of the notes format: the marker, a heading and one list item per note."""
    items = [f"- **{note.title}**\n\n  {note.text}" if note.title else f"- {note.text}" for note in release.notes]
    return "\n\n".join([PAGE_MARKER, "## What's new", *items])


def page_body(body: str, notes_text: str) -> str:
    """`body` with the What's new section of the notes file on top, or `body` itself when it needs none."""
    releases = user_notes._notes_format().parse_document(notes_text)  # the cell's reader: a file it refuses starts no page
    if not releases or PAGE_MARKER in body:
        return body
    section = whats_new_section(releases[0])  # the file holds this release alone
    return f"{section}\n\n{body}" if body.strip() else f"{section}\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("file", help="write the notes file of one release")
    build.add_argument("--repo", type=Path, default=Path("."), help="a full clone with the version tags")
    build.add_argument("--version", required=True, help="the release, as X.Y.Z")
    build.add_argument("--output", type=Path, required=True, help="where to write the notes file")

    page = commands.add_parser("page", help="write a release page body that starts with What's new")
    page.add_argument("--notes", type=Path, required=True, help="the release's notes file")
    page.add_argument("--body", type=Path, required=True, help="the release page body as it is now")
    page.add_argument("--output", type=Path, required=True, help="where to write the body")

    args = parser.parse_args(argv)
    if args.command == "file":
        document = release_document(args.repo, args.version)
        user_notes.write_notes_file(args.output, document)
        entries = document["releases"]
        print(f"Wrote {len(entries[0]['notes']) if entries else 0} note(s) of release {args.version} to {args.output}")
        return 0
    # Bytes in and out keep a body's line endings exactly, so an unchanged body is byte-identical.
    body = page_body(args.body.read_bytes().decode("utf-8"), args.notes.read_text(encoding="utf-8"))
    args.output.write_bytes(body.encode("utf-8"))
    print(f"Wrote the release page body to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
