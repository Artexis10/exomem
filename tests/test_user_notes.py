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
import yaml

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "user_notes.py"
MARKDOWN_PIN = next(
    line.split('"')[1] for line in (ROOT / "pyproject.toml").read_text("utf-8").splitlines() if "markdown-it-py==" in line
)


@pytest.fixture
def notes(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPT.parent))  # the script imports its sibling, as it does when run
    spec = importlib.util.spec_from_file_location("user_notes_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)  # a dataclass resolves its annotations through its module
    spec.loader.exec_module(module)
    return module


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
    repo.commit("2026-01-10", {"user-notes/README.md": "for authors\n", "user-notes/older.md": "Older change.\n"})
    repo.tag("v1.2.0")
    repo.commit("2026-01-20", {"code": "a"})
    repo.tag("v1.2.1")  # a release with no note
    repo.commit("2026-02-01", {"user-notes/first-new.md": "First new change.\n"})
    repo.commit("2026-02-02", {"user-notes/second-new.md": "Second new change.\n"})
    repo.tag("v1.3.0")
    repo.commit("2026-02-05", {"user-notes/older.md": "Older change, reworded.\n"})
    repo.commit("2026-03-01", {"user-notes/unreleased.md": "Not released yet.\n"})
    output = tmp_path / "out" / "notes.json"

    assert notes.main(["generate", "--repo", str(repo.path), "--version", "1.4.0", "--output", str(output)]) == 0

    assert json.loads(output.read_text("utf-8")) == {
        "schema": 1,
        "releases": [
            {"version": "1.4.0", "released_on": "2026-03-01", "notes": ["Not released yet."]},
            {"version": "1.3.0", "released_on": "2026-02-02", "notes": ["Second new change.", "First new change."]},
            {"version": "1.2.0", "released_on": "2026-01-10", "notes": ["Older change, reworded."]},
        ],
    }


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


GOOD = "Photos now keep the date they were taken, even when your chat app removed it."


def changed(notes, text: str | None = GOOD, *, added: bool = True, path: str = "user-notes/photos.md"):
    return [notes.ChangedNote(path, added, text)]


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

    assert (notes.check_pull_request(title, body, files) == []) is passes


@pytest.mark.parametrize(
    "text",
    [
        "x" * 201,
        "First line.\nSecond line.",
        "",
        "Use **bold** words here.",
        "See [the page](https://example.test) now.",
        "Run `save` to keep it.",
        "- one item",
        "# A heading",
        "Fish &amp; chips arrived.",
        "<b>New</b> look.",
    ],
)
def test_a_note_that_breaks_the_shape_rule_fails_whatever_the_title(notes, text):
    assert notes.check_pull_request("docs: x", "", changed(notes, text)) != []


def test_a_plain_sentence_with_ordinary_punctuation_passes(notes):
    text = "You can now save up to 5 photos at once, and it's faster (about 2x) for your chat app's files."

    assert notes.check_pull_request("feat: x", "", changed(notes, text)) == []


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
    repo.commit("2026-01-02", {"user-notes/photos.md": GOOD + "\n"})
    argv = ["check", "--repo", str(repo.path), "--base", base, "--head", "HEAD", "--title", "fix: dates"]

    assert notes.main(argv) == 0

    repo.commit("2026-01-03", {"user-notes/photos.md": "Use **bold**.\n"})
    assert notes.main(argv) == 1
    assert "user-notes/photos.md: the note uses Markdown syntax" in capsys.readouterr().err


def test_the_check_runs_the_trusted_base_script_and_reads_the_pull_request_only_as_data():
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "user-note.yml").read_text("utf-8"))
    steps = workflow["jobs"]["decision"]["steps"]
    run = next(step for step in steps if "user_notes.py check" in step.get("run", ""))["run"]

    assert set(workflow[True]["pull_request"]["types"]) >= {"opened", "edited", "reopened", "synchronize"}
    assert workflow["permissions"] == {"contents": "read"}
    # The first checkout is the base revision at the root: the script that judges the pull request.
    assert steps[0]["with"]["ref"] == "${{ github.event.pull_request.base.sha }}"
    # The second is the merge result in its own directory; the script reads it and never runs it.
    assert steps[1]["with"]["path"] == "pull-request"
    assert "--repo pull-request" in run
    # The parser's version comes from pyproject.toml, so the workflow keeps no second copy of the pin.
    assert MARKDOWN_PIN in run or "pyproject.toml" in run


def test_the_cloud_image_build_writes_the_notes_file_before_it_builds_the_image():
    """A missing file is served as an empty list and never fails, so a build that skipped this step
    would ship an image without notes and nothing would say so."""
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "release-please.yml").read_text("utf-8"))
    steps = workflow["jobs"]["build-cloud-images"]["steps"]
    names = [step.get("name", "") for step in steps]
    generate = next(step for step in steps if "user_notes.py generate" in step.get("run", ""))

    assert names.index(generate["name"]) < names.index("Build and push native cloud image")
    assert "src/exomem/user_release_notes.json" in generate["run"]
    assert steps[0]["with"]["fetch-depth"] == 0
    assert generate.get("continue-on-error") is True
