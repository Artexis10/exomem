"""User notes: one plain sentence and a short title per user-visible change, assigned to releases from the version tags.

Two subcommands share one definition of a note (openspec change tell-users-whats-new):

  check     The pull-request check. A feat, fix or perf pull request adds a `user-notes/*.md`
            file or has the body line `User note: none`. A note that the pull request adds or
            changes must have the right shape: a title in YAML front matter and one plain
            sentence. It never judges a note's wording.
  generate  The release build. It gives each note to the earliest version tag whose history
            contains the commit that added the file, and a note in no tag to the version being
            built. It writes the notes file that the cell image carries, in the format that
            `src/exomem/release_notes_format.py` defines and the cell reads.

A note file is front matter between two `---` lines, then the sentence:

    ---
    title: Batch moves and renames finish
    ---
    Asking your assistant to move or rename a batch of pages now finishes.

PyYAML reads the front matter, so both jobs that run this script install it. `generate` accepts a file
with no front matter (a note merged without the check) and gives it a null title.

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
TITLE_MAX_CHARS = 48
FRONT_MATTER_FENCE = "---"
QUOTE_ADVICE = 'put the title in double quotes: `title: "Your title"`'
#: The front-matter keys a note may carry. A later key (a kind, new or fixed) joins this set.
FRONT_MATTER_KEYS = frozenset({"title"})
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


def _is_plain_text(text: str) -> bool:
    """Whether CommonMark reads `text` as one paragraph of text and gives back the same characters.

    A parser decides, not a pattern: emphasis, links, code, HTML, entities, lists and headings each
    change the token stream or the text, so none passes.
    """
    from markdown_it import MarkdownIt

    tokens = MarkdownIt("commonmark").parse(text)
    if [token.type for token in tokens] != ["paragraph_open", "inline", "paragraph_close"]:
        return False
    children = tokens[1].children or []
    return all(child.type == "text" for child in children) and "".join(child.content for child in children) == text


@dataclass(frozen=True)
class ParsedNote:
    """A note file read apart: `fields` is the front matter (None when the file has none), `body` the sentence."""

    fields: dict | None
    body: str
    problems: tuple[str, ...] = ()
    raw_title: str | None = None  # an unquoted title's source text to the end of its line, for the read-back check


def parse_note(text: str) -> ParsedNote:
    """Split a note file into its front matter and its body. A fence that opens and never closes is a problem."""
    lines = text.removeprefix("\ufeff").strip().splitlines()  # an editor may add a byte order mark
    if not lines or lines[0].strip() != FRONT_MATTER_FENCE:
        return ParsedNote(None, "\n".join(lines))
    closing = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == FRONT_MATTER_FENCE), None)
    if closing is None:
        return ParsedNote(None, "", (f"the front matter opens with `{FRONT_MATTER_FENCE}` and never closes; add a closing `{FRONT_MATTER_FENCE}` line",))
    import yaml  # the release build installs it beside markdown-it-py (see .github/workflows)

    body = "\n".join(lines[closing + 1 :]).strip()
    source = "\n".join(lines[1:closing])
    try:
        fields = yaml.safe_load(source)
        tree = yaml.compose(source, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        return ParsedNote(None, body, (f"the front matter is not valid YAML; {QUOTE_ADVICE}",))
    if fields is None:
        fields = {}
    if not isinstance(fields, dict):
        return ParsedNote(None, body, ("the front matter is not a list of `key: value` lines; write `title: Your title`",))
    return ParsedNote(fields, body, raw_title=_plain_title_source(tree, source))


def _plain_title_source(tree: object, source: str) -> str | None:
    """The source text of an unquoted `title` value, from where the parser says it starts to the end of that line.

    The parser's positions find the key, so an indented or quoted `title` key is found as well. A quoted or
    block value is decoded exactly by YAML and needs no read-back, so it gives None.
    """
    from yaml.nodes import MappingNode, ScalarNode

    if not isinstance(tree, MappingNode):
        return None
    for key, value in tree.value:
        if isinstance(key, ScalarNode) and key.value == "title" and isinstance(value, ScalarNode):
            if value.style is not None or not value.value:
                return None
            return source[value.start_mark.index :].split("\n", 1)[0].strip()
    return None


def title_problems(title: object, raw: str | None = None) -> list[str]:
    """What is wrong with a note's title, in the words an author reads.

    `raw` is the title's source text from its value to the end of its line. YAML cuts a plain value at ` #`, reads `Yes`, `On` and
    numbers as other types, and a quoted value is decoded exactly, so only an unquoted value (the only one with a `raw`) must read back as written.
    """
    if title is None:
        return ['the title is empty; write `title: "Your title"`']
    if not isinstance(title, str):
        return [f"YAML reads the title as {title!r}, not as text; {QUOTE_ADVICE}"]
    title = title.strip()
    if not title:
        return ['the title is empty; write `title: "Your title"`']
    problems = []
    if raw is not None and "\n" not in title and title != raw:
        problems.append(f"YAML reads the title as {title!r}, not as written; {QUOTE_ADVICE}")
    if len(title) > TITLE_MAX_CHARS:
        problems.append(f"the title has {len(title)} characters; the limit is {TITLE_MAX_CHARS}")
    if "\n" in title:
        problems.append("write the title on one line")
    elif not _is_plain_text(title):
        problems.append("the title uses Markdown syntax; write plain words")
    if title.endswith("."):
        problems.append("remove the full stop at the end of the title")
    return problems


def body_problems(text: str) -> list[str]:
    """What is wrong with the sentence of a note. Empty when the shape is right."""
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


def note_problems(text: str, *, require_title: bool = True) -> tuple[list[str], str | None, str]:
    """`(problems, title, body)` for a note file, the problems in the words an author reads.

    The check requires the title. `generate` passes `require_title=False`, so a note from an older
    file that was merged without the check, and so has no front matter, still ships with a null title.
    """
    parsed = parse_note(text)
    problems = list(parsed.problems)
    title = None
    if parsed.fields is not None:
        unknown = sorted(str(key) for key in parsed.fields if key not in FRONT_MATTER_KEYS)
        if unknown:
            allowed = ", ".join(sorted(FRONT_MATTER_KEYS))
            problems.append(f"the front matter has unknown key(s) {', '.join(unknown)}; remove them (allowed: {allowed})")
        if "title" in parsed.fields:
            problems.extend(title_problems(parsed.fields["title"], parsed.raw_title))
            if isinstance(parsed.fields["title"], str):
                title = parsed.fields["title"].strip()
        elif require_title:
            problems.append("the front matter has no title; add `title: Your title`")
    elif require_title and not parsed.problems:
        problems.append(f"the note has no title; start the file with `{FRONT_MATTER_FENCE}`, `title: Your title`, `{FRONT_MATTER_FENCE}`")
    if not parsed.problems:
        problems.extend(body_problems(parsed.body))
    return problems, title, parsed.body


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
        errors.extend(f"{note.path}: {problem}" for problem in note_problems(note.text)[0])
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
    title: str | None
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
        problems, title, body = note_problems(text, require_title=False)
        if problems:
            _warning(path, f"left out of the release notes: {'; '.join(problems)}")
            continue
        commit, stamp = added[path]
        if commit not in containing:
            containing[commit] = _earliest_release_containing(repo, commit)
        release = containing[commit] or building
        releases.setdefault(release, []).append(_Note(path, title, body, stamp))
    head_time = int(_git(repo, "log", "-1", "--format=%ct", "HEAD").strip())
    newest_first = sorted(releases, reverse=True)[:KEEP_RELEASES]
    document = fmt.document(
        fmt.Release(
            fmt.version_text(release),
            release,
            _date(tags.get(release, head_time)),
            tuple(
                fmt.Note(note.title, note.text)
                for note in sorted(releases[release], key=lambda n: (-n.added_at, n.path))
            ),
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
