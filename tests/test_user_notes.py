"""User notes: the pull-request check and the release notes file (scripts/user_notes.py).

The generator runs against throwaway git repositories with real tags and commits, as the
release build runs it; the check runs on the strings and files a pull request carries.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from exomem import release_notes_format

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "user_notes.py"


@pytest.fixture
def notes(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPT.parent))  # the script imports its sibling, as it does when run
    spec = importlib.util.spec_from_file_location("user_notes_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)  # a dataclass resolves its annotations through its module
    spec.loader.exec_module(module)
    return module


def note_file(title: str | None, sentence: str) -> str:
    """A note file as authors write it: the title in front matter, then the sentence."""
    return f"---\ntitle: {title}\n---\n{sentence}\n" if title is not None else f"{sentence}\n"


class Repo:
    """A throwaway repository whose commits carry chosen dates, so tags and notes line up as in a release."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.mkdir(parents=True)
        self.git("init", "-q", "-b", "main")

    def git(self, *args: str, when: str | None = None) -> str:
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.test"}
        if when:
            env |= {"GIT_AUTHOR_DATE": f"{when}T12:00:00+00:00", "GIT_COMMITTER_DATE": f"{when}T12:00:00+00:00"}
        done = subprocess.run(["git", "-C", str(self.path), *args], env=env, check=True, capture_output=True, text=True)
        return done.stdout

    def commit(self, when: str, files: dict[str, str] | None = None) -> None:
        for name, text in (files or {}).items():
            target = self.path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", "change", when=when)

    def tag(self, name: str) -> None:
        self.git("tag", name)


@pytest.fixture
def repo(tmp_path) -> Repo:
    return Repo(tmp_path / "repo")


def test_each_note_goes_to_the_earliest_release_that_contains_it_and_a_new_note_to_the_build(notes, repo, tmp_path):
    """A note that lands in the wrong release is shown to people as news on the wrong day; a release with
    no note must not appear at all; a note edited after its release stays with that release."""
    repo.commit("2026-01-10", {"user-notes/README.md": "for authors\n", "user-notes/older.md": note_file("Older", "Older change.")})
    repo.tag("v1.2.0")
    repo.commit("2026-01-20", {"code": "a"})
    repo.tag("v1.2.1")  # a release with no note
    repo.commit("2026-02-01", {"user-notes/first-new.md": note_file("First new", "First new change.")})
    repo.commit("2026-02-02", {"user-notes/second-new.md": note_file("Second new", "Second new change.")})
    repo.tag("v1.3.0")
    repo.commit("2026-02-05", {"user-notes/older.md": note_file("Older, reworded", "Older change, reworded.")})
    repo.commit("2026-03-01", {"user-notes/unreleased.md": "Not released yet.\n"})  # a note merged without the check: no front matter
    output = tmp_path / "out" / "notes.json"

    assert notes.main(["generate", "--repo", str(repo.path), "--version", "1.4.0", "--output", str(output)]) == 0

    def entry(title, text):
        return {"title": title, "text": text}

    assert json.loads(output.read_text("utf-8")) == {
        "schema": 2,
        "releases": [
            {"version": "1.4.0", "released_on": "2026-03-01", "notes": [entry(None, "Not released yet.")]},
            {
                "version": "1.3.0",
                "released_on": "2026-02-02",
                "notes": [entry("Second new", "Second new change."), entry("First new", "First new change.")],
            },
            {"version": "1.2.0", "released_on": "2026-01-10", "notes": [entry("Older, reworded", "Older change, reworded.")]},
        ],
    }
    # The file as written must be one the cell's own reader accepts, not only one that looks right here.
    assert [release.version for release in release_notes_format.parse_document(output.read_text("utf-8"))] == [
        "1.4.0",
        "1.3.0",
        "1.2.0",
    ]


def test_only_the_newest_twenty_releases_with_notes_are_kept(notes, repo, monkeypatch):
    monkeypatch.setattr(notes, "KEEP_RELEASES", 2)
    for minor in range(1, 4):
        repo.commit(f"2026-01-0{minor}", {f"user-notes/n{minor}.md": f"Change {minor}.\n"})
        repo.tag(f"v1.{minor}.0")

    versions = [release["version"] for release in notes.generate(repo.path, "1.4.0")["releases"]]

    assert versions == ["1.3.0", "1.2.0"]


def test_a_shallow_clone_is_refused_rather_than_dating_every_note_from_its_boundary(notes, repo, tmp_path):
    repo.commit("2026-01-01", {"user-notes/a.md": "A change.\n"})
    repo.commit("2026-01-02", {"code": "x"})
    shallow = tmp_path / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth", "1", repo.path.as_uri(), str(shallow)], check=True)

    with pytest.raises(SystemExit, match="shallow"):
        notes.generate(shallow, "1.0.0")


SENTENCE = "Photos now keep the date they were taken, even when your chat app removed it."
GOOD = note_file("Photos keep their date", SENTENCE)


def changed(notes, text: str | None = GOOD, *, added: bool = True, path: str = "user-notes/photos.md"):
    return [notes.ChangedNote(path, added, text)]


def verdict(notes, title: str, body: str, files) -> list[str]:
    return notes.check_pull_request(title, body, files, notes.deciding_types())


@pytest.mark.parametrize(
    ("title", "body", "files", "passes"),
    [
        ("feat: share a page", "", [], False),
        ("fix(media): keep dates", "Body.\nUser note: none\n", [], True),
        ("perf: faster search", "", [], False),
        ("chore: refresh lockfile", "", [], True),
        ("feat: share a page", "", "good note", True),
        ("fix: keep dates", "", "a changed note, not an added one", False),
    ],
)
def test_a_feat_fix_or_perf_pull_request_adds_a_note_or_says_it_has_none(notes, title, body, files, passes):
    if files == "good note":
        files = changed(notes)
    elif files:
        files = changed(notes, added=False)

    assert (verdict(notes, title, body, files) == []) is passes


def test_the_deciding_types_are_the_changelog_types_that_release_please_does_not_hide(notes, tmp_path):
    """A type added to the changelog must reach the check without a second edit."""
    config = tmp_path / "release-please-config.json"
    config.write_text(json.dumps({"packages": {".": {"changelog-sections": [
        {"type": "feat", "section": "Features", "hidden": True},
        {"type": "refactor", "section": "Internals"},
        {"type": "chore", "section": "Maintenance", "hidden": True},
    ]}}}), encoding="utf-8")

    assert notes.deciding_types(config) == {"refactor"}


@pytest.mark.parametrize(
    "text",
    [
        note_file("Photos keep their date", "x" * 201),  # too long
        note_file("Photos keep their date", "First line.\nSecond line."),  # two lines
        note_file("Photos keep their date", ""),  # empty
        note_file("Photos keep their date", "- one item"),  # a block that is not a paragraph
        note_file("Photos keep their date", "Use **bold** words here."),  # inline syntax inside a paragraph
    ],
)
def test_a_note_that_breaks_the_shape_rule_fails_whatever_the_pull_request_type(notes, text):
    assert verdict(notes, "docs: x", "", changed(notes, text)) != []


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (SENTENCE + "\n", "the note has no title"),
        (f"---\nkind: fixed\n---\n{SENTENCE}\n", "the front matter has no title"),
        (f"---\ntitle:\n---\n{SENTENCE}\n", "the title is empty"),
        (f"---\ntitle: 2026\n---\n{SENTENCE}\n", "not as text; put the title in double quotes"),
        # YAML would turn these into a comment, a boolean and a syntax error: each must say to quote the title.
        (f"---\ntitle: Search tags #work again\n---\n{SENTENCE}\n", "not as written; put the title in double quotes"),
        (f"---\ntitle: Yes\n---\n{SENTENCE}\n", "not as text; put the title in double quotes"),
        # The key may be indented or quoted; the parser still finds it, so the title is still read back.
        (f"---\n  title: Search tags #work again\n---\n{SENTENCE}\n", "not as written; put the title in double quotes"),
        (f'---\n"title": Search tags #work again\n---\n{SENTENCE}\n', "not as written; put the title in double quotes"),
        (f"---\ntitle: @mentions work\n---\n{SENTENCE}\n", "put the title in double quotes"),
        (note_file("A" * 49, SENTENCE), "the title has 49 characters; the limit is 48"),
        (note_file("Photos keep their date.", SENTENCE), "remove the full stop at the end of the title"),
        (note_file("Photos **keep** their date", SENTENCE), "the title uses Markdown syntax"),
        (note_file("Photos [keep](x) dates", SENTENCE), "the title uses Markdown syntax"),
        (f"---\ntitle: |\n  two\n  lines\n---\n{SENTENCE}\n", "write the title on one line"),
        (f"---\ntitle: Photos keep their date\nkind: fixed\n---\n{SENTENCE}\n", "unknown key(s) kind"),
        (f"---\ntitle: Photos: dates\n---\n{SENTENCE}\n", "the front matter is not valid YAML; put the title in double quotes"),
        (f"---\ntitle: Photos keep their date\n{SENTENCE}\n", "never closes"),
    ],
)
def test_a_note_whose_title_breaks_the_rules_fails_and_says_what_to_change(notes, text, message):
    errors = verdict(notes, "feat: x", "", changed(notes, text))

    assert any(message in error for error in errors)


def test_a_note_that_starts_with_a_byte_order_mark_is_read_with_its_front_matter(notes):
    assert verdict(notes, "feat: x", "", changed(notes, "\ufeff" + GOOD)) == []
    assert notes.note_problems("\ufeff" + GOOD, require_title=False)[1:] == ("Photos keep their date", SENTENCE)


def test_a_quoted_title_may_hold_a_colon_a_hash_or_a_word_yaml_would_read_as_a_boolean(notes):
    """Quoting is the way out the messages point to, so it has to pass."""
    for title in ("Imports: faster", "Search tags #work again", "Yes"):
        quoted = f'---\ntitle: "{title}"\n---\n{SENTENCE}\n'

        assert verdict(notes, "feat: x", "", changed(notes, quoted)) == [], title


def test_a_plain_sentence_and_title_with_ordinary_punctuation_pass(notes):
    text = note_file("Faster, for 5 photos (2x)", "You can now save up to 5 photos at once, and it's faster (about 2x) for your chat app's files.")

    assert verdict(notes, "feat: x", "", changed(notes, text)) == []


def test_the_authors_readme_does_not_count_as_a_note(notes, repo, capsys):
    repo.commit("2026-01-01", {"code": "a"})
    base = repo.git("rev-parse", "HEAD").strip()
    repo.commit("2026-01-02", {"user-notes/README.md": "for authors\n"})

    argv = ["check", "--repo", str(repo.path), "--base", base, "--head", "HEAD", "--title", "feat: x"]

    assert notes.main(argv) == 1
    assert "needs a user note" in capsys.readouterr().err


def test_the_check_reads_the_added_note_from_the_pull_requests_files(notes, repo, capsys):
    repo.commit("2026-01-01", {"code": "a"})
    base = repo.git("rev-parse", "HEAD").strip()
    repo.commit("2026-01-02", {"user-notes/photos.md": GOOD})
    argv = ["check", "--repo", str(repo.path), "--base", base, "--head", "HEAD", "--title", "fix: dates"]

    assert notes.main(argv) == 0

    repo.commit("2026-01-03", {"user-notes/photos.md": note_file("Photos keep their date", "Use **bold**.")})
    assert notes.main(argv) == 1
    assert "user-notes/photos.md: the note uses Markdown syntax" in capsys.readouterr().err


def test_a_note_that_skipped_the_pull_request_check_is_left_out_with_a_warning_and_the_build_goes_on(notes, repo, capsys):
    repo.commit("2026-01-01", {"user-notes/good.md": note_file("Good", "A good change."), "user-notes/bad.md": note_file("Bad", "Use **bold**.")})
    repo.tag("v1.0.0")

    document = notes.generate(repo.path, "1.1.0")

    assert [release["notes"] for release in document["releases"]] == [[{"title": "Good", "text": "A good change."}]]
    assert capsys.readouterr().out == (
        "::warning file=user-notes/bad.md::left out of the release notes: the note uses Markdown syntax; write plain words\n"
    )


@pytest.mark.skipif(sys.platform == "win32", reason="a Windows file name cannot hold a line break")
def test_a_file_name_cannot_start_a_workflow_command_of_its_own(notes, repo, capsys):
    """A pull request controls note file names, and the Actions log reads `::` at the start of a line as a command."""
    repo.commit("2026-01-01", {"user-notes/x\n::error::injected.md": "A change.\n"})

    notes.generate(repo.path, "1.0.0")

    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("::warning file=user-notes/x%0A%3A%3Aerror%3A%3Ainjected.md::")
