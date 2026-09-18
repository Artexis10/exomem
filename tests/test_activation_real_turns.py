"""Tests for the local-only real-turn acceptance script (task 9).

Loaded by path, matching ``tests/test_private_vault_snapshot.py``'s
convention for a ``scripts/`` module that is not on ``sys.path``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "activation_real_turns.py"


def _module():
    spec = importlib.util.spec_from_file_location("activation_real_turns", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _seed_vault(vault: Path) -> None:
    kb = vault / "Knowledge Base"
    (kb / "Products").mkdir(parents=True, exist_ok=True)
    (kb / "Products" / "widget.md").write_text(
        "---\ntitle: Widget\nstatus: active\nupdated: 2026-09-01\n---\n\n"
        "# Widget\n\n## Summary\n\nA small generic device kept on the shelf.\n",
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def _isolated_env():
    # `prepare_environment` mutates `os.environ` directly (it is meant to
    # affect the one resident process it runs in) rather than through
    # `monkeypatch`, so this fixture saves and restores the whole environment
    # itself -- otherwise a test's own `EXOMEM_VAULT_PATH`/`XDG_STATE_HOME`
    # would leak into every test that runs after it, in this file or another.
    before = dict(os.environ)
    # Pollute the environment the way an interactive shell would, so
    # `prepare_environment` has something real to strip.
    os.environ["EXOMEM_LOG_DIR"] = "/somewhere/unrelated"
    os.environ["EXOMEM_KB_DIRNAME"] = "Some Other Folder"
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(before)


# --------------------------------------------------------------------------- #
# Path refusal (N4 guard, reused)
# --------------------------------------------------------------------------- #


def test_refuses_a_snapshot_path_inside_a_git_checkout(tmp_path: Path) -> None:
    module = _module()
    fake_repo = tmp_path / "fake-repo"
    (fake_repo / ".git").mkdir(parents=True)
    with pytest.raises(module.SnapshotError):
        module.refuse_path_inside_a_repository(fake_repo / "snapshot", label="--snapshot")


def test_refuses_a_turns_path_inside_a_git_checkout(tmp_path: Path) -> None:
    module = _module()
    fake_repo = tmp_path / "fake-repo"
    (fake_repo / ".git").mkdir(parents=True)
    with pytest.raises(module.SnapshotError):
        module.refuse_path_inside_a_repository(fake_repo / "turns.json", label="--turns")


def test_accepts_paths_outside_any_checkout(tmp_path: Path) -> None:
    module = _module()
    resolved = module.refuse_path_inside_a_repository(tmp_path / "snapshot", label="--snapshot")
    assert resolved == (tmp_path / "snapshot").resolve()


# --------------------------------------------------------------------------- #
# Environment isolation
# --------------------------------------------------------------------------- #


def test_prepare_environment_strips_unrelated_exomem_vars_and_sets_vault_path(
    tmp_path: Path,
) -> None:
    module = _module()
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()

    module.prepare_environment(snapshot, state_dir=None)

    assert "EXOMEM_LOG_DIR" not in os.environ
    assert "EXOMEM_KB_DIRNAME" not in os.environ
    assert os.environ["EXOMEM_VAULT_PATH"] == str(snapshot)


def test_prepare_environment_keeps_the_embeddings_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()

    module.prepare_environment(snapshot, state_dir=None)

    assert os.environ["EXOMEM_DISABLE_EMBEDDINGS"] == "1"


def test_prepare_environment_defaults_state_dir_under_the_snapshots_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    snapshot = tmp_path / "vault-snap"
    snapshot.mkdir()

    module.prepare_environment(snapshot, state_dir=None)

    assert os.environ["XDG_STATE_HOME"] == str(tmp_path / "vault-snap-activation-state")


def test_prepare_environment_honours_an_explicit_state_dir(tmp_path: Path) -> None:
    module = _module()
    snapshot = tmp_path / "vault-snap"
    snapshot.mkdir()
    explicit = str(tmp_path / "explicit-state")

    module.prepare_environment(snapshot, state_dir=explicit)

    assert os.environ["XDG_STATE_HOME"] == explicit


def test_prepare_environment_ignores_an_inherited_xdg_state_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review round 3, MINOR 8: an inherited `XDG_STATE_HOME` (a machine that
    exports one for its live, real vault) must never be honoured -- this
    script always writes the snapshot's sidecars under the snapshot's own
    parent unless `--state-dir` is given explicitly.
    """
    module = _module()
    live_state_root = str(tmp_path / "live-state-root")
    monkeypatch.setenv("XDG_STATE_HOME", live_state_root)
    snapshot = tmp_path / "vault-snap"
    snapshot.mkdir()

    module.prepare_environment(snapshot, state_dir=None)

    assert os.environ["XDG_STATE_HOME"] != live_state_root
    assert os.environ["XDG_STATE_HOME"] == str(tmp_path / "vault-snap-activation-state")


# --------------------------------------------------------------------------- #
# Turns file
# --------------------------------------------------------------------------- #


def test_load_turns_reads_a_well_formed_file(tmp_path: Path) -> None:
    module = _module()
    path = tmp_path / "turns.json"
    path.write_text(json.dumps([{"turn": "hello"}, {"turn": "world", "expect_abstain": True}]), encoding="utf-8")

    turns = module.load_turns(path)

    assert [t["turn"] for t in turns] == ["hello", "world"]


def test_load_turns_refuses_a_non_list_file(tmp_path: Path) -> None:
    module = _module()
    path = tmp_path / "turns.json"
    path.write_text(json.dumps({"turn": "hello"}), encoding="utf-8")

    with pytest.raises(module.TurnsFileError):
        module.load_turns(path)


def test_load_turns_refuses_an_entry_with_no_turn(tmp_path: Path) -> None:
    module = _module()
    path = tmp_path / "turns.json"
    path.write_text(json.dumps([{"expect_abstain": True}]), encoding="utf-8")

    with pytest.raises(module.TurnsFileError):
        module.load_turns(path)


# --------------------------------------------------------------------------- #
# Running turns against a real vault
# --------------------------------------------------------------------------- #


def test_run_turn_passes_when_the_expected_anchor_resolves(tmp_path: Path) -> None:
    module = _module()
    vault = tmp_path / "vault"
    _seed_vault(vault)
    module.prepare_environment(vault, state_dir=str(tmp_path / "state"))

    result = module.run_turn(vault, {"turn": "what about the widget", "expect": ["Knowledge Base/Products/widget.md"]})

    assert result["judged"] is True
    assert result["passed"] is True
    assert result["missing"] == []


def test_run_turn_fails_when_the_expected_anchor_is_missing(tmp_path: Path) -> None:
    module = _module()
    vault = tmp_path / "vault"
    _seed_vault(vault)
    module.prepare_environment(vault, state_dir=str(tmp_path / "state"))

    result = module.run_turn(
        vault, {"turn": "completely unrelated gibberish", "expect": ["Knowledge Base/Products/widget.md"]}
    )

    assert result["judged"] is True
    assert result["passed"] is False
    assert result["missing"] == ["Knowledge Base/Products/widget.md"]


def test_run_turn_passes_when_an_unresolved_turn_is_expected_to_abstain(tmp_path: Path) -> None:
    module = _module()
    vault = tmp_path / "vault"
    _seed_vault(vault)
    module.prepare_environment(vault, state_dir=str(tmp_path / "state"))

    result = module.run_turn(vault, {"turn": "completely unrelated gibberish", "expect_abstain": True})

    assert result["judged"] is True
    assert result["passed"] is True


def test_run_turn_is_unjudged_with_neither_expectation(tmp_path: Path) -> None:
    module = _module()
    vault = tmp_path / "vault"
    _seed_vault(vault)
    module.prepare_environment(vault, state_dir=str(tmp_path / "state"))

    result = module.run_turn(vault, {"turn": "what about the widget"})

    assert result["judged"] is False
    assert result["passed"] is None


def _seed_ambiguous_vault(vault: Path) -> None:
    """Two hub anchors, named exactly by one turn, with disjoint
    neighbourhoods -- a packet that must come back `ambiguous`, never
    `unresolved`.
    """
    kb = vault / "Knowledge Base"
    (kb / "Notes" / "Insights").mkdir(parents=True, exist_ok=True)
    (kb / "Notes" / "Insights" / "north-hub.md").write_text(
        "---\ntitle: Northern Programme\nstatus: active\ntags: [hub]\nupdated: 2026-09-01\n---\n\n"
        "# Northern Programme\n\nA coordination hub. See [[Notes/Insights/north-note]].\n",
        encoding="utf-8",
    )
    (kb / "Notes" / "Insights" / "north-note.md").write_text(
        "---\ntitle: North note\nstatus: active\nupdated: 2026-09-01\n---\n\n# North note\n\nSupporting material.\n",
        encoding="utf-8",
    )
    (kb / "Notes" / "Insights" / "south-hub.md").write_text(
        "---\ntitle: Southern Venture\nstatus: active\ntags: [hub]\nupdated: 2026-09-01\n---\n\n"
        "# Southern Venture\n\nA coordination hub. See [[Notes/Insights/south-note]].\n",
        encoding="utf-8",
    )
    (kb / "Notes" / "Insights" / "south-note.md").write_text(
        "---\ntitle: South note\nstatus: active\nupdated: 2026-09-01\n---\n\n# South note\n\nSupporting material.\n",
        encoding="utf-8",
    )


def test_run_turn_labels_an_ambiguous_packet_ambiguous_not_unresolved(tmp_path: Path) -> None:
    """Review round 3, MINOR 9: the packet's own `abstained` field is true for
    BOTH `unresolved` and `ambiguous` -- checking it first made the
    `ambiguous` branch unreachable.
    """
    module = _module()
    vault = tmp_path / "vault"
    _seed_ambiguous_vault(vault)
    module.prepare_environment(vault, state_dir=str(tmp_path / "state"))

    result = module.run_turn(
        vault, {"turn": "compare the Northern Programme and the Southern Venture"}
    )

    assert result["status"] == "ambiguous"
    assert result["abstained"] is True


# --------------------------------------------------------------------------- #
# main()
# --------------------------------------------------------------------------- #


def test_main_refuses_a_snapshot_inside_a_repository(tmp_path: Path, capsys) -> None:
    module = _module()
    fake_repo = tmp_path / "fake-repo"
    (fake_repo / ".git").mkdir(parents=True)
    turns_path = tmp_path / "turns.json"
    turns_path.write_text(json.dumps([{"turn": "hello", "expect_abstain": True}]), encoding="utf-8")

    exit_code = module.main(["--snapshot", str(fake_repo), "--turns", str(turns_path)])

    assert exit_code == 2
    assert "git checkout" in capsys.readouterr().err


def test_main_exits_zero_when_every_judged_turn_passes(tmp_path: Path, capsys) -> None:
    module = _module()
    vault = tmp_path / "vault"
    _seed_vault(vault)
    turns_path = tmp_path / "turns.json"
    turns_path.write_text(
        json.dumps([{"turn": "what about the widget", "expect": ["Knowledge Base/Products/widget.md"]}]),
        encoding="utf-8",
    )

    exit_code = module.main(
        ["--snapshot", str(vault), "--turns", str(turns_path), "--state-dir", str(tmp_path / "state")]
    )

    assert exit_code == 0
    assert "PASS" in capsys.readouterr().out


def test_main_exits_nonzero_when_a_judged_turn_fails(tmp_path: Path, capsys) -> None:
    module = _module()
    vault = tmp_path / "vault"
    _seed_vault(vault)
    turns_path = tmp_path / "turns.json"
    turns_path.write_text(
        json.dumps(
            [{"turn": "completely unrelated gibberish", "expect": ["Knowledge Base/Products/widget.md"]}]
        ),
        encoding="utf-8",
    )

    exit_code = module.main(
        ["--snapshot", str(vault), "--turns", str(turns_path), "--state-dir", str(tmp_path / "state")]
    )

    assert exit_code == 1
    assert "FAIL" in capsys.readouterr().out


def test_main_json_mode_emits_parseable_json(tmp_path: Path, capsys) -> None:
    module = _module()
    vault = tmp_path / "vault"
    _seed_vault(vault)
    turns_path = tmp_path / "turns.json"
    turns_path.write_text(json.dumps([{"turn": "what about the widget"}]), encoding="utf-8")

    exit_code = module.main(
        ["--snapshot", str(vault), "--turns", str(turns_path), "--json", "--state-dir", str(tmp_path / "state")]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["turn"] == "what about the widget"
