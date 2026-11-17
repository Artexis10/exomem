"""The incident regression runner reads outcomes the way the product records them.

Failures a green run would not reveal: a note that the agent superseded reads as untouched, and a
harness fault or judge error counts as a product failure.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from membench.outcome.incidents import __main__ as incidents
from membench.outcome.isolation import REPO_ROOT, child_env, fresh_vault, lay_instruction_block

# Each test starts the product in several fresh interpreters, which takes about a minute on a busy machine.
pytestmark = pytest.mark.timeout(180)

SUPERSEDE = """
import sys
from pathlib import Path
from exomem import commands
from exomem.governance.principal import owner_principal, request_scope
vault, old, content = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
with request_scope(owner_principal(surface="mcp")):
    commands.op_replace(vault, old, content=content, note_type="insight", title="Replacement")
"""
REPLACEMENT = "## Findings\n\n- [finding] A different step.\n"


def seeded_vault(root: Path, arm: Path = REPO_ROOT) -> tuple[dict, Path]:
    """A fresh vault under ``root`` with the first case's note, written by ``arm``'s own product code."""
    case = next(case for case in incidents.load_cases(None) if "seed" in case)
    fresh_vault(root, arm)
    work = root / "work"
    work.mkdir()
    seeded = incidents.run_arm_side(
        arm, root, "seed", str(root / "vault"), str(work), stdin=json.dumps(case["seed"])
    )
    return seeded, work


def probe(root: Path, seeded: dict) -> dict:
    return incidents.run_arm_side(REPO_ROOT, root, "probe", str(root / "vault"), seeded["path"])


def test_a_superseded_note_counts_as_corrected_and_an_untouched_one_does_not(tmp_path):
    seeded, _ = seeded_vault(tmp_path)
    assert incidents.seed_corrected(seeded, probe(tmp_path, seeded)) is False

    subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, "-c", SUPERSEDE, str(tmp_path / "vault"), seeded["path"], REPLACEMENT],
        env=child_env(tmp_path, REPO_ROOT),
        check=True,
        capture_output=True,
    )
    after = probe(tmp_path, seeded)
    assert after["body"] == seeded["body"]
    assert after["lifecycle_class"] == "superseded"
    assert incidents.seed_corrected(seeded, after) is True


def test_each_arm_writes_its_own_learning_instruction(tmp_path):
    """An arm whose guidance differs from the runner's checkout must get its own text in CLAUDE.md."""
    package = tmp_path / "arm" / "src" / "exomem"
    package.mkdir(parents=True)
    source = REPO_ROOT / "src" / "exomem"
    for entry in source.iterdir():
        if entry.name not in {"prominence.py", "__pycache__"}:
            (package / entry.name).symlink_to(entry)
    (package / "prominence.py").write_text(
        (source / "prominence.py").read_text(encoding="utf-8")
        + '\nLEARNING_INSTRUCTION = "ARM-OWN-TEXT"\n',
        encoding="utf-8",
    )
    _, work = seeded_vault(tmp_path / "session", tmp_path / "arm")
    assert "ARM-OWN-TEXT" in (work / "CLAUDE.md").read_text(encoding="utf-8")


def test_the_instruction_block_is_read_from_the_arm_checkout(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "prominence.md").write_text("### Balanced\n\n```\nARM-OWN-BLOCK\n```\n")
    lay_instruction_block(tmp_path / "block.txt", tmp_path)
    assert (tmp_path / "block.txt").read_text(encoding="utf-8") == "ARM-OWN-BLOCK\n"


@pytest.mark.parametrize(
    ("checks", "passed"),
    [
        ({"a": True, "b": True}, True),
        ({"a": True, "b": False}, False),
        ({"a": None, "b": True}, None),
        ({"a": None, "b": False}, False),
    ],
)
def test_an_unknown_check_is_not_a_failure(checks, passed):
    assert incidents.settle(checks) is passed


def test_a_case_that_would_pass_every_run_is_refused(tmp_path, monkeypatch):
    case = {
        "id": "empty-require",
        "pass": {"judge": {"schema": {"type": "object", "required": ["x"]}, "require": {}}},
    }
    (tmp_path / "bad.json").write_text(json.dumps({"cases": [case]}))
    monkeypatch.setattr(incidents, "CASES_DIR", tmp_path)
    with pytest.raises(SystemExit, match="non-empty"):
        incidents.load_cases(None)


def test_an_unknown_case_id_is_refused():
    with pytest.raises(SystemExit, match="no case with id"):
        incidents.load_cases(["no-such-case"])


def test_load_cases_loads_the_handed_file_custody_cases():
    cases = incidents.load_cases(["repository-file-to-fix", "named-file-to-ignore"])
    assert [case["id"] for case in cases] == ["repository-file-to-fix", "named-file-to-ignore"]


@pytest.mark.parametrize(
    ("patch", "match"),
    [
        ({"git": "yes"}, "`git` must be true or false"),
        ({"pass": {"sources_added": {"equals": "0"}}}, "sources_added needs an integer `equals`"),
    ],
)
def test_validate_case_refuses_a_bad_git_or_sources_added(patch, match):
    case = {"id": "bad", "prompt": "turn", "pass": {"called_any": ["search_memory"]}, **patch}
    with pytest.raises(SystemExit, match=match):
        incidents.validate_case(case)


def test_git_setup_commits_the_case_files_with_no_global_config(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    (work / "retry_settings.py").write_text("RETRY_DELAY = 5\n", encoding="utf-8")
    (work / "notes.txt").write_text("a note\n", encoding="utf-8")
    broken = tmp_path / "broken.gitconfig"
    broken.write_text(
        "[user]\nname = WRONG\nemail = wrong@invalid\n[core]\nhooksPath = /nonexistent-hooks\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(broken))
    incidents.init_repo(work)
    files = subprocess.run(
        ["git", "ls-files"], cwd=work, capture_output=True, text=True, check=True
    ).stdout.split()
    assert sorted(files) == ["notes.txt", "retry_settings.py"]
    author = subprocess.run(
        ["git", "log", "-1", "--format=%an <%ae>"],
        cwd=work,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert author == "bench <bench@invalid>"


def test_sources_added_counts_only_the_session_sources(monkeypatch, tmp_path):
    case = {"id": "added", "prompt": "turn", "pass": {"sources_added": {"equals": 1}}}
    record = {
        "sources_before": ["Knowledge Base/Sources/before.md"],
        "fixtures": {},
        "transport": "stdio",
    }
    done = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    monkeypatch.setattr(incidents, "play", lambda *args, **kwargs: (None, done, dict(record)))
    facts = {
        "sources": [
            {"path": "Knowledge Base/Sources/before.md", "frontmatter": {}},
            {"path": "Knowledge Base/Sources/new.md", "frontmatter": {}},
        ],
        "citing": [],
    }
    monkeypatch.setattr(incidents, "run_arm_side", lambda *args, **kwargs: facts)
    monkeypatch.setattr(incidents, "fault_reason", lambda *args: None)
    monkeypatch.setattr(incidents, "init_faults", lambda *args, **kwargs: [])
    result = incidents.run_unit(
        case, tmp_path, tmp_path / "unit", judge=None, model="m", executable="e", max_turns=1
    )
    assert result["checks"]["sources_added"] is True
    assert result["passed"] is True
