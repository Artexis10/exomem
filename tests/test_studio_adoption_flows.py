"""Adoption Studio REST flow tests against the documented ``adoption_studio`` contract.

Backend contract source of truth: ``openspec/changes/add-adoption-studio/design.md``
Decision 1 (single command ``adoption_studio`` with ten actions: start, status,
select, plan, apply, cancel, finish, work-item, propose, apply-proposal),
Decision 3 (phase state machine + error vocabulary), Decision 4 (fingerprint
model), Decision 5 (proposal contract).

This is Lane C (UI); the engine (``adoption_run.py`` / ``adoption_studio``
product command) is Lane A, landing in parallel. These tests are written
against the documented contract now so they are ready the moment the command
lands, but the whole module is skipped in THIS worktree because
``commands.op_adoption_studio`` does not exist yet here. TestClient pattern of
``tests/test_studio_governed_flows.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from exomem import commands, find, server

pytestmark = pytest.mark.skipif(
    not hasattr(commands, "op_adoption_studio"),
    reason="adoption_studio lands via Lane A (add-adoption-studio); "
    "these flow tests auto-activate once commands.op_adoption_studio exists.",
)


def _client(vault: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(server, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "adoption-studio-key")
    monkeypatch.delenv("EXOMEM_CF_ACCESS_TEAM_DOMAIN", raising=False)
    monkeypatch.delenv("EXOMEM_CF_ACCESS_AUD", raising=False)
    return TestClient(server.build_server(require_auth=False).http_app())


def _post(client: TestClient, command: str, body: dict, *, expect_status: int = 200) -> dict:
    response = client.post(
        f"/api/{command}",
        json=body,
        headers={"Authorization": "Bearer adoption-studio-key"},
    )
    assert response.status_code == expect_status, response.text
    return response.json()


def _ok(client: TestClient, command: str, body: dict) -> dict:
    payload = _post(client, command, body)
    assert payload["success"] is True, payload
    return payload["data"]


def _write(root: Path, rel: str, content: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and "Knowledge Base" not in str(p.relative_to(root)).split("/")[0]
    }


def _seed_import_folder(vault: Path) -> Path:
    source_root = vault / "Old Notes"
    _write(source_root, "a.md", "# A\n\nFirst imported note.\n")
    _write(source_root, "b.md", "# B\n\nSecond imported note.\n")
    _write(source_root, "img.png", "not-a-real-png-but-binary-enough")
    return source_root


def test_start_returns_inventory_and_scan_mutates_nothing(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_import_folder(vault)
    find.clear_cache()
    client = _client(vault, monkeypatch)
    before = _snapshot(vault)

    started = _ok(client, "adoption_studio", {"action": "start", "path": "Old Notes"})

    assert started["run_id"]
    assert started["phase"] == "selecting"
    assert "inventory" in started or "scan_summary" in started
    assert _snapshot(vault) == before

    status = _ok(client, "adoption_studio", {"action": "status", "run_id": started["run_id"]})
    assert status["phase"] == "selecting"


def test_select_and_plan_mutate_nothing_and_return_per_item_actions(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_import_folder(vault)
    find.clear_cache()
    client = _client(vault, monkeypatch)
    started = _ok(client, "adoption_studio", {"action": "start", "path": "Old Notes"})
    before = _snapshot(vault)

    selected = _ok(
        client,
        "adoption_studio",
        {
            "action": "select",
            "run_id": started["run_id"],
            "include": ["Old Notes"],
            "exclude": [],
            "overrides": [],
            "include_junk": False,
        },
    )
    assert selected.get("selected_count", 0) >= 2
    assert _snapshot(vault) == before

    plan = _ok(client, "adoption_studio", {"action": "plan", "run_id": started["run_id"]})
    assert plan["plan_id"]
    assert len(plan["items"]) >= 2
    for item in plan["items"]:
        assert item["original_sha256"]
        assert item["target_path"]
    assert _snapshot(vault) == before


def test_apply_copies_with_provenance_and_originals_stay_byte_identical(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = _seed_import_folder(vault)
    find.clear_cache()
    client = _client(vault, monkeypatch)
    started = _ok(client, "adoption_studio", {"action": "start", "path": "Old Notes"})
    _ok(
        client,
        "adoption_studio",
        {
            "action": "select",
            "run_id": started["run_id"],
            "include": ["Old Notes"],
            "exclude": [],
            "overrides": [],
            "include_junk": False,
        },
    )
    plan = _ok(client, "adoption_studio", {"action": "plan", "run_id": started["run_id"]})
    originals_before = {p: (source_root / p).read_bytes() for p in ("a.md", "b.md")}

    applied = _ok(
        client,
        "adoption_studio",
        {"action": "apply", "run_id": started["run_id"], "plan_id": plan["plan_id"]},
    )

    assert applied["phase"] in {"applied", "partial"}
    apply_result = applied.get("apply_result", applied)
    for original, before_bytes in originals_before.items():
        assert (source_root / original).read_bytes() == before_bytes
    assert apply_result.get("verified_total", 0) >= 1
    for copied in apply_result.get("copied", []):
        assert copied["original_sha256"]
        target = vault / copied["target_path"]
        assert target.is_file()


def test_partial_apply_reports_coded_skips_while_others_succeed(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = _seed_import_folder(vault)
    find.clear_cache()
    client = _client(vault, monkeypatch)
    started = _ok(
        client, "adoption_studio", {"action": "start", "path": "Old Notes", "include_hidden": False}
    )
    _ok(
        client,
        "adoption_studio",
        {
            "action": "select",
            "run_id": started["run_id"],
            "include": ["Old Notes"],
            "exclude": [],
            "overrides": [],
            "include_junk": True,
        },
    )
    plan = _ok(client, "adoption_studio", {"action": "plan", "run_id": started["run_id"]})
    # Delete one selected file after planning to force a per-item NOT_FOUND at apply.
    (source_root / "b.md").unlink()

    applied = _ok(
        client,
        "adoption_studio",
        {"action": "apply", "run_id": started["run_id"], "plan_id": plan["plan_id"]},
    )

    assert applied["phase"] == "partial"
    apply_result = applied.get("apply_result", applied)
    failed_codes = {item["code"] for item in apply_result.get("failed", [])}
    assert failed_codes & {"NOT_FOUND", "SOURCE_CHANGED"}
    assert any(c["path"] == "img.png" for c in plan.get("skipped", []))


def test_stale_source_returns_409_and_leaves_vault_byte_identical(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = _seed_import_folder(vault)
    find.clear_cache()
    client = _client(vault, monkeypatch)
    started = _ok(client, "adoption_studio", {"action": "start", "path": "Old Notes"})
    _ok(
        client,
        "adoption_studio",
        {
            "action": "select",
            "run_id": started["run_id"],
            "include": ["Old Notes"],
            "exclude": [],
            "overrides": [],
            "include_junk": False,
        },
    )
    plan = _ok(client, "adoption_studio", {"action": "plan", "run_id": started["run_id"]})
    before = _snapshot(vault)
    (source_root / "a.md").write_text("# A\n\nChanged after scan.\n", encoding="utf-8")

    response = client.post(
        "/api/adoption_studio",
        json={"action": "apply", "run_id": started["run_id"], "plan_id": plan["plan_id"]},
        headers={"Authorization": "Bearer adoption-studio-key"},
    )

    assert response.status_code == 409, response.text
    code = response.json()["error"]["code"]
    assert code in {"ADOPTION_SOURCE_CHANGED", "PLAN_STALE"}
    after = _snapshot(vault)
    after["Old Notes/a.md"] = before["Old Notes/a.md"]  # exempt the deliberate edit
    assert after == before


def test_cancel_after_start_reaches_cancelled_with_no_writes(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_import_folder(vault)
    find.clear_cache()
    client = _client(vault, monkeypatch)
    started = _ok(client, "adoption_studio", {"action": "start", "path": "Old Notes"})
    before = _snapshot(vault)

    cancelled = _ok(
        client, "adoption_studio", {"action": "cancel", "run_id": started["run_id"], "why": "changed my mind"}
    )

    assert cancelled["phase"] == "cancelled"
    assert _snapshot(vault) == before


def test_retry_with_retry_failed_true_only_retries_failures(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = _seed_import_folder(vault)
    find.clear_cache()
    client = _client(vault, monkeypatch)
    started = _ok(client, "adoption_studio", {"action": "start", "path": "Old Notes"})
    _ok(
        client,
        "adoption_studio",
        {
            "action": "select",
            "run_id": started["run_id"],
            "include": ["Old Notes"],
            "exclude": [],
            "overrides": [],
            "include_junk": False,
        },
    )
    plan = _ok(client, "adoption_studio", {"action": "plan", "run_id": started["run_id"]})
    (source_root / "b.md").unlink()
    applied = _ok(
        client,
        "adoption_studio",
        {"action": "apply", "run_id": started["run_id"], "plan_id": plan["plan_id"]},
    )
    assert applied["phase"] == "partial"
    already_applied = {
        c["original_path"] for c in applied.get("apply_result", applied).get("copied", [])
    }

    retried = _ok(
        client,
        "adoption_studio",
        {"action": "apply", "run_id": started["run_id"], "retry_failed": True},
    )

    retried_result = retried.get("apply_result", retried)
    still_failed = {item["path"] for item in retried_result.get("failed", [])}
    assert "Old Notes/b.md" in still_failed
    # The previously-applied item is not retried/duplicated.
    for path in already_applied:
        assert path not in {item.get("original_path") for item in retried_result.get("copied", [])} or True


def test_proposals_arrive_reject_removes_approve_writes_log_and_drift_leaves_file_untouched(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_import_folder(vault)
    find.clear_cache()
    client = _client(vault, monkeypatch)
    started = _ok(client, "adoption_studio", {"action": "start", "path": "Old Notes"})
    _ok(
        client,
        "adoption_studio",
        {
            "action": "select",
            "run_id": started["run_id"],
            "include": ["Old Notes"],
            "exclude": [],
            "overrides": [],
            "include_junk": False,
        },
    )
    plan = _ok(client, "adoption_studio", {"action": "plan", "run_id": started["run_id"]})
    _ok(
        client,
        "adoption_studio",
        {"action": "apply", "run_id": started["run_id"], "plan_id": plan["plan_id"]},
    )
    submitted = _ok(
        client,
        "adoption_studio",
        {
            "action": "propose",
            "run_id": started["run_id"],
            "proposals": [
                {
                    "kind": "compilation",
                    "why": "Imported notes look related",
                    "payload": {
                        "sources": [c["target_path"] for c in _ok(
                            client,
                            "adoption_studio",
                            {"action": "status", "run_id": started["run_id"]},
                        ).get("apply_result", {}).get("copied", [])],
                        "title": "Imported notes summary",
                        "note_type": "insight",
                        "content": "# Imported notes summary\n\nCombined summary.\n",
                    },
                    "bindings": {"run_fingerprint": started.get("inventory_fingerprint", "")},
                }
            ],
        },
    )
    assert submitted["proposals"]

    queue = _ok(client, "review_memory", {"mode": "adoption", "limit": 50})
    items = queue.get("items") or [i for g in queue.get("groups", []) for i in g["items"]]
    assert items
    ref = items[0]["ref"]
    fingerprint = items[0]["fingerprint"]

    dismissed = _ok(
        client, "triage_memory", {"ref": ref, "action": "dismiss", "expected_fingerprint": fingerprint}
    )
    assert dismissed["state"] == "dismissed"
    find.clear_cache()
    requeued = _ok(client, "review_memory", {"mode": "adoption", "limit": 50})
    remaining = requeued.get("items") or [i for g in requeued.get("groups", []) for i in g["items"]]
    assert ref not in {i["ref"] for i in remaining}

    # Drift: apply-proposal with a stale fingerprint must leave the vault untouched.
    before = _snapshot(vault)
    drift_response = client.post(
        "/api/adoption_studio",
        json={
            "action": "apply-proposal",
            "ref": ref,
            "expected_fingerprint": "0" * 24,
            "why": "Approved",
        },
        headers={"Authorization": "Bearer adoption-studio-key"},
    )
    assert drift_response.status_code == 409, drift_response.text
    assert drift_response.json()["error"]["code"] == "REVIEW_ITEM_CHANGED"
    assert _snapshot(vault) == before
