"""Adoption Studio command-surface registration and REST journey (Lane A)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from exomem import commands, find, server

REPO_ROOT = Path(__file__).resolve().parents[1]
GOLDEN_PATH = REPO_ROOT / "tests" / "fixtures" / "mcp_tool_schemas.json"


def _adoption_command() -> commands.Command:
    for cmd in commands.PRODUCT_COMMANDS:
        if cmd.name == "adoption_studio":
            return cmd
    raise AssertionError("adoption_studio not registered in PRODUCT_COMMANDS")


def _seed_legacy(vault: Path) -> None:
    old = vault / "Old Notes"
    old.mkdir(parents=True, exist_ok=True)
    (old / "quarterly-planning.md").write_text(
        "# Quarterly Planning Notes\n\nShip the adoption studio this quarter.\n",
        encoding="utf-8",
    )
    (old / "standup.txt").write_text("standup: nothing blocking\n", encoding="utf-8")
    find.clear_cache()


def _client(vault: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(server, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "studio-key")
    monkeypatch.delenv("EXOMEM_CF_ACCESS_TEAM_DOMAIN", raising=False)
    monkeypatch.delenv("EXOMEM_CF_ACCESS_AUD", raising=False)
    return TestClient(server.build_server(require_auth=False).http_app())


def _post(client: TestClient, command: str, body: dict) -> dict:
    response = client.post(
        f"/api/{command}",
        json=body,
        headers={"Authorization": "Bearer studio-key"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["success"] is True, response.text
    return payload["data"]


# --- 25 ---
def test_registry_entry_and_route_validation() -> None:
    names = {c.name for c in commands.PRODUCT_COMMANDS}
    assert "adoption_studio" in names
    cmd = _adoption_command()
    assert cmd.routes == ("adopt",)
    assert cmd.first_run_safe is True
    assert cmd.cli_writes is True  # write-capable by default (mutating actions)
    # Route metadata references a canonical implementation leaf → validation passes.
    report = commands.validate_product_registry()
    assert "adoption_studio" in report["product_commands"]


# --- 26 ---
def test_read_only_action_classification() -> None:
    cmd = _adoption_command()
    read_only = {"status", "work-item"}
    for action in [
        "start",
        "status",
        "select",
        "plan",
        "apply",
        "cancel",
        "finish",
        "work-item",
        "propose",
        "apply-proposal",
    ]:
        result = commands.invocation_is_read_only(cmd, {"action": action})
        assert result is (action in read_only), action
    # An omitted action selector (no default) is treated as a mutation.
    assert commands.invocation_is_read_only(cmd, {}) is False


# --- 27 ---
def test_rest_facade_round_trip(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_legacy(vault)
    client = _client(vault, monkeypatch)
    original = (vault / "Old Notes/quarterly-planning.md").read_bytes()

    started = _post(client, "adoption_studio", {"action": "start", "path": "Old Notes"})
    run_id = started["run_id"]
    assert started["phase"] == "selecting"

    selected = _post(
        client,
        "adoption_studio",
        {"action": "select", "run_id": run_id, "include": ["Old Notes"]},
    )
    assert "Old Notes/quarterly-planning.md" in selected["selection"]["paths"]

    planned = _post(client, "adoption_studio", {"action": "plan", "run_id": run_id})
    plan_id = planned["plan"]["plan_id"]
    # Preview wrote no imported Source yet.
    assert not (vault / "Knowledge Base/Sources/Imported").exists()

    applied = _post(
        client,
        "adoption_studio",
        {"action": "apply", "run_id": run_id, "plan_id": plan_id},
    )
    assert applied["phase"] == "applied"
    assert applied["verified_unchanged"] == applied["verified_total"]
    # Originals byte-identical.
    assert (vault / "Old Notes/quarterly-planning.md").read_bytes() == original

    finished = _post(client, "adoption_studio", {"action": "finish", "run_id": run_id})
    assert finished["phase"] == "done"
    assert finished["finish"]["route"]["tool"] == "ask_memory"

    # Read-only status round-trips without a run_id (list) and with one (full doc).
    listed = _post(client, "adoption_studio", {"action": "status"})
    assert any(row["run_id"] == run_id for row in listed["runs"])

    # --- propose -> review -> apply-proposal (Lane B agent contract) ---
    imported_path = [
        o["target_path"] for o in applied["outcomes"].values() if o.get("status") == "applied"
    ][0]
    submitted = _post(
        client,
        "adoption_studio",
        {
            "action": "propose",
            "run_id": run_id,
            "proposals": [
                {
                    "kind": "compilation",
                    "why": "Imported note looks worth summarizing",
                    "payload": {
                        "sources": [imported_path],
                        "title": "Quarterly planning summary",
                        "note_type": "insight",
                        "content": "# Quarterly planning summary\n\nShip the studio.\n",
                    },
                    "bindings": {"run_fingerprint": started.get("inventory_fingerprint", "")},
                }
            ],
        },
    )
    assert submitted["proposals"][0]["status"] == "proposed"
    proposal_ref = submitted["proposals"][0]["ref"]
    proposal_fp = submitted["proposals"][0]["fingerprint"]
    assert proposal_ref.startswith("exomem://review/adoption/")

    queue = _post(client, "review_memory", {"mode": "adoption", "limit": 50})
    open_refs = {i["ref"] for group in queue["runs"] for i in group["items"]}
    assert proposal_ref in open_refs

    approved = _post(
        client,
        "adoption_studio",
        {
            "action": "apply-proposal",
            "ref": proposal_ref,
            "expected_fingerprint": proposal_fp,
            "why": "Looks correct, approving",
        },
    )
    assert approved["applied"] is True
    assert (vault / approved["result_path"]).is_file()

    # A resubmitted apply-proposal is now applied (idempotent), not re-applied.
    replayed = _post(
        client,
        "adoption_studio",
        {
            "action": "apply-proposal",
            "ref": proposal_ref,
            "expected_fingerprint": proposal_fp,
            "why": "Looks correct, approving",
        },
    )
    assert replayed.get("already_applied") is True


# --- 28 ---
def test_mutation_replay_is_idempotent(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_legacy(vault)
    started = commands.op_adoption_studio(vault, action="start", path="Old Notes")
    run_id = started["run_id"]
    commands.op_adoption_studio(vault, action="select", run_id=run_id, include=["Old Notes"])
    planned = commands.op_adoption_studio(vault, action="plan", run_id=run_id)
    plan_id = planned["plan"]["plan_id"]

    first = commands.op_adoption_studio(
        vault, action="apply", run_id=run_id, plan_id=plan_id
    )
    assert first["phase"] == "applied"
    imported_dir = vault / "Knowledge Base/Sources/Imported"
    after_first = sorted(p.name for p in imported_dir.iterdir())

    # Replaying the identical apply must not create a duplicate Source.
    second = commands.op_adoption_studio(
        vault, action="apply", run_id=run_id, plan_id=plan_id
    )
    assert sorted(p.name for p in imported_dir.iterdir()) == after_first
    assert all(
        outcome["status"] in {"applied", "already-applied"}
        for outcome in second["outcomes"].values()
    )


# --- 28b: unknown action is rejected ---
def test_unknown_action_is_rejected(vault: Path) -> None:
    with pytest.raises(ValueError) as ei:
        commands.op_adoption_studio(vault, action="frobnicate")
    assert "INVALID_MODE" in str(ei.value)


# --- 29 ---
def test_golden_fixture_names_adoption_studio() -> None:
    """The intentional golden regen must have landed adoption_studio."""
    baseline = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    assert "adoption_studio" in baseline
    schema = baseline["adoption_studio"]["inputSchema"]
    assert "action" in schema["properties"]
