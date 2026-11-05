"""Import recovery must cross the same mutation boundary as an agent's retry."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastmcp import Client
from test_adoption_collection import TODAY, _import, _vault
from test_adoption_own_notes import JOURNAL_RULE, _notes

from exomem import adoption_run, command_surface, server, writer_lease
from exomem import indexes, vault as vault_module
from exomem.cli_ops import OpError


def test_refused_reconciliation_retains_the_original_unknown_identity(tmp_path):
    """A scope/custody refusal must not erase the old unknown and authorize generic replay."""
    store = writer_lease.IdempotencyStore(tmp_path / "runtime/idempotency.sqlite")
    digest = "a" * 64

    class Interrupted(BaseException):
        pass

    def interrupted():
        raise Interrupted()

    with pytest.raises(Interrupted):
        store.run("same-identity", digest, interrupted)
    calls = []
    with pytest.raises(OpError, match="MUTATION_OUTCOME_UNKNOWN"):
        store.run("same-identity", digest, lambda: calls.append("unsafe"))

    def unresolved():
        assert writer_lease.active_mutation_recovery_identity()[2] is True
        raise OpError("IMPORT_RECOVERY_UNRESOLVED", "original scope cannot be established")

    with pytest.raises(OpError, match="IMPORT_RECOVERY_UNRESOLVED"):
        store.run("same-identity", digest, unresolved, reconcile_abandoned=True)
    with pytest.raises(OpError, match="MUTATION_OUTCOME_UNKNOWN"):
        store.run("same-identity", digest, lambda: calls.append("unsafe"))
    with pytest.raises(OpError, match="IDEMPOTENCY_KEY_REUSED"):
        store.run("same-identity", "b" * 64, lambda: calls.append("unsafe"), reconcile_abandoned=True)
    assert calls == []


def test_import_reconciliation_cannot_reclaim_an_active_owner(tmp_path):
    """A live exact import remains the sole writer even when a retry permits reconciliation."""
    store = writer_lease.IdempotencyStore(tmp_path / "runtime/idempotency.sqlite", wait_seconds=0)
    entered, release = threading.Event(), threading.Event()

    def active_import():
        entered.set()
        assert release.wait(10)
        return {"applied": 1}

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(store.run, "same-identity", "a" * 64, active_import)
        try:
            assert entered.wait(5)
            with pytest.raises(OpError, match="MUTATION_ACKNOWLEDGEMENT_PENDING"):
                store.run("same-identity", "a" * 64, lambda: pytest.fail("second writer executed"),
                          reconcile_abandoned=True)
        finally:
            release.set()
        assert first.result(timeout=5) == {"applied": 1}
    assert store.run("same-identity", "a" * 64, lambda: pytest.fail("completed leaf executed again"),
                     reconcile_abandoned=True) == {"applied": 1}


@pytest.mark.parametrize("cut", ["source", "artifact", "log", "source-no-log"])
def test_partial_chunk_completes_only_frozen_components_and_logs_once(tmp_path, monkeypatch, cut):
    """A crash between destinations must keep the artifact, Source identity and exact count."""
    original = b"representative binary bytes\x00\xff" * 128
    root = _vault(tmp_path, {"Coll/document.bin": original})
    run_id, planned = _import(root, path="Coll", include=["Coll"])
    plan_id = planned["plan"]["plan_id"]
    item = planned["plan"]["items"][0]
    paths = {"source": root / item["target_path"], "artifact": root / item["artifact_path"],
             "log": root / "Knowledge Base/log.md"}
    if cut == "source-no-log":
        paths["log"].unlink()

    class Interrupted(BaseException):
        pass

    def interrupt_after_destination(path):
        if path == paths["source" if cut == "source-no-log" else cut]:
            raise Interrupted()

    with monkeypatch.context() as patch:
        patch.setattr(vault_module, "_after_batch_destination_published", interrupt_after_destination)
        with writer_lease.active_mutation_claim_context(
            claim_token="a" * 24, command_digest="a" * 64, identity_digest="1" * 64,
        ):
            with pytest.raises(Interrupted):
                adoption_run.apply(root, run_id=run_id, plan_id=plan_id, today=TODAY)
    source_bytes = paths["source"].read_bytes()
    assert paths["artifact"].exists() == (cut not in {"source", "source-no-log"})
    store = adoption_run.AdoptionRunStore(root)
    assert store.load(run_id)["outcomes"] == {}
    if cut == "source":
        # A second process stop, now after the missing artifact lands, must keep the same chunk identity.
        def interrupt_recovery(path):
            if path == paths["artifact"]:
                raise Interrupted()

        with monkeypatch.context() as patch:
            patch.setattr(vault_module, "_after_batch_destination_published", interrupt_recovery)
            with writer_lease.active_mutation_claim_context(
                claim_token="b" * 24, command_digest="a" * 64, identity_digest="1" * 64, reconciling=True,
            ):
                with pytest.raises(Interrupted):
                    adoption_run.apply(root, run_id=run_id, plan_id=plan_id, today=TODAY)
        assert paths["source"].read_bytes() == source_bytes
        assert paths["artifact"].read_bytes() == original
    with writer_lease.active_mutation_claim_context(
        claim_token="c" * 24, command_digest="a" * 64, identity_digest="1" * 64, reconciling=True,
    ):
        recovered = adoption_run.apply(root, run_id=run_id, plan_id=plan_id, today=TODAY)
        assert recovered["phase"] == "applied"
        assert recovered["progress"]["remaining"] == 0
        assert paths["source"].read_bytes() == source_bytes
        assert paths["artifact"].read_bytes() == original
        assert (root / "Coll/document.bin").read_bytes() == original
        counts = indexes._count_sources(root / "Knowledge Base/Sources")
        assert sum(counts.values()) == 1
        assert "- Sources: 1" in (root / "Knowledge Base/index.md").read_text(encoding="utf-8")
        history = vault_module.read_operation_log(root)
        entries = [entry for entry in history["entries"] if entry["op"] == "adopt-copy"]
        assert len(entries) == (0 if cut == "source-no-log" else 1)
        if entries:
            assert entries[0]["provenance"]["import_chunk"]
        snapshot = {path: path.read_bytes() for path in (root / "Knowledge Base").rglob("*") if path.is_file()}
        replayed = adoption_run.apply(root, run_id=run_id, plan_id=plan_id, today=TODAY)
        assert replayed["apply_result"] == recovered["apply_result"]
        assert {path: path.read_bytes() for path in snapshot} == snapshot


def test_interrupted_failed_subset_keeps_its_owed_note_after_another_invocation(tmp_path, monkeypatch):
    """A Source-only receipt must stay in A's scope after B changes the run's current scope."""
    originals = {f"Coll/Journal/{name}.md": f"# Entry {name}\n\nMaterial {name}.\n" for name in ("a", "b")}
    root = _vault(tmp_path, originals)
    run_id, planned = _import(root, path="Coll", include=[JOURNAL_RULE])
    plan_id = planned["plan"]["plan_id"]
    store = adoption_run.AdoptionRunStore(root)
    run = store.load(run_id)
    first_path, second_path = originals
    run["outcomes"] = {first_path: {"status": "failed", "code": "BATCH_ROLLED_BACK"}}
    run["phase"] = "failed"
    store.save(run)

    def crash_before_note(*args, **kwargs):
        raise RuntimeError("synthetic interruption after Source, before draft note")

    with monkeypatch.context() as patch:
        patch.setattr(adoption_run, "_create_notes", crash_before_note)
        with writer_lease.active_mutation_claim_context(
            claim_token="a" * 24, command_digest="a" * 64, identity_digest="1" * 64,
        ):
            with pytest.raises(RuntimeError, match="synthetic interruption"):
                adoption_run.apply(root, run_id=run_id, plan_id=plan_id, today=TODAY,
                                   retry_failed=True, only_paths=[first_path])
    interrupted = store.load(run_id)
    assert interrupted["outcomes"][first_path]["status"] == "source-applied"
    assert interrupted["apply_invocations"]["1" * 64]["scope"] == [first_path]
    source = root / interrupted["outcomes"][first_path]["target_path"]
    assert _notes(root) == []

    with writer_lease.active_mutation_claim_context(
        claim_token="b" * 24, command_digest="b" * 64, identity_digest="2" * 64,
    ):
        adoption_run.apply(root, run_id=run_id, plan_id=plan_id, today=TODAY, only_paths=[second_path])
    assert store.load(run_id)["scope"] == [second_path]
    assert len(_notes(root)) == 1
    # The run summary covers every planned item, even though B's own subset is complete.
    summary = adoption_run.status(root, run_id=run_id, view="summary")
    assert summary["counts"]["applied"] == 1
    assert summary["counts"]["source_applied"] == 1
    assert summary["remaining"] == 1
    assert summary["interrupted"] is True

    with writer_lease.active_mutation_claim_context(
        claim_token="c" * 24, command_digest="a" * 64, identity_digest="1" * 64, reconciling=True,
    ):
        recovered = adoption_run.apply(root, run_id=run_id, plan_id=plan_id, today=TODAY,
                                       retry_failed=True, only_paths=[first_path])
        assert recovered["phase"] == "applied"
        assert "apply_invocations" not in recovered
        assert len(_notes(root)) == 2
        assert recovered["outcomes"][first_path]["note"]["target_path"]
        # A saved leaf reply survives the gap before the outer writer's signed receipt.
        snapshot = {path: path.read_bytes() for path in (root / "Knowledge Base").rglob("*.md")}
        replayed = adoption_run.apply(root, run_id=run_id, plan_id=plan_id, today=TODAY,
                                      retry_failed=True, only_paths=[first_path])
        assert replayed["apply_result"] == recovered["apply_result"]
        assert {path: path.read_bytes() for path in snapshot} == snapshot
    assert source.exists()
    assert adoption_run.status(root, run_id=run_id, view="summary")["interrupted"] is False
    assert recovered["outcomes"][first_path]["sha256"] == hashlib.sha256(originals[first_path].encode()).hexdigest()
    assert {path: (root / path).read_text(encoding="utf-8") for path in originals} == originals
    assert len(list((root / "Knowledge Base/Sources/Imported").glob("*.md"))) == 2


def test_unfinished_chunk_rejects_an_overlapping_invocation_and_changed_post_image(tmp_path, monkeypatch):
    """A later call or edit cannot replace the evidence that owns a partial publication."""
    original = b"original artifact bytes"
    root = _vault(tmp_path, {"Coll/document.bin": original})
    run_id, planned = _import(root, path="Coll", include=["Coll"])
    plan_id = planned["plan"]["plan_id"]
    item = planned["plan"]["items"][0]
    source = root / item["target_path"]
    store = adoption_run.AdoptionRunStore(root)

    class Interrupted(BaseException):
        pass

    def interrupt_after_source(path):
        if path == source:
            raise Interrupted()

    with monkeypatch.context() as patch:
        patch.setattr(vault_module, "_after_batch_destination_published", interrupt_after_source)
        with writer_lease.active_mutation_claim_context(
            claim_token="a" * 24, command_digest="a" * 64, identity_digest="1" * 64,
        ):
            with pytest.raises(Interrupted):
                adoption_run.apply(root, run_id=run_id, plan_id=plan_id, today=TODAY)
    run_bytes = store._run_file(run_id).read_bytes()
    with writer_lease.active_mutation_claim_context(
        claim_token="b" * 24, command_digest="a" * 64, identity_digest="2" * 64,
    ):
        with pytest.raises(adoption_run.AdoptionRunError, match="IMPORT_RECOVERY_UNRESOLVED"):
            adoption_run.apply(root, run_id=run_id, plan_id=plan_id, today=TODAY)
    assert store._run_file(run_id).read_bytes() == run_bytes
    changed = source.read_bytes() + b"\nAn independent edit must survive.\n"
    source.write_bytes(changed)
    with writer_lease.active_mutation_claim_context(
        claim_token="c" * 24, command_digest="a" * 64, identity_digest="1" * 64, reconciling=True,
    ):
        with pytest.raises(adoption_run.AdoptionRunError, match="IMPORT_RECOVERY_UNRESOLVED"):
            adoption_run.apply(root, run_id=run_id, plan_id=plan_id, today=TODAY)
    assert source.read_bytes() == changed
    assert not (root / item["artifact_path"]).exists()
    assert (root / item["original_path"]).read_bytes() == original
    assert store.load(run_id)["outcomes"] == {}


@pytest.mark.parametrize("scope_kind", ["all", "subset", "empty"])
def test_legacy_open_continuation_retains_its_effective_scope(tmp_path, scope_kind):
    """Ignored retry/path arguments must not widen or replace a legacy cursor's scope."""
    originals = {f"Coll/{name}.md": f"# Document {name}\n\nOriginal {name}.\n" for name in ("a", "b")}
    root = _vault(tmp_path, originals)
    run_id, planned = _import(root, path="Coll", include=["Coll"])
    first_path, second_path = originals
    scope = None if scope_kind == "all" else [first_path] if scope_kind == "subset" else []
    store = adoption_run.AdoptionRunStore(root)
    raw = store.load(run_id)
    raw.update(phase="applying", inflight=True, scope=scope)
    cursor = adoption_run._issue_cursor(raw, "apply")
    raw["cursors"][cursor].pop("scope")  # Pre-repair cursors did not carry a scope field.
    store.save(raw)
    with writer_lease.active_mutation_claim_context(
        claim_token="a" * 24, command_digest="a" * 64, identity_digest="1" * 64, reconciling=True,
    ):
        recovered = adoption_run.apply(root, run_id=run_id, plan_id=planned["plan"]["plan_id"],
                                       continuation=cursor, retry_failed=True, only_paths=[second_path], today=TODAY)
    expected = set(originals) if scope is None else set(scope)
    assert set(recovered["outcomes"]) == expected
    assert recovered["progress"]["total"] == len(expected)
    assert recovered["progress"]["remaining"] == 0
    assert store.load(run_id)["scope"] == scope
    assert {path: (root / path).read_text(encoding="utf-8") for path in originals} == originals


def test_legacy_superseded_cursor_cannot_recover_a_later_invocations_scope(tmp_path):
    """A fresh B marks A used with an empty reply, which cannot authorize A over B's scope."""
    root = _vault(tmp_path, {"Coll/a.md": "# A\n\nOriginal A.\n", "Coll/b.md": "# B\n\nOriginal B.\n"})
    run_id, planned = _import(root, path="Coll", include=["Coll"])
    store = adoption_run.AdoptionRunStore(root)
    raw = store.load(run_id)
    raw.update(phase="applying", inflight=True, scope=["Coll/a.md"])
    cursor = adoption_run._issue_cursor(raw, "apply")
    raw["cursors"][cursor].pop("scope")
    store.save(raw)
    adoption_run.apply(root, run_id=run_id, plan_id=planned["plan"]["plan_id"], only_paths=["Coll/b.md"], today=TODAY)
    before = {path: path.read_bytes() for path in (root / "Knowledge Base").rglob("*") if path.is_file()}
    with writer_lease.active_mutation_claim_context(
        claim_token="a" * 24, command_digest="a" * 64, identity_digest="1" * 64, reconciling=True,
    ):
        with pytest.raises(adoption_run.AdoptionRunError, match="IMPORT_RECOVERY_UNRESOLVED"):
            adoption_run.apply(root, run_id=run_id, plan_id=planned["plan"]["plan_id"], continuation=cursor, today=TODAY)
    assert {path: path.read_bytes() for path in before} == before


def test_legacy_completed_cursor_replays_its_saved_reply_after_scope_changes(tmp_path):
    """A completed cursor's reply survives B without running work in B's current scope."""
    root = _vault(tmp_path, {"Coll/a.md": "# A\n\nOriginal A.\n", "Coll/b.md": "# B\n\nOriginal B.\n"})
    run_id, planned = _import(root, path="Coll", include=["Coll"])
    store = adoption_run.AdoptionRunStore(root)
    raw = store.load(run_id)
    raw.update(phase="applying", inflight=True, scope=["Coll/a.md"])
    cursor = adoption_run._issue_cursor(raw, "apply")
    raw["cursors"][cursor].pop("scope")
    store.save(raw)
    completed = adoption_run.apply(root, run_id=run_id, plan_id=planned["plan"]["plan_id"], continuation=cursor, today=TODAY)
    adoption_run.apply(root, run_id=run_id, plan_id=planned["plan"]["plan_id"], only_paths=["Coll/b.md"], today=TODAY)
    before = {path: path.read_bytes() for path in (root / "Knowledge Base").rglob("*") if path.is_file()}
    with writer_lease.active_mutation_claim_context(
        claim_token="a" * 24, command_digest="a" * 64, identity_digest="1" * 64, reconciling=True,
    ):
        replayed = adoption_run.apply(root, run_id=run_id, plan_id=planned["plan"]["plan_id"], continuation=cursor, today=TODAY)
    assert replayed["apply_result"] == completed["apply_result"]
    assert replayed["progress"] == completed["progress"]
    assert {path: path.read_bytes() for path in before} == before


@pytest.mark.parametrize("missing_proof", ["failed-subset", "missing-scope", "invalid-scope", "invalid-reply", "partial-source"])
def test_legacy_recovery_without_scope_or_publication_proof_changes_nothing(tmp_path, missing_proof):
    """Old failed subsets and incomplete or corrupt receipts cannot authorize a broader import."""
    root = _vault(tmp_path, {"Coll/a.md": "# A\n\nOriginal A.\n", "Coll/b.md": "# B\n\nOriginal B.\n"})
    run_id, planned = _import(root, path="Coll", include=["Coll"])
    store = adoption_run.AdoptionRunStore(root)
    raw = store.load(run_id)
    options = {}
    if missing_proof == "failed-subset":
        raw.update(phase="applying", inflight=True, scope=["Coll/a.md"])
        raw["outcomes"] = {"Coll/a.md": {"status": "source-applied"}}
        options["retry_failed"] = True
    elif missing_proof == "partial-source":
        target = root / planned["plan"]["items"][0]["target_path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("A destination with no complete chunk receipt.\n", encoding="utf-8")
    else:
        raw.update(phase="applying", inflight=True, scope=["Coll/a.md"])
        cursor = adoption_run._issue_cursor(raw, "apply")
        raw["cursors"][cursor].pop("scope")
        options["continuation"] = cursor
        if missing_proof == "missing-scope":
            del raw["scope"]
        elif missing_proof == "invalid-scope":
            raw["scope"] = ["Coll/unplanned.md"]
        else:
            raw["cursors"][cursor].update(state="used", reply={"apply_result": {}, "progress": {"total": 99}})
    store.save(raw)
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    with writer_lease.active_mutation_claim_context(
        claim_token="a" * 24, command_digest="a" * 64, identity_digest="1" * 64, reconciling=True,
    ):
        with pytest.raises(adoption_run.AdoptionRunError, match="IMPORT_RECOVERY_UNRESOLVED"):
            adoption_run.apply(root, run_id=run_id, plan_id=planned["plan"]["plan_id"], today=TODAY, **options)
    assert {path: path.read_bytes() for path in root.rglob("*") if path.is_file()} == before


def test_replan_after_prepublication_refusal_has_no_phantom_interruption(vault, monkeypatch):
    """A known uncommitted refusal must not make a later successful plan look interrupted."""
    folder = vault / "Legacy"
    folder.mkdir()
    original = folder / "document.md"
    original.write_text("# Initial\n\nInitial material.\n", encoding="utf-8")
    run = adoption_run.start(vault, path="Legacy", consume=False)
    run_id = run["run_id"]
    adoption_run.select(vault, run_id=run_id, include=["Legacy"])
    planned = adoption_run.plan(vault, run_id=run_id)
    original.write_text("# Updated\n\nUpdated material.\n", encoding="utf-8")
    monkeypatch.setattr(server, "load_dotenv", lambda *args, **kwargs: None)

    async def journey():
        async with Client(server.build_server(require_auth=False)) as client:
            refused = await client.call_tool("import_memory", {"operation": {
                "kind": "apply", "run_id": run_id, "plan_id": planned["plan"]["plan_id"]},
                "response_detail": "full"}, raise_on_error=False,
                meta={command_surface.MCP_IDEMPOTENCY_META_KEY: "refused-plan"})
            assert "ADOPTION_SOURCE_CHANGED" in json.dumps(refused.structured_content)
            assert adoption_run.status(vault, run_id=run_id, view="summary")["interrupted"] is False
            refreshed = adoption_run.plan(vault, run_id=run_id)
            result = await client.call_tool("import_memory", {"operation": {
                "kind": "apply", "run_id": run_id, "plan_id": refreshed["plan"]["plan_id"]},
                "response_detail": "full"}, raise_on_error=False,
                meta={command_surface.MCP_IDEMPOTENCY_META_KEY: "refreshed-plan"})
            assert not result.is_error and result.structured_content.get("success") is not False, result
            summary = adoption_run.status(vault, run_id=run_id, view="summary")
            assert summary["phase"] == "applied" and summary["remaining"] == 0
            assert summary["interrupted"] is False

    try:
        asyncio.run(journey())
    finally:
        writer_lease.reset_managers_for_tests()


def test_same_identity_recovers_a_frozen_chunk_after_rename_before_source(tmp_path, monkeypatch):
    """The rename journal and invocation checkpoint must retain one original and one Source identity."""
    import test_adoption_consume as consume
    import test_adoption_rename as rename

    monkeypatch.setattr(consume.local_import, "user_state_dir", lambda name: str(tmp_path / "user-state"))
    root = consume._vault(tmp_path)
    original = b"representative staged binary\x00\xff" * 128
    import_id = rename._job_stage(root, {"scan.bin": original})
    staged = rename._staged(root, import_id)
    inode = staged.stat().st_ino
    run_id, plan_id = consume._plan(root, f"_Imports/{import_id}")
    store = adoption_run.AdoptionRunStore(root)
    planned = store.load(run_id)["plan"]["items"][0]
    with monkeypatch.context() as patch:
        rename._stop_after_the_rename(patch)
        with writer_lease.active_mutation_claim_context(
            claim_token="a" * 24, command_digest="a" * 64, identity_digest="1" * 64,
        ):
            with pytest.raises(rename._Stopped):
                adoption_run.apply(root, run_id=run_id, plan_id=plan_id, today=consume.TODAY)
    frozen = store.load(run_id)["apply_invocations"]["1" * 64]["publication"]["items"][planned["original_path"]]
    artifact, source = root / planned["artifact_path"], root / planned["target_path"]
    assert not staged.exists() and not source.exists()
    assert artifact.stat().st_ino == inode and artifact.read_bytes() == original
    with writer_lease.active_mutation_claim_context(
        claim_token="b" * 24, command_digest="a" * 64, identity_digest="1" * 64, reconciling=True,
    ):
        recovered = adoption_run.apply(root, run_id=run_id, plan_id=plan_id, today=consume.TODAY)
    assert recovered["progress"] == {"applied": 1, "failed": 0, "remaining": 0, "total": 1}
    assert recovered["outcomes"][planned["original_path"]]["consumed"] is True
    assert hashlib.sha256(source.read_bytes()).hexdigest() == frozen["source_sha256"]
    assert artifact.read_bytes() == original and artifact.stat().st_ino == inode
    assert not staged.exists() and not rename.rename_journal.is_held(root, artifact)
    assert sum(indexes._count_sources(root / "Knowledge Base/Sources").values()) == 1


def test_large_run_status_is_bounded_and_pages_one_revision(vault, monkeypatch):
    """An agent can inspect thousands of rows without losing counts or page coverage."""
    folder = vault / "Legacy"
    folder.mkdir()
    (folder / "document.md").write_text("# Document\n\nOriginal material.\n", encoding="utf-8")
    started = adoption_run.start(vault, path="Legacy", consume=False)
    adoption_run.select(vault, run_id=started["run_id"], include=["Legacy"])
    planned = adoption_run.plan(vault, run_id=started["run_id"])
    store = adoption_run.AdoptionRunStore(vault)
    run = store.load(started["run_id"])
    inventory = run["inventory"][0]
    item = run["plan"]["items"][0]
    paths = [f"Legacy/document-{index:05d}.md" for index in range(5000)]
    run["inventory"] = [{**inventory, "path": path} for path in paths]
    run["selection"]["paths"] = paths
    run["plan"]["items"] = [{**copy.deepcopy(item), "original_path": path} for path in paths]
    run["phase"] = "applying"
    run["outcomes"] = {path: {"status": "applied"} for path in paths[:2000]}
    run["outcomes"].update({path: {"status": "failed", "code": "SOURCE_CHANGED"}
                            for path in paths[2000:3000]})
    cursor = adoption_run._issue_cursor(run, "apply")
    store.save(run)
    original_run_bytes = store._run_file(run["run_id"]).read_bytes()
    monkeypatch.setattr(server, "load_dotenv", lambda *args, **kwargs: None)

    async def journey():
        async with Client(server.build_server(require_auth=False)) as client:
            result = await client.call_tool(
                "read_import", {"operation": {"kind": "status", "run_id": run["run_id"]}})
            summary = result.structured_content
            assert len(json.dumps(summary).encode()) < 16 * 1024
            assert summary["counts"]["inventory"] == 5000
            assert summary["counts"]["selected"] == 5000
            assert summary["counts"]["applied"] == 2000
            assert summary["counts"]["failed"] == 1000
            assert summary["remaining"] == 2000
            assert summary["continuation"] == cursor
            assert summary["plan_id"] == planned["plan"]["plan_id"]
            assert "inventory" not in summary and "outcomes" not in summary
            revision = summary["revision"]
            seen = []
            offset = 0
            while offset is not None:
                result = await client.call_tool("read_import", {"operation": {
                    "kind": "status", "run_id": run["run_id"], "view": "inventory",
                    "offset": offset, "limit": 100, "revision": revision}})
                page = result.structured_content
                assert page["total"] == 5000
                assert len(page["items"]) <= 100
                assert len(json.dumps(page).encode()) < 48 * 1024
                seen.extend(row["path"] for row in page["items"])
                offset = page["next_offset"]
            assert seen == paths
            assert store._run_file(run["run_id"]).read_bytes() == original_run_bytes
            changed = store.load(run["run_id"])
            changed["phase"] = "partial"
            store.save(changed)
            stale = await client.call_tool("read_import", {"operation": {
                "kind": "status", "run_id": run["run_id"], "view": "outcomes", "revision": revision}},
                raise_on_error=False)
            assert "IMPORT_STATUS_CHANGED" in json.dumps(stale.structured_content)

    try:
        asyncio.run(journey())
        # Only the explicit fixture mutation changed the canonical run.
        assert original_run_bytes != store._run_file(run["run_id"]).read_bytes()
        assert adoption_run.status(vault, run_id=run["run_id"])["inventory"] == run["inventory"]
    finally:
        writer_lease.reset_managers_for_tests()


def test_oversized_status_row_advances_with_an_explicit_omission(vault):
    """One very large plan row must not trap an agent on the same page forever."""
    run = adoption_run.start(vault)
    store = adoption_run.AdoptionRunStore(vault)
    raw = store.load(run["run_id"])
    raw["plan"] = {"plan_id": "large-metadata-plan", "items": [
        {"original_path": "Legacy/large.md", "aliases": ["a" * 40000]},
        {"original_path": "Legacy/small.md"},
    ]}
    store.save(raw)
    page = adoption_run.status(vault, run_id=run["run_id"], view="plan", limit=1)
    assert page["items"] == []
    assert page["omitted"][0]["code"] == "ROW_TOO_LARGE"
    assert page["omitted"][0]["offset"] == 0
    assert page["total"] == 2 and page["next_offset"] == 1
    next_page = adoption_run.status(
        vault, run_id=run["run_id"], view="plan", limit=1, offset=page["next_offset"], revision=page["revision"])
    assert next_page["items"] == [{"original_path": "Legacy/small.md"}]
    assert next_page["next_offset"] is None


@pytest.mark.parametrize("fields", [{"limit": 0}, {"limit": 101}, {"offset": -1}, {"offset": 1}])
def test_status_rejects_unbounded_or_unbound_page_requests(tmp_path, fields):
    """Malformed page requests cannot bypass the bounds or silently page changing state."""
    with pytest.raises(adoption_run.AdoptionRunError, match="IMPORT_REQUEST_INVALID"):
        adoption_run.status(tmp_path, run_id="unused", view="inventory", **fields)


@pytest.mark.parametrize("checkpoint_format", ["current", "legacy", "legacy-continuation"])
def test_crashed_apply_resumes_same_mcp_identity_without_duplicate_sources(vault, monkeypatch, checkpoint_format):
    """A landed chunk must not strand an exact agent retry behind an unknown terminal."""
    originals = {}
    folder = vault / "Legacy"
    folder.mkdir()
    for index in range(4):
        path = folder / f"document-{index}.md"
        path.write_text(f"# Document {index}\n\nOriginal material {index}.\n", encoding="utf-8")
        originals[path] = hashlib.sha256(path.read_bytes()).hexdigest()
    run = adoption_run.start(vault, path="Legacy", consume=False)
    adoption_run.select(vault, run_id=run["run_id"], include=["Legacy"])
    planned = adoption_run.plan(vault, run_id=run["run_id"])
    targets = {item["target_path"] for item in planned["plan"]["items"]}
    before = set((vault / "Knowledge Base" / "Sources").rglob("*.md"))
    monkeypatch.setattr(adoption_run, "APPLY_CHUNK_ITEMS", 2)
    monkeypatch.setattr(server, "load_dotenv", lambda *args, **kwargs: None)
    real_commit = adoption_run._commit_chunk
    crashed = False

    def commit_then_crash(*args, **kwargs):
        nonlocal crashed
        result = real_commit(*args, **kwargs)
        if not crashed:
            crashed = True
            raise RuntimeError("synthetic interruption after Source commit")
        return result

    monkeypatch.setattr(adoption_run, "_commit_chunk", commit_then_crash)
    arguments = {
        "operation": {"kind": "apply", "run_id": run["run_id"],
                      "plan_id": planned["plan"]["plan_id"]},
        "response_detail": "full",
    }
    if checkpoint_format == "legacy-continuation":
        store = adoption_run.AdoptionRunStore(vault)
        raw = store.load(run["run_id"])
        raw.update(phase="applying", inflight=True, scope=None)
        cursor = adoption_run._issue_cursor(raw, "apply")
        raw["cursors"][cursor].pop("scope")
        store.save(raw)
        arguments["operation"].update(continuation=cursor, retry_failed=True,
                                      only_paths=[planned["plan"]["items"][-1]["original_path"]])

    async def journey():
        async with Client(server.build_server(require_auth=False)) as client:
            meta = {command_surface.MCP_IDEMPOTENCY_META_KEY: "interrupted-import"}
            with monkeypatch.context() as patch:
                if checkpoint_format.startswith("legacy"):
                    # Publish the pre-repair format: no invocation/chunk checkpoints or new log provenance.
                    patch.setattr(writer_lease, "active_mutation_recovery_identity", lambda: None)
                    # Freeze the old producer recipe independently of the current digest helper.
                    def legacy_digest(command, kwargs):
                        arguments = {key: value for key, value in kwargs.items()
                                     if key not in ("view", "offset", "limit", "revision")}
                        return hashlib.sha256(json.dumps(
                            {"command": command.name, "kwargs": arguments}, sort_keys=True,
                            separators=(",", ":"), default=str,
                        ).encode("utf-8")).hexdigest()

                    patch.setattr(writer_lease, "_command_digest", legacy_digest)
                first = await client.call_tool("import_memory", arguments, raise_on_error=False, meta=meta)
            landed = adoption_run.AdoptionRunStore(vault).load(run["run_id"])
            assert len(landed["outcomes"]) == 2
            if checkpoint_format.startswith("legacy"):
                assert "apply_invocations" not in landed
            assert first.structured_content["error"]["code"] == "MUTATION_COMMITTED_ACKNOWLEDGEMENT_UNCERTAIN"
            recovered = await client.call_tool("import_memory", arguments, raise_on_error=False, meta=meta)
            assert not recovered.is_error, recovered
            assert "MUTATION_OUTCOME_UNKNOWN" not in json.dumps(recovered.structured_content)
            assert recovered.structured_content.get("success") is not False
            run_bytes = adoption_run.AdoptionRunStore(vault)._run_file(run["run_id"]).read_bytes()
            replayed = await client.call_tool("import_memory", arguments, raise_on_error=False, meta=meta)
            assert not replayed.is_error
            assert adoption_run.AdoptionRunStore(vault)._run_file(run["run_id"]).read_bytes() == run_bytes
            return recovered

    try:
        asyncio.run(journey())
        persisted = adoption_run.AdoptionRunStore(vault).load(run["run_id"])
        assert persisted["phase"] == "applied"
        assert len(persisted["outcomes"]) == 4
        assert all(outcome["status"] in {"applied", "already-applied"}
                   for outcome in persisted["outcomes"].values())
        after = set((vault / "Knowledge Base" / "Sources").rglob("*.md"))
        assert after - before == {vault / target for target in targets}
        assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in originals} == originals
    finally:
        writer_lease.reset_managers_for_tests()


@pytest.mark.parametrize("cut", ["note", "final-run", "signed-receipt", "canonical-row"])
def test_same_mcp_identity_recovers_note_and_terminal_crash_cuts(vault, monkeypatch, cut):
    """A note or leaf result that landed before acknowledgement must not run twice or lose its progress."""
    from exomem import graph_sync

    initial_notes = set(_notes(vault))
    initial_sources = set((vault / "Knowledge Base/Sources").rglob("*.md"))
    original = "# Entry\n\nRepresentative original entry.\n"
    folder = vault / "Coll/Journal"
    folder.mkdir(parents=True)
    (folder / "entry.md").write_text(original, encoding="utf-8")
    run_id, planned = _import(vault, path="Coll", include=[JOURNAL_RULE])
    monkeypatch.setattr(server, "load_dotenv", lambda *args, **kwargs: None)
    fired = False

    def stop_once():
        nonlocal fired
        if not fired:
            fired = True
            raise RuntimeError("synthetic interruption before acknowledgement")

    if cut == "note":
        settle = adoption_run._settle_draft_note

        def stop_before_settling(*args, **kwargs):
            stop_once()
            return settle(*args, **kwargs)

        monkeypatch.setattr(adoption_run, "_settle_draft_note", stop_before_settling)
    elif cut == "final-run":
        save = adoption_run.AdoptionRunStore.save

        def save_then_stop(self, run):
            result = save(self, run)
            if any("reply" in invocation for invocation in (run.get("apply_invocations") or {}).values()):
                stop_once()
            return result

        monkeypatch.setattr(adoption_run.AdoptionRunStore, "save", save_then_stop)
    elif cut == "signed-receipt":
        persist = graph_sync.write_graph_commit_receipt

        def stop_before_receipt(*args, **kwargs):
            stop_once()
            return persist(*args, **kwargs)

        monkeypatch.setattr(graph_sync, "write_graph_commit_receipt", stop_before_receipt)
    else:
        persist = writer_lease.IdempotencyStore._persist_canonically_committed

        def stop_after_receipt(*args, **kwargs):
            stop_once()
            return persist(*args, **kwargs)

        monkeypatch.setattr(writer_lease.IdempotencyStore, "_persist_canonically_committed", stop_after_receipt)
    arguments = {"operation": {"kind": "apply", "run_id": run_id, "plan_id": planned["plan"]["plan_id"]},
                 "response_detail": "full"}
    store = adoption_run.AdoptionRunStore(vault)

    async def journey():
        async with Client(server.build_server(require_auth=False)) as client:
            meta = {command_surface.MCP_IDEMPOTENCY_META_KEY: "note-terminal-crash"}
            first = await client.call_tool("import_memory", arguments, raise_on_error=False, meta=meta)
            assert fired and first.structured_content["success"] is False
            assert first.structured_content["error"]["code"] == "MUTATION_COMMITTED_ACKNOWLEDGEMENT_UNCERTAIN"
            new_notes = set(_notes(vault)) - initial_notes
            assert len(new_notes) == 1
            note_bytes = {path: path.read_bytes() for path in new_notes}
            recovered = await client.call_tool("import_memory", arguments, raise_on_error=False, meta=meta)
            assert not recovered.is_error, recovered
            assert recovered.structured_content.get("success") is not False, recovered
            persisted = store.load(run_id)
            assert persisted["phase"] == "applied"
            assert persisted["outcomes"]["Coll/Journal/entry.md"]["note"]["target_path"]
            assert adoption_run._progress(persisted) == {"applied": 1, "failed": 0, "remaining": 0, "total": 1}
            assert set(_notes(vault)) - initial_notes == new_notes
            assert {path: path.read_bytes() for path in note_bytes} == note_bytes
            before = {path: path.read_bytes() for path in (vault / "Knowledge Base").rglob("*") if path.is_file()}
            replay = await client.call_tool("import_memory", arguments, raise_on_error=False, meta=meta)
            assert not replay.is_error
            assert {path: path.read_bytes() for path in before} == before

    try:
        asyncio.run(journey())
        assert (folder / "entry.md").read_text(encoding="utf-8") == original
        assert set((vault / "Knowledge Base/Sources").rglob("*.md")) - initial_sources == {
            vault / planned["plan"]["items"][0]["target_path"]}
    finally:
        writer_lease.reset_managers_for_tests()
