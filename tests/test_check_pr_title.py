"""Contract tests for the pull-request title release guard."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "check_pr_title.py"
WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "pr-title.yml"


def _load_script_module():
    assert SCRIPT.is_file(), "the reusable PR-title checker must exist"
    spec = importlib.util.spec_from_file_location("check_pr_title_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "title",
    [
        "fix: preserve release commits",
        "feat(parser): accept scoped titles",
        "feat!: change the public contract",
        "feat(api)!: change the public contract",
        "chore(deps-dev): refresh the lockfile",
        "revert: restore the previous behavior",
        "refactor!: remove the legacy contract",
        "docs(release): explain the title contract",
    ],
)
def test_validate_pr_title_accepts_conventional_commit_headers(title: str) -> None:
    checker = _load_script_module()

    assert checker.validate_pr_title(title) is None


@pytest.mark.parametrize(
    "title",
    [
        "Fix release title parsing",
        "fix release title parsing",
        "Fix: reject uppercase types",
        "fix:",
        "fix: ",
        "fix(): reject an empty scope",
        "fix(scope: reject an unclosed scope",
        " fix: reject leading whitespace",
        "fix: reject trailing whitespace ",
        "fix(scope)! : reject spacing before the colon",
    ],
)
def test_validate_pr_title_rejects_unparseable_squash_headers(title: str) -> None:
    checker = _load_script_module()

    assert checker.validate_pr_title(title) is not None


def test_main_reports_a_useful_error_for_an_invalid_title(capsys) -> None:
    checker = _load_script_module()

    assert checker.main(["Fix the release pipeline"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "Invalid PR title" in output.err
    assert "fix: describe the change" in output.err
    assert "feat(parser)!: describe the breaking change" in output.err


def test_main_accepts_a_valid_title(capsys) -> None:
    checker = _load_script_module()

    assert checker.main(["fix(release): preserve parsed squash commits"]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert "Valid PR title" in output.out


def test_workflow_checks_edited_titles_with_the_trusted_base_script() -> None:
    assert WORKFLOW.is_file(), "the PR-title workflow must exist"
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "pull_request_target:" in text
    assert "types: [opened, edited, reopened, synchronize]" in text
    assert "permissions:\n  contents: read" in text
    assert "ref: ${{ github.event.pull_request.base.sha }}" in text
    assert "PR_TITLE: ${{ github.event.pull_request.title }}" in text
    assert 'python scripts/check_pr_title.py "$PR_TITLE"' in text
