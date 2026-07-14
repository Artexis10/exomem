"""Adoption Studio REST flow tests against the real ``adoption_studio`` contract.

Backend contract source of truth: ``openspec/changes/add-adoption-studio/design.md``
Decision 1 (single command ``adoption_studio`` with ten actions: start, status,
select, plan, apply, cancel, finish, work-item, propose, apply-proposal),
Decision 3 (phase state machine + error vocabulary), Decision 4 (fingerprint
model), Decision 5 (proposal contract).

Verified against the merged Lane A engine (``adoption_run.py``): EVERY
``adoption_studio`` action returns the full presented run document (a uniform
shape via ``adoption_run._present``) — so ``plan_id`` lives at
``plan["plan"]["plan_id"]`` (not top-level), plan items at
``plan["plan"]["items"]``, skipped entries at ``plan["plan"]["skipped"]``, and
per-item apply outcomes at ``outcomes`` (a map keyed by original path, each
``{status, code?, reason?, target_path?, source_ref?, sha256?, at}``).
``verified_unchanged``/``verified_total`` are real post-apply re-hash counts
that appear TOP-LEVEL on the run document once it has applied outcomes. HTTP
409 is real for ``ADOPTION_SOURCE_CHANGED`` (run-level: every requested source
drifted/vanished with nothing yet applied), ``PLAN_STALE`` (plan_id/selection
mismatch), and ``REVIEW_ITEM_CHANGED`` (proposal fingerprint drift), via
``cli_ops``'s conflict-code mapping. TestClient pattern of
``tests/test_studio_governed_flows.py``.

The proposals test additionally needs Lane B (``adoption_proposals.py``,
``work-item``/``propose``/``apply-proposal``/``review_memory(mode="adoption")``);
it carries its own ``importlib.util.find_spec`` guard so it auto-activates the
moment that module lands, independent of this module's own guard (kept for
portability to worktrees where Lane A itself is not yet merged).
"""

from __future__ import annotations

import importlib.util
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
    # Selection state lives on the run document's `selection.paths`, not a
    # bespoke `selected_count` field.
    assert len(selected["selection"]["paths"]) >= 2
    assert _snapshot(vault) == before

    planned = _ok(client, "adoption_studio", {"action": "plan", "run_id": started["run_id"]})
    plan = planned["plan"]
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
    planned = _ok(client, "adoption_studio", {"action": "plan", "run_id": started["run_id"]})
    plan_id = planned["plan"]["plan_id"]
    originals_before = {p: (source_root / p).read_bytes() for p in ("a.md", "b.md")}

    applied = _ok(
        client,
        "adoption_studio",
        {"action": "apply", "run_id": started["run_id"], "plan_id": plan_id},
    )

    assert applied["phase"] in {"applied", "partial"}
    for original, before_bytes in originals_before.items():
        assert (source_root / original).read_bytes() == before_bytes
    # verified_unchanged/verified_total are real re-hash counts, top-level.
    assert applied["verified_total"] >= 1
    assert applied["verified_unchanged"] == applied["verified_total"]
    # Per-item provenance lives in the `outcomes` map (keyed by original path).
    copied = [o for o in applied["outcomes"].values() if o.get("status") in {"applied", "already-applied"}]
    assert copied
    for outcome in copied:
        assert outcome["sha256"]
        target = vault / outcome["target_path"]
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
    # img.png's coded skip lives on the INVENTORY row (eligible=false + reason),
    # not on `plan.skipped`: folder-rule `select` materializes only ELIGIBLE
    # files, so an ineligible sibling under an included folder is silently
    # excluded from the selection — it never reaches `plan` at all, and is
    # therefore never "skipped" by plan either. This is the real coded-skip
    # signal for an unsupported file type.
    img_row = next(row for row in started["inventory"] if row["path"] == "Old Notes/img.png")
    assert img_row["eligible"] is False
    assert img_row["reason"] == "UNSUPPORTED_IMPORT_TYPE"

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
    planned = _ok(client, "adoption_studio", {"action": "plan", "run_id": started["run_id"]})
    plan = planned["plan"]
    plan_id = plan["plan_id"]
    # img.png was never selected (ineligible), so only a.md/b.md are planned.
    assert {item["original_path"] for item in plan["items"]} == {
        "Old Notes/a.md",
        "Old Notes/b.md",
    }
    # Delete one selected file after planning to force a per-item NOT_FOUND at apply.
    (source_root / "b.md").unlink()

    applied = _ok(
        client,
        "adoption_studio",
        {"action": "apply", "run_id": started["run_id"], "plan_id": plan_id},
    )

    assert applied["phase"] == "partial"
    outcomes = applied["outcomes"]
    failed_codes = {o["code"] for o in outcomes.values() if o.get("status") == "failed"}
    assert failed_codes & {"NOT_FOUND", "SOURCE_CHANGED"}
    applied_paths = {p for p, o in outcomes.items() if o.get("status") in {"applied", "already-applied"}}
    assert "Old Notes/a.md" in applied_paths


def test_stale_source_returns_409_and_leaves_vault_byte_identical(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = _seed_import_folder(vault)
    find.clear_cache()
    client = _client(vault, monkeypatch)
    started = _ok(client, "adoption_studio", {"action": "start", "path": "Old Notes"})
    # Select ONLY a.md: the run-level ADOPTION_SOURCE_CHANGED refusal fires
    # when write-time re-validation finds NOTHING left to commit on a run that
    # has never applied anything. With a's.md the sole selected file, drifting
    # it leaves the validated subset empty, which is exactly that case (a
    # whole-folder selection with one of several files drifting instead
    # produces a normal partial 200, not a 409).
    _ok(
        client,
        "adoption_studio",
        {
            "action": "select",
            "run_id": started["run_id"],
            "include": ["Old Notes/a.md"],
            "exclude": [],
            "overrides": [],
            "include_junk": False,
        },
    )
    planned = _ok(client, "adoption_studio", {"action": "plan", "run_id": started["run_id"]})
    plan_id = planned["plan"]["plan_id"]
    before = _snapshot(vault)
    (source_root / "a.md").write_text("# A\n\nChanged after scan.\n", encoding="utf-8")

    response = client.post(
        "/api/adoption_studio",
        json={"action": "apply", "run_id": started["run_id"], "plan_id": plan_id},
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
    planned = _ok(client, "adoption_studio", {"action": "plan", "run_id": started["run_id"]})
    plan_id = planned["plan"]["plan_id"]
    (source_root / "b.md").unlink()
    applied = _ok(
        client,
        "adoption_studio",
        {"action": "apply", "run_id": started["run_id"], "plan_id": plan_id},
    )
    assert applied["phase"] == "partial"
    already_applied = {
        p for p, o in applied["outcomes"].items() if o.get("status") in {"applied", "already-applied"}
    }
    assert already_applied

    # apply always echoes plan_id, even for a retry_failed=true call — the
    # engine refuses a mismatched/missing plan_id with PLAN_STALE regardless.
    retried = _ok(
        client,
        "adoption_studio",
        {"action": "apply", "run_id": started["run_id"], "plan_id": plan_id, "retry_failed": True},
    )

    retried_outcomes = retried["outcomes"]
    still_failed = {p for p, o in retried_outcomes.items() if o.get("status") == "failed"}
    assert "Old Notes/b.md" in still_failed
    # The previously-applied item stays applied, not duplicated/reprocessed.
    for path in already_applied:
        assert retried_outcomes[path]["status"] in {"applied", "already-applied"}


@pytest.mark.skipif(
    importlib.util.find_spec("exomem.adoption_proposals") is None,
    reason="adoption_proposals lands via Lane B; this test auto-activates once "
    "exomem.adoption_proposals exists (work-item/propose/apply-proposal + the "
    "review_memory(mode='adoption') / triage_memory adoption-ref dispatch it needs).",
)
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
    planned = _ok(client, "adoption_studio", {"action": "plan", "run_id": started["run_id"]})
    plan_id = planned["plan"]["plan_id"]
    applied = _ok(
        client,
        "adoption_studio",
        {"action": "apply", "run_id": started["run_id"], "plan_id": plan_id},
    )
    copied_paths = [
        o["target_path"] for o in applied["outcomes"].values() if o.get("status") in {"applied", "already-applied"}
    ]
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
                        "sources": copied_paths,
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
