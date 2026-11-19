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
from membench.outcome.isolation import (
    REPO_ROOT,
    child_env,
    fresh_vault,
    lay_instruction_block,
    mcp_server,
)

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


def seeded_vault(
    root: Path, arm: Path = REPO_ROOT, arm_env: dict[str, str] | None = None
) -> tuple[dict, Path]:
    """A fresh vault under ``root`` with the note of a case that seeds one, written by ``arm``'s own product code."""
    case = incidents.load_cases(["capture-reported-failure"])[0]
    fresh_vault(root, arm, arm_env)
    work = root / "work"
    work.mkdir()
    seeded = incidents.run_arm_side(
        arm,
        root,
        "seed",
        str(root / "vault"),
        str(work),
        stdin=json.dumps(case["seed"]),
        arm_env=arm_env,
    )
    return seeded, work


def arm_checkout(tmp_path: Path, learning_instruction: str) -> Path:
    """A checkout of this product whose own guidance text is ``learning_instruction`` (a Python expression)."""
    package = tmp_path / "arm" / "src" / "exomem"
    package.mkdir(parents=True)
    source = REPO_ROOT / "src" / "exomem"
    for entry in source.iterdir():
        if entry.name not in {"prominence.py", "__pycache__"}:
            (package / entry.name).symlink_to(entry)
    (package / "prominence.py").write_text(
        (source / "prominence.py").read_text(encoding="utf-8")
        + f"\nimport os\nLEARNING_INSTRUCTION = {learning_instruction}\n",
        encoding="utf-8",
    )
    return tmp_path / "arm"


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
    arm = arm_checkout(tmp_path, '"ARM-OWN-TEXT"')
    _, work = seeded_vault(tmp_path / "session", arm)
    assert "ARM-OWN-TEXT" in (work / "CLAUDE.md").read_text(encoding="utf-8")


def test_a_list_seed_writes_every_page_the_case_names(tmp_path):
    """A case that needs a vault of linked pages must find each of them there, not only the first or last."""
    # The writer asks a later page for a qualifying link, so each page after the first links back, or
    # carries the reviewed outcome that no truthful relation exists (a distractor that links nothing).
    findings = {
        "Hub page": "A hub.",
        "Spoke page": "See [[Hub page]].",
        "Other spoke": "See [[Spoke page]].",
        "Unrelated page": "Nothing here relates to the others.",
    }
    pages = [
        {"title": title, "note_type": "insight", "content": f"## Findings\n\n- [finding] {line}\n"}
        for title, line in findings.items()
    ]
    pages[-1] |= {
        "relation_disposition": "reviewed_none",
        "relation_review_reason": "The page is a distractor with no truthful relation to the others.",
    }
    vault, work = fresh_vault(tmp_path, REPO_ROOT), tmp_path / "work"
    work.mkdir()
    seeded = incidents.run_arm_side(
        REPO_ROOT, tmp_path, "seed", str(vault), str(work), stdin=json.dumps(pages)
    )
    assert [page["exists"] for page in seeded] == [True] * len(findings)
    assert [Path(page["path"]).stem for page in seeded] == list(findings)


def test_an_arm_env_setting_reaches_its_own_arm_and_no_other(tmp_path, monkeypatch):
    """Two arms may share a checkout. The setting belongs to one of them, whatever the runner itself inherited."""
    arm = arm_checkout(tmp_path, 'os.environ.get("EXOMEM_DISABLE_GRAPH_INDEX", "unset")')
    monkeypatch.setenv("EXOMEM_DISABLE_GRAPH_INDEX", "1")
    arm_env = incidents.parse_arm_env(
        ["graph_off=EXOMEM_DISABLE_GRAPH_INDEX=1"], {"changed": arm, "graph_off": arm}
    )
    seen = {}
    for name in ("changed", "graph_off"):
        _, work = seeded_vault(tmp_path / name, arm, arm_env[name])
        seen[name] = (work / "CLAUDE.md").read_text(encoding="utf-8")
        server = mcp_server(tmp_path / name, arm, arm_env[name])["env"]
        assert server.get("EXOMEM_DISABLE_GRAPH_INDEX") == arm_env[name].get(
            "EXOMEM_DISABLE_GRAPH_INDEX"
        )
    assert "unset" in seen["changed"]
    assert "unset" not in seen["graph_off"]


@pytest.mark.parametrize(
    "item",
    [
        "graph_of=EXOMEM_DISABLE_GRAPH_INDEX=1",
        "graph_off=PATH=/tmp",
        "graph_off=EXOMEM_DISABLE_GRAPH_INDEX",
        "graph_off=EXOMEM_STATE_ROOT=/elsewhere",
    ],
)
def test_an_arm_env_that_would_leave_an_arm_unswitched_or_unisolated_is_refused(item):
    """A misspelt arm would run identical to the changed arm and a pinned key would leave the unit's state."""
    with pytest.raises(SystemExit, match="--arm-env"):
        incidents.parse_arm_env([item], {"changed": REPO_ROOT, "graph_off": REPO_ROOT})


@pytest.mark.parametrize(
    "seed",
    [
        [],
        [{"title": "Only", "note_type": "insight"}],
        ["not a note"],
        {
            "title": "T",
            "note_type": "insight",
            "content": "c",
            "relation_disposition": "reviewed_none",
        },
        [{"title": "T", "note_type": "insight", "content": "c", "relation_review_reason": "why"}],
    ],
)
def test_a_seed_the_arm_could_not_write_is_refused_before_a_session_starts(seed):
    case = {"id": "bad-seed", "seed": seed, "pass": {"called_any": ["recall_context"]}}
    with pytest.raises(SystemExit, match="bad-seed"):
        incidents.validate_case(case)


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
