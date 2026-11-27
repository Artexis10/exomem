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


# --------------------------------------------------------------------------
# scripts/release_notes_asset.py: what one release publishes of its notes
# --------------------------------------------------------------------------


@pytest.fixture
def asset(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPT.parent))  # the script imports user_notes beside it, as it does when run
    script = ROOT / "scripts" / "release_notes_asset.py"
    spec = importlib.util.spec_from_file_location("release_notes_asset_under_test", script)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def notes_file_text(*notes: dict) -> str:
    entry = {"version": "1.3.0", "released_on": "2026-02-02", "notes": list(notes)}
    return json.dumps({"schema": 2, "releases": [entry] if notes else []})


def test_a_releases_file_holds_the_notes_its_own_tag_has_and_no_other_release(asset, repo):
    """The file of 1.3.0 is built after 1.4.0 exists and edited a note of 1.3.0: it must still hold the note as 1.3.0 shipped it."""
    repo.commit("2026-01-10", {"user-notes/older.md": note_file("Older", "Older change.")})
    repo.tag("v1.2.0")
    repo.commit("2026-02-01", {"user-notes/first.md": note_file("First", "First change.")})
    repo.commit("2026-02-02", {"user-notes/second.md": note_file(None, "Second change.")})
    repo.tag("v1.3.0")
    repo.commit("2026-03-01", {"user-notes/first.md": note_file("First", "First change, reworded."), "user-notes/new.md": note_file("New", "New change.")})
    repo.tag("v1.4.0")

    document = asset.release_document(repo.path, "v1.3.0")

    assert document == {
        "schema": 2,
        "releases": [
            {
                "version": "1.3.0",
                "released_on": "2026-02-02",
                "notes": [{"title": None, "text": "Second change."}, {"title": "First", "text": "First change."}],
            }
        ],
    }
    assert [r.version for r in release_notes_format.parse_document(json.dumps(document))] == ["1.3.0"]


def test_a_release_without_notes_gets_an_empty_list_that_the_cells_reader_accepts(asset, repo):
    """The file must exist for such a release: a missing file has to mean the notes are missing."""
    repo.commit("2026-01-10", {"user-notes/older.md": note_file("Older", "Older change.")})
    repo.tag("v1.2.0")
    repo.commit("2026-01-20", {"code": "a"})
    repo.tag("v1.2.1")

    document = asset.release_document(repo.path, "v1.2.1")

    assert document == {"schema": 2, "releases": []}
    assert release_notes_format.parse_document(json.dumps(document)) == ()


def test_the_page_starts_with_whats_new_and_shows_a_note_without_a_title_by_its_sentence(asset):
    notes_file = notes_file_text(
        {"title": "Photos keep their date", "text": "Imported photos keep the day they were taken."},
        {"title": None, "text": "Searches find more."},
    )

    body = asset.page_body("## 1.3.0\n\n* a technical change\n", notes_file)

    assert body.splitlines()[:3] == [asset.PAGE_MARKER, "", "## What's new"]
    assert body.index("**Photos keep their date**") < body.index("Imported photos keep the day they were taken")
    assert "- Searches find more\\." in body.splitlines()  # no title: the sentence alone
    assert body.endswith("## 1.3.0\n\n* a technical change\n")  # the changelog follows, untouched


def test_a_release_without_notes_leaves_its_page_alone(asset):
    changelog = "## 1.2.1\n\n* a technical change\n"

    assert asset.page_body(changelog, notes_file_text()) == changelog


def test_a_page_that_has_the_marker_is_left_as_it_is_and_a_page_without_it_gets_one_section(asset):
    notes_file = notes_file_text({"title": "First", "text": "First change."})
    changelog = "## 1.3.0\r\n\r\n* a technical change\r\n"

    first = asset.page_body(changelog, notes_file)
    second = asset.page_body(first, notes_file)

    assert first != changelog and first.count(asset.PAGE_MARKER) == 1
    assert first.endswith(changelog)  # the changelog's own line endings survive
    assert second == first


def test_note_text_reads_as_plain_letters_whatever_it_holds(asset):
    """GitHub must show a sentence as written: CommonMark finds no syntax in it, and `@` and `#` are broken up
    so GitHub's own linking of names and issue numbers (which a backslash does not stop) finds nothing."""
    from markdown_it import MarkdownIt

    text = "Ask @someone about #123 at https://example.com or me@example.com, use * freely, _a_ <b>x</b> [y](z) `c` $5 & $6."

    shown = asset._plain(text)

    tokens = MarkdownIt("commonmark").parse(shown)
    assert [token.type for token in tokens] == ["paragraph_open", "inline", "paragraph_close"]
    assert {child.type for child in tokens[1].children} == {"text"}
    assert "".join(child.content for child in tokens[1].children) == text.replace("@", "@\u200b").replace("#", "#\u200b")


def releases(*rows):
    """GitHub's release list as `_published_releases` returns it: (tag, assets, draft) rows, newest first."""
    return [{"tag": tag, "assets": assets, "draft": draft} for tag, assets, draft in rows]


def test_a_run_checks_the_newest_twenty_published_releases_that_have_assets_and_lack_the_file(asset):
    file = asset.NOTES_ASSET
    listing = releases(
        ("v2.0.0", [], True),  # a draft is not published and does not count toward the twenty
        ("v1.9.0", [], False),  # publication has not started: the release controller still reads this page
        ("v1.8.0", ["sample.whl"], False),  # lacks the file
        ("v1.7.0", ["sample.whl", file], False),  # has it
        ("v1.6.0-rc.1", ["sample.whl"], False),  # not vX.Y.Z: the notes format cannot name it
        ("v1.5.0", ["sample.whl"], False),  # lacks the file
        ("v1.4.0", ["sample.whl"], False),  # beyond the limit
    )

    assert asset.newest_missing(listing, limit=5) == ["v1.8.0", "v1.5.0"]


class FakeGitHub:
    """`gh` and the upload command, recorded: the release page as it is and what each call did to it."""

    def __init__(self, body: str) -> None:
        self.body = body
        self.calls: list[str] = []

    def __call__(self, *command: str) -> str:
        if command[:3] == ("gh", "release", "view"):
            return json.dumps({"body": self.body})
        if command[:3] == ("gh", "release", "edit"):
            self.calls.append("edit page")
            self.body = Path(command[command.index("--notes-file") + 1]).read_text("utf-8")
        elif "upload-file" in command:
            self.calls.append("upload file")
        else:
            raise AssertionError(f"unexpected call {command}")
        return ""


@pytest.fixture
def released(repo):
    repo.commit("2026-02-01", {"user-notes/first.md": note_file("First", "First change.")})
    repo.tag("v1.3.0")
    return repo


def test_the_page_is_written_before_the_file_is_uploaded(asset, released, tmp_path, monkeypatch):
    """A release that has the file must have its section: the file is the sweep's proof that the release is done."""
    github = FakeGitHub("## 1.3.0\n\n* a technical change\n")
    monkeypatch.setattr(asset, "_run", github)

    message = asset.publish_release(released.path, "v1.3.0", tmp_path / "out", dry_run=False)

    assert github.calls == ["edit page", "upload file"]
    assert github.body.startswith(asset.PAGE_MARKER) and "First change" in github.body
    assert "now starts with What's new" in message


def test_a_release_that_has_its_page_section_is_not_edited_again_but_still_gets_its_file(asset, released, tmp_path, monkeypatch):
    first = FakeGitHub("## 1.3.0\n\n* a technical change\n")
    monkeypatch.setattr(asset, "_run", first)
    asset.publish_release(released.path, "v1.3.0", tmp_path / "out", dry_run=False)
    second = FakeGitHub(first.body)  # the page as the first run left it, the upload having failed
    monkeypatch.setattr(asset, "_run", second)

    asset.publish_release(released.path, "v1.3.0", tmp_path / "out", dry_run=False)

    assert second.calls == ["upload file"]
    assert second.body.count(asset.PAGE_MARKER) == 1


def test_a_dry_run_builds_the_file_and_changes_nothing_on_github(asset, released, tmp_path, monkeypatch):
    github = FakeGitHub("## 1.3.0\n\n* a technical change\n")
    monkeypatch.setattr(asset, "_run", github)

    message = asset.publish_release(released.path, "v1.3.0", tmp_path / "out", dry_run=True)

    assert github.calls == []
    assert (tmp_path / "out" / "v1.3.0" / asset.NOTES_ASSET).is_file()
    assert "nothing written to GitHub" in message


def test_one_releases_failure_does_not_stop_the_others_and_fails_the_run(asset, released, tmp_path, monkeypatch, capsys):
    released.commit("2026-03-01", {"code": "b"})
    released.tag("v1.4.0")
    github = FakeGitHub("## changelog\n")

    def run(*command):
        if "v1.3.0" in command and "upload-file" in command:
            raise subprocess.CalledProcessError(1, command, stderr="immutable release asset differs")
        return github(*command)

    monkeypatch.setattr(asset, "_run", run)

    status = asset.publish(released.path, ["v1.3.0", "v1.4.0"], tmp_path / "out", dry_run=False)

    assert status == 1
    assert github.calls == ["edit page", "upload file"]  # v1.3.0's page, then v1.4.0's file: the first release's failure stopped nothing
    assert "release notes failed for v1.3.0" in capsys.readouterr().err
