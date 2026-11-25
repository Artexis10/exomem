"""User notes: one plain sentence per user-visible change, assigned to releases from the version tags.

Two subcommands share one definition of a note (openspec change tell-users-whats-new):

  check     The pull-request check. A feat, fix or perf pull request adds a `user-notes/*.md`
            file or has the body line `User note: none`. A note that the pull request adds or
            changes must have the right shape. It never judges a note's wording.
  generate  The release build. It gives each note to the earliest version tag whose history
            contains the commit that added the file, and a note in no tag to the version being
            built. It writes the notes file that the cell image carries, in the format that
            `src/exomem/release_notes_format.py` defines and the cell reads.

Run `check` from the trusted base revision and read the pull request only as data.
`generate` needs the full history and the version tags: a shallow clone would date every
note wrongly, so it refuses one.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

NOTES_DIR = "user-notes"
NOTE_MAX_CHARS = 200
KEEP_RELEASES = 20
NO_NOTE_LINE = "user note: none"
ROOT = Path(__file__).resolve().parents[1]
RELEASE_CONFIG = ROOT / "release-please-config.json"
FORMAT_MODULE = ROOT / "src" / "exomem" / "release_notes_format.py"

Version = tuple[int, int, int]


def deciding_types(config: Path = RELEASE_CONFIG) -> frozenset[str]:
    """The Conventional Commit types that release-please lists in the changelog: what a user can notice.

    Read from release-please-config.json, so a type added there reaches this check with no second edit.
    """
    try:
        sections = json.loads(config.read_text(encoding="utf-8"))["packages"]["."]["changelog-sections"]
        return frozenset(section["type"] for section in sections if not section.get("hidden", False))
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise SystemExit(f"cannot read the changelog sections in {config}: {error}") from error


def _notes_format():
    """The notes file's format, loaded by path: the release build runs this script without the package installed."""
    name = "_release_notes_format"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, FORMAT_MODULE)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module  # a dataclass resolves its annotations through its module
        try:
            spec.loader.exec_module(module)
        except OSError as error:
            del sys.modules[name]
            raise SystemExit(f"generate: cannot load the notes format from {FORMAT_MODULE}: {error}") from error
    return sys.modules[name]


def _escape(text: str, *, in_property: bool = False) -> str:
    """Escape `text` for a GitHub workflow command, so a file name cannot start a command of its own."""
    text = text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return text.replace(":", "%3A").replace(",", "%2C") if in_property else text


def _warning(path: str, message: str) -> None:
    """A warning that the Actions log shows against `path`. It never fails the build."""
    print(f"::warning file={_escape(path, in_property=True)}::{_escape(message)}")


def is_note_path(path: str) -> bool:
    """A note is a Markdown file directly under `user-notes/`; the authors' README is not one."""
    location = PurePosixPath(path)
    return location.parent == PurePosixPath(NOTES_DIR) and location.suffix == ".md" and location.name != "README.md"


# --------------------------------------------------------------------------
# The shape of a note
# --------------------------------------------------------------------------


def _is_plain_text(note: str) -> bool:
    """Whether CommonMark reads `note` as one paragraph of text and gives back the same characters.

    A parser decides, not a pattern: emphasis, links, code, HTML, entities, lists and headings each
    change the token stream or the text, so none passes.
    """
    from markdown_it import MarkdownIt

    tokens = MarkdownIt("commonmark").parse(note)
    if [token.type for token in tokens] != ["paragraph_open", "inline", "paragraph_close"]:
        return False
    children = tokens[1].children or []
    return all(child.type == "text" for child in children) and "".join(child.content for child in children) == note


def note_problems(text: str) -> list[str]:
    """What is wrong with the shape of a note, in the words an author reads. Empty when the shape is right."""
    note = text.strip()
    if not note:
        return ["the note is empty"]
    problems = []
    if len(note) > NOTE_MAX_CHARS:
        problems.append(f"the note has {len(note)} characters; the limit is {NOTE_MAX_CHARS}")
    if "\n" in note:
        problems.append("write the note as one paragraph on one line")
    elif not _is_plain_text(note):
        problems.append("the note uses Markdown syntax; write plain words")
    return problems


# --------------------------------------------------------------------------
# The pull-request check
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ChangedNote:
    """A note file that a pull request adds or changes. `text` is None when the file is not UTF-8."""

    path: str
    added: bool
    text: str | None


def has_no_note_line(body: str) -> bool:
    return any(line.strip().casefold() == NO_NOTE_LINE for line in body.splitlines())


def check_pull_request(title: str, body: str, notes: Sequence[ChangedNote], deciding: Collection[str]) -> list[str]:
    """What stops this pull request, one sentence each. Empty when it passes."""
    # Only the check needs the title grammar, so `generate` runs from this one file in the release job.
    from check_pr_title import conventional_type

    errors = []
    for note in notes:
        if note.text is None:
            errors.append(f"{note.path}: the file is not UTF-8 text")
            continue
        errors.extend(f"{note.path}: {problem}" for problem in note_problems(note.text))
    kind = conventional_type(title)
    if kind in deciding and not any(note.added for note in notes) and not has_no_note_line(body):
        errors.append(
            f"a {kind} pull request needs a user note: add a file `{NOTES_DIR}/<short-name>.md` "
            f"(see {NOTES_DIR}/README.md), or put the line `User note: none` in the pull-request body"
        )
    return errors


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout


def _blob(repo: Path, revision: str, path: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo), "show", f"{revision}:{path}"], check=True, capture_output=True
    ).stdout


def _decode(data: bytes) -> str | None:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def changed_notes(repo: Path, base: str, head: str) -> list[ChangedNote]:
    """The note files that `head` adds or changes against `base`, read from `head`."""
    fields = _git(repo, "diff", "--no-renames", "--name-status", "-z", base, head, "--", NOTES_DIR).split("\0")
    changes = zip(fields[0::2], fields[1::2], strict=False)
    return [
        ChangedNote(path, status == "A", _decode(_blob(repo, head, path)))
        for status, path in changes
        if status in ("A", "M") and is_note_path(path)
    ]


# --------------------------------------------------------------------------
# The release notes file
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Note:
    path: str
    text: str
    added_at: int


def _tag_version(name: str) -> Version | None:
    """The version of a release tag: `v` and then `X.Y.Z`, the shape the notes format gives a version."""
    return _notes_format().parse_version(name[1:]) if name.startswith("v") else None


def _release_tags(repo: Path) -> dict[Version, int]:
    """Each release tag's version and creation time (the commit's for a lightweight tag)."""
    tags = {}
    for line in _git(repo, "for-each-ref", "--format=%(refname:strip=2) %(creatordate:unix)", "refs/tags/v*").splitlines():
        name, _, stamp = line.partition(" ")
        version = _tag_version(name)
        if version is not None:
            tags[version] = int(stamp)
    return tags


def _add_commits(repo: Path) -> dict[str, tuple[str, int]]:
    """For each note path: the commit that added it and that commit's time. The newest add counts."""
    log = _git(
        repo, "-c", "core.quotepath=off", "log", "--no-renames", "--diff-filter=A", "--name-only",
        "--format=%x01%H %ct", "HEAD", "--", NOTES_DIR,
    )
    added: dict[str, tuple[str, int]] = {}
    commit: tuple[str, int] | None = None
    for line in log.splitlines():
        if line.startswith("\x01"):
            name, _, stamp = line[1:].partition(" ")
            commit = (name, int(stamp))
        elif commit is not None and is_note_path(line):
            added.setdefault(line, commit)
    return added


def _earliest_release_containing(repo: Path, commit: str) -> Version | None:
    names = _git(repo, "tag", "--list", "v*", "--contains", commit).split()
    versions = [version for name in names if (version := _tag_version(name)) is not None]
    return min(versions, default=None)


def _date(stamp: int) -> str:
    return datetime.fromtimestamp(stamp, UTC).date().isoformat()


def generate(repo: Path, version: str) -> dict:
    """The notes of every release, newest first, from the notes in `repo`'s HEAD and its tags.

    `version` is the release being built: it receives the notes that no tag contains yet. Within a
    release the newest note comes first, so a reader that wants one note takes the first. A note
    that breaks the shape rule is left out with a warning, since it may have skipped the pull-request
    check; the build never fails for it. The result is checked by the notes format's own parser, the
    one the cell uses, before it is returned.
    """
    fmt = _notes_format()
    building = fmt.parse_version(version)
    if building is None:
        raise SystemExit(f"generate: the version must read X.Y.Z, got {version!r}")
    if _git(repo, "rev-parse", "--is-shallow-repository").strip() == "true":
        raise SystemExit("generate: this clone is shallow; fetch the full history and the tags first")
    tags = _release_tags(repo)
    added = _add_commits(repo)
    paths = [p for p in _git(repo, "ls-tree", "-r", "--name-only", "-z", "HEAD", "--", NOTES_DIR).split("\0") if is_note_path(p)]
    releases: dict[Version, list[_Note]] = {}
    containing: dict[str, Version | None] = {}
    for path in sorted(paths):
        text = _decode(_blob(repo, "HEAD", path))
        if path not in added or text is None or not text.strip():
            _warning(path, "left out of the release notes: no readable text, or no commit that added it")
            continue
        if problems := note_problems(text):
            _warning(path, f"left out of the release notes: {'; '.join(problems)}")
            continue
        commit, stamp = added[path]
        if commit not in containing:
            containing[commit] = _earliest_release_containing(repo, commit)
        release = containing[commit] or building
        releases.setdefault(release, []).append(_Note(path, text.strip(), stamp))
    head_time = int(_git(repo, "log", "-1", "--format=%ct", "HEAD").strip())
    newest_first = sorted(releases, reverse=True)[:KEEP_RELEASES]
    document = fmt.document(
        fmt.Release(
            fmt.version_text(release),
            release,
            _date(tags.get(release, head_time)),
            tuple(note.text for note in sorted(releases[release], key=lambda n: (-n.added_at, n.path))),
        )
        for release in newest_first
    )
    try:
        fmt.parse_document(json.dumps(document))
    except ValueError as error:
        raise SystemExit(f"generate: the notes file would not pass the cell's reader: {error}") from error
    return document


def write_notes_file(output: Path, document: dict) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    staged = output.with_name(output.name + ".tmp")
    staged.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    staged.replace(output)


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    check = commands.add_parser("check", help="check a pull request's user note")
    check.add_argument("--repo", type=Path, default=Path("."), help="a clone that holds the pull request (read as data)")
    check.add_argument("--base", required=True, help="the revision the pull request is compared with")
    check.add_argument("--head", required=True, help="the revision that holds the pull request's files")
    check.add_argument("--title", required=True, help="the pull-request title")
    check.add_argument("--body", default="", help="the pull-request body")

    build = commands.add_parser("generate", help="write the release notes file")
    build.add_argument("--repo", type=Path, default=Path("."), help="a full clone with the version tags")
    build.add_argument("--version", required=True, help="the version being built, as X.Y.Z")
    build.add_argument("--output", type=Path, required=True, help="where to write the notes file")

    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            errors = check_pull_request(
                args.title, args.body, changed_notes(args.repo, args.base, args.head), deciding_types()
            )
            if errors:
                print("User note check failed:", file=sys.stderr)
                for error in errors:
                    # A file name may hold a line break; one error stays one line of the log.
                    print("  - " + error.replace("\n", " ").replace("\r", " "), file=sys.stderr)
                return 1
            print("User note check passed.")
            return 0
        document = generate(args.repo, args.version)
        write_notes_file(args.output, document)
        print(f"Wrote {len(document['releases'])} release(s) with notes to {args.output}")
        return 0
    except subprocess.CalledProcessError as error:
        detail = error.stderr if isinstance(error.stderr, str) else (error.stderr or b"").decode("utf-8", "replace")
        print(f"git failed: {' '.join(map(str, error.cmd))}\n{detail}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
