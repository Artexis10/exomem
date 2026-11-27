"""What each release publishes of its user notes: the `release-notes.json` asset and the start of its page.

The notes come from the same two definitions as the cell image's notes file: `scripts/user_notes.py`
gives each note to the first release that contains it, and `src/exomem/release_notes_format.py` is the
file format. This script adds no second assignment and no second serialiser. Two subcommands:

  missing  Prints the tags of the releases that have no `release-notes.json`, one per line: every release
           published since the start of the previous UTC month, and the release `--also` names (which may be
           older). The release list is read a page of 100 at a time, newest first, until a release older
           than that start.
  publish  For each tag given, builds that release's notes from its tag's tree, starts its page with a
           "What's new" section when it has notes, then uploads the file. Page first, so a release that
           has the file has its page. A tag that fails does not stop the others; the run then fails.
           `--dry-run` builds and reads, and writes nothing to GitHub.

A release's file holds that release alone: the entry `generate` gives it, or an empty list when it has no
notes, so a missing file always means the notes are missing and never that there are none. The notes are
read from the release tag's tree, and the file is built with the code of the revision that runs this
script, so a file healed later has the format that revision writes.

`publish` needs the full history and the version tags, as `generate` does, and imports what it imports
(markdown-it-py and PyYAML read the note shape). `missing` needs only the standard library and `gh`.
"""

from __future__ import annotations

import argparse
import json
import string
import subprocess
import sys
from collections.abc import Iterable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import user_notes  # the sibling script: a script run by path finds its own directory first

NOTES_ASSET = "release-notes.json"
#: GitHub's largest page of the release list.
PAGE_SIZE = 100
#: The first line of a release page's section. A comment shows nothing on the page, and a run finds its own
#: section by it, so no code reads the heading's words.
PAGE_MARKER = "<!-- evermatter-whats-new -->"
RECOVERY = Path(__file__).resolve().parents[1] / "infra" / "scripts" / "release_recovery.py"
GH_TIMEOUT_SECONDS = 300
ZERO_WIDTH_SPACE = "\u200b"


def _run(*command: str) -> str:
    return subprocess.run(
        command, check=True, capture_output=True, text=True, encoding="utf-8", timeout=GH_TIMEOUT_SECONDS
    ).stdout


def release_document(repo: Path, tag: str) -> dict:
    """The notes file of the release at `tag`: its own entry, or an empty list when it has no notes.

    `generate` reads the notes of the tag's own tree, so a release gets the notes it shipped with, and
    ranks that release first among those with notes, so its 20-release limit never cuts it off. The
    older releases it also returns are dropped here. The format is that of the code running this script.
    """
    fmt = user_notes._notes_format()
    everything = user_notes.generate(repo, tag.removeprefix("v"), head=f"refs/tags/{tag}")  # it refuses a shallow clone
    own = fmt.version_text(fmt.parse_version(tag.removeprefix("v")))
    return {**everything, "releases": [entry for entry in everything["releases"] if entry["version"] == own]}


def _plain(text: str) -> str:
    """`text` for the page: GitHub finds no mention, reference, link or emphasis in it, whatever a note or title holds.

    CommonMark lets any ASCII punctuation mark take a backslash (a set the specification fixes), so no
    character reads as emphasis, a link, an autolink, HTML or math. GitHub links `@name`, `#123` and an
    address after it has read the Markdown, and a backslash does not stop that, so a zero-width space follows
    each `@` and `#`. The notes file keeps the original text; only the page is escaped.
    """
    escaped = "".join(f"\\{char}" if char in string.punctuation else char for char in text)
    return escaped.replace("@", "@" + ZERO_WIDTH_SPACE).replace("#", "#" + ZERO_WIDTH_SPACE)


def whats_new_section(release) -> str:
    """The section for one release of the notes format: the marker, a heading and one list item per note."""
    items = [
        f"- **{_plain(note.title)}**\n\n  {_plain(note.text)}" if note.title else f"- {_plain(note.text)}"
        for note in release.notes
    ]
    return "\n\n".join([PAGE_MARKER, "## What's new", *items])


def page_body(body: str, notes_text: str) -> str:
    """`body` with the What's new section of the notes file on top, or `body` itself when it needs none.

    The section is written whenever the marker is absent, so a section that someone deleted comes back.
    """
    releases = user_notes._notes_format().parse_document(notes_text)  # the cell's reader: a file it refuses starts no page
    if not releases or PAGE_MARKER in body:
        return body
    section = whats_new_section(releases[0])  # the file holds this release alone
    return f"{section}\n\n{body}" if body.strip() else f"{section}\n"


def previous_month_start(now: datetime) -> datetime:
    """Midnight UTC on the first day of the month before `now`'s month."""
    first_of_month = now.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return (first_of_month - timedelta(days=1)).replace(day=1)


def why_left_alone(release: dict) -> str | None:
    """Why a run does not publish notes for `release`, or None when it is published and lacks the notes file.

    `release` is a row of `_release_row`. A tag that is not `vX.Y.Z` cannot name a release in the notes
    format. A release with no asset has not started publication: the release controller writes the changelog
    entry from its page, and dispatches the publisher only after that (scripts/release_cuts.py), so an edit of
    the page before then would change what the controller writes.
    """
    if release["draft"]:
        return "it is a draft"
    if user_notes._tag_version(release["tag"]) is None:
        return "its tag is not vX.Y.Z"
    if not release["assets"]:
        return "it has no asset yet, so the release controller has not finished with its page"
    if NOTES_ASSET in release["assets"]:
        return "it already has its notes file"
    return None


def missing_tags(pages: Iterable[Sequence[dict]], since: datetime, named: dict | None = None) -> list[str]:
    """The tags, newest first, of the releases published at or after `since` that lack the notes file, then `named` if it lacks it.

    `pages` are GitHub's release list, newest first, one page after another; reading stops after the first
    page that holds a release published before `since`, and the releases of that page before `since` are
    left out. `named` is one release outside that window that the caller asks for (a dispatch's tag).
    """
    tags: list[str] = []
    for page in pages:
        reached_older = False
        for release in page:
            if release["draft"]:
                continue
            if release["published_at"] < since:
                reached_older = True
            elif why_left_alone(release) is None:
                tags.append(release["tag"])
        if reached_older:
            break
    if named is not None and named["tag"] not in tags and why_left_alone(named) is None:
        tags.append(named["tag"])
    return tags


def _release_row(release: dict) -> dict:
    """One release of GitHub's API as `why_left_alone` and `missing_tags` read it."""
    published = release["published_at"]  # a draft has none
    return {
        "tag": release["tag_name"],
        "draft": release["draft"],
        "published_at": None if published is None else datetime.fromisoformat(published),
        "assets": [asset["name"] for asset in release["assets"]],
    }


def _release_pages() -> Iterator[list[dict]]:
    """GitHub's releases, newest first, a page of `PAGE_SIZE` at a time, read only as far as the caller goes."""
    page = 1
    while True:
        listing = json.loads(_run("gh", "api", f"repos/{{owner}}/{{repo}}/releases?per_page={PAGE_SIZE}&page={page}"))  # the repository: GH_REPO
        yield [_release_row(release) for release in listing]
        if len(listing) < PAGE_SIZE:
            return
        page += 1


def print_missing(also: str | None) -> None:
    """Print the tags that `missing_tags` finds for a run now, and say on stderr why `also` is left out when it is."""
    named = None
    if also:
        named = _release_row(json.loads(_run("gh", "api", f"repos/{{owner}}/{{repo}}/releases/tags/{also}")))
        reason = why_left_alone(named)
        if reason is not None:
            print(f"{also} is left alone: {reason}", file=sys.stderr)
    for tag in missing_tags(_release_pages(), previous_month_start(datetime.now(UTC)), named):
        print(tag)


def publish_release(repo: Path, tag: str, directory: Path, *, dry_run: bool) -> str:
    """Build the notes of the release at `tag`, start its page and upload its file. Says what it did."""
    document = release_document(repo, tag)
    notes_file = directory / tag / NOTES_ASSET
    user_notes.write_notes_file(notes_file, document)
    # JSON, not text, so the body's own line endings survive the read.
    body = json.loads(_run("gh", "release", "view", tag, "--json", "body"))["body"]
    new_body = page_body(body, notes_file.read_text(encoding="utf-8"))
    body_file = notes_file.with_name("release-body.md")
    body_file.write_bytes(new_body.encode("utf-8"))
    count = sum(len(entry["notes"]) for entry in document["releases"])
    changes_page = new_body != body
    if dry_run:
        page = "would start with What's new" if changes_page else "needs no What's new change"
        return f"dry run, {tag}: {count} note(s); the page {page}; nothing written to GitHub"
    if changes_page:
        _run("gh", "release", "edit", tag, "--notes-file", str(body_file))
    # The upload is the existing release upload path: it adds a missing file and refuses to replace a published one with other bytes.
    _run(sys.executable, str(RECOVERY), "upload-file", "--tag", tag, "--file", str(notes_file))
    page = "now starts with What's new" if changes_page else "needs no What's new change"
    return f"{tag}: {count} note(s); the page {page}; {NOTES_ASSET} is published"


def _why(error: BaseException) -> str:
    """One line for the log: a failed command's last line of output (its traceback's last line names the cause)."""
    if isinstance(error, subprocess.CalledProcessError):
        lines = (error.stderr or error.stdout or "").strip().splitlines()
        return lines[-1] if lines else f"exit status {error.returncode}"
    return str(error.code if isinstance(error, SystemExit) else error)


def publish(repo: Path, tags: Sequence[str], directory: Path, *, dry_run: bool) -> int:
    failures = []
    for tag in tags:
        try:
            print(publish_release(repo, tag, directory, dry_run=dry_run), flush=True)
        # One release's failure must not keep the others from their notes; the run still fails below.
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, ValueError, SystemExit) as error:
            failures.append(f"{tag}: {_why(error)}")
    for failure in failures:
        print(f"release notes failed for {failure}", file=sys.stderr)
    return 1 if failures else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    find = commands.add_parser("missing", help="print the releases since the previous month's start that have no notes file")
    find.add_argument("--also", metavar="TAG", help="a release of any age to include when it lacks the file")

    build = commands.add_parser("publish", help="publish the notes of the given releases")
    build.add_argument("tags", nargs="+", help="release tags, vX.Y.Z")
    build.add_argument("--repo", type=Path, default=Path("."), help="a full clone with the version tags")
    build.add_argument("--directory", type=Path, default=Path("release-notes"), help="where to build each release's files")
    build.add_argument("--dry-run", action="store_true", help="build and read; write nothing to GitHub")

    args = parser.parse_args(argv)
    if args.command == "missing":
        try:
            print_missing(args.also)
        except subprocess.CalledProcessError as error:  # `gh` says why, for example that a named tag has no release
            raise SystemExit(f"missing: {_why(error)}") from error
        return 0
    return publish(args.repo, args.tags, args.directory, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
