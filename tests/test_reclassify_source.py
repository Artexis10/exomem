"""Acceptance for correcting a captured source's classification.

Classification was a one-way door: `edit.py` refuses every write into
`Sources/`, so a source captured under the wrong kind stayed wrong forever, and
the `source_classification_debt` advisory shipped with the open taxonomy had
nothing to act on. This suite binds the correction path:

- kind and domain become correctable, and the location follows the corrected
  values through the same projection capture uses;
- the body is byte-identical afterwards, and identity and provenance survive;
- every inbound reference follows the source and the old path stays discoverable;
- nothing reclassifies on its own, and nothing guesses a kind for you.

Every fixture here is synthetic.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import threading
from pathlib import Path
from unittest.mock import ANY

import pytest
import yaml

from exomem import add as add_module
from exomem import lexstore
from exomem import reclassify_source as rc
from exomem import schema as schema_module
from exomem import source_taxonomy as st

TODAY = dt.date(2026, 8, 18)
KB = "Knowledge Base"


def _capture(
    vault: Path, source_schema: schema_module.SourceSchema, **kwargs: object
) -> add_module.AddResult:
    kwargs.setdefault("content", "Body text for a captured source.\n\nSecond paragraph.")
    # What a capture with no agent in the loop records: no kind was chosen.
    kwargs.setdefault("source_type", st.UNCLASSIFIED_KIND)
    kwargs.setdefault("today", TODAY)
    return add_module.add(vault, source_schema, **kwargs)  # type: ignore[arg-type]


def _front(vault: Path, rel: str) -> dict:
    text = (vault / rel).read_text(encoding="utf-8")
    front, _, _ = text.partition("\n---\n")
    return yaml.safe_load(front.removeprefix("---\n"))


def _body(vault: Path, rel: str) -> str:
    from exomem.vault import parse_frontmatter

    _, body, _ = parse_frontmatter((vault / rel).read_text(encoding="utf-8"))
    return body


def test_served_preview_after_move_waits_for_one_quiet_watcher_build(
    vault: Path, source_schema: schema_module.SourceSchema, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from _public_call import public_call
    from fastmcp import Client
    from record_fixtures import copy_dataset_fixture
    from test_agent_tool_surface import _call
    from test_collection_store_s1_gate import _until

    from exomem import file_watcher, find, freshness, move_file, readiness, request_budget, server
    from exomem import vault as vault_module

    monkeypatch.delenv("EXOMEM_DISABLE_RESOLVER_WARM")
    monkeypatch.setenv("EXOMEM_MODE", "quiet")
    monkeypatch.setenv("EXOMEM_DISABLE_GRAPH_INDEX", "1")
    monkeypatch.setattr(server, "load_dotenv", lambda *args, **kwargs: None)
    captured = _capture(vault, source_schema, title="Harbor measurements")
    note = f"{KB}/Notes/move-me.md"
    destination = f"{KB}/Notes/moved.md"
    (vault / note).write_text(f"# Measurements\n\n[[{captured.path}]]\n", encoding="utf-8")
    copy_dataset_fixture(vault)
    mcp = server.build_server(require_auth=False)
    mcp._exomem_local_runtime_activation.fallback_seconds = 60
    watcher = file_watcher.FileWatcher(vault, debounce_seconds=0.02)
    assert watcher.start()
    started, release = threading.Event(), threading.Event()
    builders: list[str] = []
    original_build = vault_module._build_inbound_index

    def paused_build(root: Path):
        builders.append(threading.current_thread().name)
        started.set()
        assert release.wait(15)
        return original_build(root)

    async def exercise() -> None:
        async with Client(mcp, timeout=5) as client:
            async def call(tool: str, **arguments):
                return await _call(client, *public_call(tool, arguments))

            moved = await call(
                "manage_memory_file", operation="move", old_path=note, new_path=destination,
            )
            assert moved.get("paths") == [note, destination], moved
            assert vault_module.inbound_recovery_available(vault)
            assert not readiness.is_warming()
            assert freshness.generation(vault, "vault") is not None
            await asyncio.to_thread(_until, lambda: not freshness.external_pending(vault))
            # Withdraw the cache after the move, as an idle quiet-mode service may.
            vault_module.evict_inbound_index(vault)
            monkeypatch.setattr(vault_module, "_build_inbound_index", paused_build)
            preview = {
                "operation": "propose-reclassification", "path": captured.path,
                "source_kind": "dataset-export",
            }
            try:
                cold = await call("manage_memory_file", **preview)
                assert cold["error"]["code"] == "RECLASSIFICATION_PREVIEW_WARMING"
                assert cold["error"]["retry"]["after_s"] > 0
                assert "references" not in cold and "preview_digest" not in cold
                assert await asyncio.to_thread(started.wait, 5)
                overlap, records = await asyncio.gather(
                    call("manage_memory_file", **preview),
                    call("query_records", collection="Meter readings", limit=1),
                )
                assert overlap["error"]["code"] == "RECLASSIFICATION_PREVIEW_WARMING"
                assert records.get("rows"), records
            finally:
                release.set()
            assert await asyncio.to_thread(find.await_recall_resolver_warm)
            warm = await call("manage_memory_file", **preview)
            assert warm.get("references") == 2, warm
            assert warm["referrers"]["rewritten"] == [destination, f"{KB}/Sources/index.md"]
            assert warm["preview_digest"]

            expired = threading.Event()
            plan = move_file.plan_inbound_rewrites

            class Budget(request_budget.RequestBudget):
                def remaining(self):
                    return 0.0 if expired.is_set() else super().remaining()

            def expire_during_planning(*args, **kwargs):
                expired.set()
                return plan(*args, **kwargs)

            monkeypatch.setattr(request_budget, "RequestBudget", Budget)
            monkeypatch.setattr(move_file, "plan_inbound_rewrites", expire_during_planning)
            timed_out = await call("manage_memory_file", **preview)
            assert timed_out["error"]["code"] == "RECLASSIFICATION_PREVIEW_WARMING"
            assert "references" not in timed_out and "preview_digest" not in timed_out

    try:
        assert watcher.wait_until_seeded(10)
        watcher.arm_optional_recovery()
        asyncio.run(exercise())
        assert builders == ["exomem-inbound-index-warm"]
    finally:
        release.set()
        watcher.stop()


@pytest.mark.parametrize("fault", [
    "release", "render", "cold-resolver", "removed-link", "changed-source", "catalogue-link", "catalogue-candidates",
])
def test_served_preview_retries_when_its_planning_or_release_proof_fails(vault, source_schema, monkeypatch, fault):
    from _public_call import public_call
    from fastmcp import Client
    from fastmcp.tools import FunctionTool
    from test_agent_tool_surface import _call

    from exomem import (
        file_watcher,
        find,
        freshness,
        move_file,
        path_mentions,
        request_budget,
        server,
    )
    from exomem import vault as vault_module
    from exomem.governance import egress

    monkeypatch.delenv("EXOMEM_DISABLE_RESOLVER_WARM")
    monkeypatch.setattr(server, "load_dotenv", lambda *args, **kwargs: None)
    source = _capture(vault, source_schema, title="Field measurements").path
    referrer = (
        f"{KB}/Evidence/field-log.md" if fault in ("cold-resolver", "catalogue-link")
        else f"{KB}/Notes/field-log.md"
    )
    (vault / referrer).parent.mkdir(parents=True, exist_ok=True)
    link = f"[[{source.removesuffix('.md')}]]"
    original_reference = {
        "catalogue-link": f"See `{source}`.",
        "catalogue-candidates": "The field log identifies the samples.",
    }.get(fault, link)
    (vault / referrer).write_text(f"# Field log\n\n{original_reference}\n", encoding="utf-8")
    freshness.seed(vault, "vault", (
        (str(path), freshness.stat_signature(path)) for path in vault_module.walk_vault_md(vault)
    ))
    assert vault_module.prime_inbound_index(vault, eager=True)
    if fault in ("catalogue-link", "catalogue-candidates"):
        freshness.seed(vault, "kb", (
            (str(path), freshness.stat_signature(path)) for path in vault_module.walk_vault_md(vault / KB)
        ))
        lexstore.ensure_fresh(vault)
        lookup = path_mentions.find_pages(vault, [source])
        assert lookup.complete
        assert lookup.pages == ((referrer,) if fault == "catalogue-link" else ())
        find.shared_resolver(vault)
    stop = threading.Event()
    monkeypatch.setattr(file_watcher, "observing_inbound_recovery_stop", lambda _: stop)
    mcp = server.build_server(require_auth=False)
    expired = threading.Event()

    class Budget(request_budget.RequestBudget):
        def remaining(self):
            return 0.0 if expired.is_set() else super().remaining()

    monkeypatch.setattr(request_budget, "RequestBudget", Budget)
    if fault in ("release", "render"):
        target, name = (egress, "postfilter") if fault == "release" else (FunctionTool, "convert_result")
        convert = getattr(target, name)

        def expire(*args, **kwargs):
            expired.set()
            return convert(*args, **kwargs)

        monkeypatch.setattr(target, name, expire)
    elif fault in ("removed-link", "changed-source", "catalogue-link"):
        plan = move_file.plan_inbound_rewrites

        def edit_during_planning(*args, **kwargs):
            # Hold watcher delivery back, so the planner sees the edit before the cache does.
            changed = source if fault == "changed-source" else referrer
            body = link if fault == "catalogue-link" else "# Edited while planning"
            (vault / changed).write_text(f"{body}\n", encoding="utf-8")
            return plan(*args, **kwargs)

        monkeypatch.setattr(move_file, "plan_inbound_rewrites", edit_during_planning)
    elif fault == "catalogue-candidates":
        find_pages = path_mentions.find_pages

        def publish_prose_edit_after_lookup(*args, **kwargs):
            lookup = find_pages(*args, **kwargs)
            assert lookup.complete and not lookup.pages
            page = vault / referrer
            page.write_text(f"See `{source}`.\n", encoding="utf-8")
            freshness.on_files_changed(vault, changed=[page])
            vault_module.on_inbound_files_changed(vault, changed_rels=[referrer], deleted_rels=[])
            return lookup

        monkeypatch.setattr(path_mentions, "find_pages", publish_prose_edit_after_lookup)

    async def exercise() -> None:
        async with Client(mcp, timeout=5) as client:
            if fault == "cold-resolver":
                find._RESOLVER_CACHE.pop(vault, None)
            call = public_call("manage_memory_file", {
                "operation": "propose-reclassification", "path": source, "source_kind": "dataset-export",
            })
            retry = await _call(client, *call)
            assert retry.get("error", {}).get("code") == "RECLASSIFICATION_PREVIEW_WARMING", retry
            assert "references" not in retry and "preview_digest" not in retry
            if fault == "render":
                assert retry["error"]["retry"]["tool"] == "inspect_memory"
            if fault == "cold-resolver":
                assert await asyncio.to_thread(find.await_recall_resolver_warm)
                preview = await _call(client, *call)
                assert preview["referrers"]["pointer_repointed"] == [referrer]
                assert preview["preview_digest"]

    try:
        asyncio.run(exercise())
    finally:
        stop.set()


@pytest.mark.parametrize("gap", ["external-pending", "new-seed", "handover"])
def test_a_live_watcher_recovers_preview_without_a_foreground_walk(vault, source_schema, monkeypatch, gap):
    from exomem import file_watcher, find, freshness
    from exomem import vault as vault_module
    from exomem.cli_ops import OpError
    from exomem.governance.principal import owner_principal, request_scope

    monkeypatch.delenv("EXOMEM_DISABLE_RESOLVER_WARM")
    source = _capture(vault, source_schema, title="Stream flow measurements").path
    entries = [(str(path), freshness.stat_signature(path)) for path in vault_module.walk_vault_md(vault)]
    freshness.seed(vault, "vault", entries)
    stop = threading.Event()
    monkeypatch.setattr(file_watcher, "observing_inbound_recovery_stop", lambda _: None if stop.is_set() else stop)
    assert vault_module.prime_inbound_index(vault, eager=True)
    if gap == "external-pending":
        freshness.mark_external_pending(vault)
    elif gap == "new-seed":
        freshness.seed(vault, "vault", entries)
    else:
        lookup = vault_module.cached_inbound_wikilinks

        def stop_during_lookup(*args, **kwargs):
            inbound = lookup(*args, **kwargs)
            stop.set()
            return inbound

        monkeypatch.setattr(vault_module, "cached_inbound_wikilinks", stop_during_lookup)

    builds = []
    original_build = vault_module._build_inbound_index

    def counted_build(root, **kwargs):
        builds.append(threading.current_thread().name)
        return original_build(root, **kwargs)

    monkeypatch.setattr(vault_module, "_build_inbound_index", counted_build)
    with request_scope(owner_principal(surface="mcp")), pytest.raises(OpError) as retry:
        rc.propose(vault, source, source_kind="dataset-export")
    assert retry.value.code == "RECLASSIFICATION_PREVIEW_WARMING"
    assert find.await_recall_resolver_warm()
    assert builds == (["exomem-inbound-index-warm"] if gap == "new-seed" else [])


# ---------------------------------------------------------------------------
# The correction itself
# ---------------------------------------------------------------------------
def test_a_fallback_capture_is_corrected_to_a_real_kind(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    captured = _capture(vault, source_schema, title="Airfare notes", domain="travel")
    assert captured.path.startswith(f"{KB}/Sources/Unclassified/Travel/")

    result = rc.reclassify(
        vault,
        path=captured.path,
        source_kind="research-report",
        reason="it is a written investigation, not unclassified material",
        today=TODAY,
    )

    assert result.new_path == f"{KB}/Sources/Reports/Travel/2026-08-18-airfare-notes.md"
    assert result.relocated is True
    assert not (vault / captured.path).exists()
    front = _front(vault, result.new_path)
    assert front["source_type"] == "research-report"
    assert front["domain"] == "travel"


def test_a_domain_is_corrected_without_touching_the_kind(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    captured = _capture(
        vault, source_schema, title="Kelp survey", source_type="research-report",
        domain="travel",
    )
    result = rc.reclassify(
        vault, path=captured.path, domain="marine-biology",
        reason="the subject is the survey, not the trip", today=TODAY,
    )
    assert result.kind == "research-report"
    front = _front(vault, result.new_path)
    assert front["source_type"] == "research-report"
    assert front["domain"] == "marine-biology"
    assert result.new_path.startswith(f"{KB}/Sources/Reports/Marine Biology/")


def test_a_correction_that_does_not_change_the_projection_moves_nothing(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    captured = _capture(
        vault, source_schema, title="Order confirmation", source_type="invoice-receipt",
        domain="equipment",
    )
    result = rc.reclassify(
        vault, path=captured.path, source_kind="invoice-receipt",
        reason="confirming the existing classification after review", today=TODAY,
    )
    assert result.relocated is False
    assert result.new_path == captured.path
    assert (vault / captured.path).exists()
    front = _front(vault, result.new_path)
    assert front["source_type"] == "invoice-receipt"
    assert front["domain"] == "equipment"
    assert front["reclassified_reason"] == (
        "confirming the existing classification after review"
    )
    event = front["classification_history"]["events"][0]
    assert event["before"] == event["after"]
    assert event["reason"] == "confirming the existing classification after review"


def test_an_unseen_kind_registers_itself_like_a_capture_does(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    captured = _capture(vault, source_schema, title="Hedgerow notes", domain="travel")
    result = rc.reclassify(
        vault, path=captured.path, source_kind="field-notebook",
        reason="handwritten observations recorded on site", today=TODAY,
    )
    registry = yaml.safe_load(st.registry_path(vault).read_text(encoding="utf-8"))
    assert "field-notebook" in registry["source_kinds"]
    assert result.new_path.startswith(f"{KB}/Sources/Field Notebook/Travel/")


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------
def test_a_correction_with_nothing_to_correct_is_refused(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    captured = _capture(vault, source_schema, title="Loose item", domain="media")
    with pytest.raises(rc.ReclassifyError) as exc:
        rc.reclassify(vault, path=captured.path, reason="tidying", today=TODAY)
    assert exc.value.code == "NO_CHANGE_REQUESTED"
    assert (vault / captured.path).exists()


def test_a_correction_without_a_reason_is_refused(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    captured = _capture(vault, source_schema, title="Loose item", domain="media")
    for reason in (None, "", "   "):
        with pytest.raises(rc.ReclassifyError) as exc:
            rc.reclassify(
                vault, path=captured.path, source_kind="research-report",
                reason=reason, today=TODAY,
            )
        assert exc.value.code == "REASON_REQUIRED"
    assert _front(vault, captured.path)["source_type"] == "unclassified"


@pytest.mark.parametrize("unchosen", ["other", "unclassified"])
def test_a_correction_cannot_file_a_source_under_no_chosen_kind(
    vault: Path, source_schema: schema_module.SourceSchema, unchosen: str
) -> None:
    captured = _capture(
        vault, source_schema, title="Field log", source_type="field-notebook"
    )
    before = (vault / captured.path).read_bytes()

    with pytest.raises(rc.ReclassifyError) as preview:
        rc.propose(vault, captured.path, source_kind=unchosen)
    with pytest.raises(rc.ReclassifyError) as correction:
        rc.reclassify(
            vault, path=captured.path, source_kind=unchosen,
            reason="unsure what it is", today=TODAY,
        )

    assert preview.value.code == correction.value.code == "SOURCE_KIND_REQUIRED"
    assert (vault / captured.path).read_bytes() == before


@pytest.mark.parametrize(
    "hostile",
    [
        "../../escape", "/absolute", "travel/reports", "travel\\reports", "..", ".",
        "", "   ", "con", "nul", "a" * 80, "\u0000null", "trailing. ",
    ],
)
def test_an_unsafe_corrected_value_never_reaches_a_path(
    vault: Path, source_schema: schema_module.SourceSchema, hostile: str
) -> None:
    """Refused, or normalized into a safe key — never a path segment as supplied.

    The taxonomy normalizes rather than refuses whenever a safe canonical key
    survives, so asserting refusal would test the wrong property. The invariant
    that matters is that nothing hostile reaches the filesystem.
    """
    captured = _capture(vault, source_schema, title="Loose item", domain="media")
    try:
        result = rc.reclassify(
            vault, path=captured.path, source_kind=hostile,
            reason="attempting an unsafe value", today=TODAY,
        )
    except rc.ReclassifyError as error:
        assert error.code == "INVALID_CLASSIFICATION"
        assert (vault / captured.path).exists()
        assert _front(vault, captured.path)["source_type"] == "unclassified"
        return

    assert result.new_path.startswith(f"{KB}/Sources/")
    assert ".." not in result.new_path
    assert "\\" not in result.new_path
    assert result.new_path.count("/") == 4  # KB/Sources/<Kind>/<Domain>/<file>
    landed = (vault / result.new_path).resolve()
    assert landed.is_file()
    assert landed.is_relative_to((vault / KB / "Sources").resolve())


def test_a_compiled_note_is_refused_because_it_carries_no_source_kind(
    vault: Path,
) -> None:
    note = vault / KB / "Notes" / "Insights" / "example.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("---\ntype: insight\n---\n\n# Example\n", encoding="utf-8")
    with pytest.raises(rc.ReclassifyError) as exc:
        rc.reclassify(
            vault, path=f"{KB}/Notes/Insights/example.md", source_kind="article",
            reason="wrong tree", today=TODAY,
        )
    assert exc.value.code == "NOT_A_SOURCE"


def test_a_missing_source_is_refused(vault: Path) -> None:
    with pytest.raises(rc.ReclassifyError) as exc:
        rc.reclassify(
            vault, path=f"{KB}/Sources/Other/nope.md", source_kind="article",
            reason="does not exist", today=TODAY,
        )
    assert exc.value.code == "NOT_FOUND"


# ---------------------------------------------------------------------------
# Immutability — the body and the provenance
# ---------------------------------------------------------------------------
def test_the_body_survives_a_correction_byte_identically(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    captured = _capture(
        vault, source_schema, title="Airfare notes", domain="travel",
        content="First paragraph.\n\n- a bullet\n- another\n\nClosing line.",
    )
    before = _body(vault, captured.path)
    result = rc.reclassify(
        vault, path=captured.path, source_kind="research-report",
        reason="a written investigation", today=TODAY,
    )
    assert _body(vault, result.new_path) == before


def test_a_crlf_source_is_corrected_and_keeps_its_line_endings(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    captured = _capture(vault, source_schema, title="Airfare notes", domain="travel")
    page = vault / captured.path
    page.write_bytes(page.read_bytes().replace(b"\n", b"\r\n"))

    result = rc.reclassify(
        vault, path=captured.path, source_kind="research-report",
        reason="a written investigation", today=TODAY,
    )

    raw = (vault / result.new_path).read_bytes()
    assert b"\n" not in raw.replace(b"\r\n", b"")
    from exomem.vault import parse_frontmatter

    front, _, _ = parse_frontmatter(raw.decode("utf-8"))
    assert front["source_type"] == "research-report"
    assert front["reclassified_reason"] == "a written investigation"


def test_identity_and_provenance_survive_a_correction(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    captured = _capture(
        vault, source_schema, title="Airfare notes", domain="travel",
        url="https://example.com/fares", tags=["fares"],
        why_captured="baseline for later comparison",
    )
    before = _front(vault, captured.path)
    result = rc.reclassify(
        vault, path=captured.path, source_kind="research-report",
        reason="a written investigation", today=TODAY,
    )
    after = _front(vault, result.new_path)
    for preserved in ("exomem_id", "title", "captured", "url", "tags", "ingested_into"):
        assert after[preserved] == before[preserved], preserved


def test_the_correction_records_its_reason_and_previous_path(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    captured = _capture(vault, source_schema, title="Airfare notes", domain="travel")
    result = rc.reclassify(
        vault, path=captured.path, source_kind="research-report",
        reason="it is a written investigation", today=TODAY,
    )
    after = _front(vault, result.new_path)
    assert str(after["reclassified"]) == TODAY.isoformat()
    # The entry names the correction's operation, never the pages it repointed.
    assert after["moved_from"] == [
        {"path": captured.path, "kind": "unclassified", "domain": "travel", "operation": ANY}
    ]
    assert "reclassified_from" not in after
    assert after["reclassified_reason"] == "it is a written investigation"


def test_a_correction_that_does_not_relocate_records_no_previous_path(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    captured = _capture(
        vault, source_schema, title="Order confirmation",
        source_type="invoice-receipt", domain="equipment",
    )
    result = rc.reclassify(
        vault, path=captured.path, source_kind="invoice-receipt",
        reason="confirmed on review", today=TODAY,
    )
    assert "moved_from" not in _front(vault, result.new_path)


# ---------------------------------------------------------------------------
# References follow the source
# ---------------------------------------------------------------------------
def test_inbound_references_follow_the_source(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    captured = _capture(vault, source_schema, title="Airfare notes", domain="travel")
    stem = captured.path.removesuffix(".md")

    citing = vault / KB / "Notes" / "Insights" / "fares-conclusion.md"
    citing.parent.mkdir(parents=True, exist_ok=True)
    citing.write_text(
        f'---\ntype: insight\nsources:\n  - "[[{stem}]]"\n---\n\n'
        f"# Fares conclusion\n\nSee [[{stem}]] for the raw material.\n\n"
        f"## Observations\n\n- [finding] Fares varied by booking window #fares\n",
        encoding="utf-8",
    )

    result = rc.reclassify(
        vault, path=captured.path, source_kind="research-report",
        reason="a written investigation", today=TODAY,
    )

    text = citing.read_text(encoding="utf-8")
    new_stem = result.new_path.removesuffix(".md")
    assert stem not in text
    assert text.count(new_stem) == 2
    assert result.references_updated >= 2


def test_the_sources_index_reference_is_rewritten_too(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    captured = _capture(vault, source_schema, title="Airfare notes", domain="travel")
    result = rc.reclassify(
        vault, path=captured.path, source_kind="research-report",
        reason="a written investigation", today=TODAY,
    )
    index = (vault / KB / "Sources" / "index.md").read_text(encoding="utf-8")
    assert captured.path.removesuffix(".md") not in index
    assert result.new_path.removesuffix(".md") in index


# ---------------------------------------------------------------------------
# Proposals report evidence and decline rather than guess
# ---------------------------------------------------------------------------
def test_a_proposal_writes_nothing(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    captured = _capture(vault, source_schema, title="Loose item", domain="media")
    before = (vault / captured.path).read_text(encoding="utf-8")
    proposal = rc.propose(vault, captured.path)
    assert proposal.path == captured.path
    assert (vault / captured.path).read_text(encoding="utf-8") == before


def test_a_domain_already_in_the_location_is_proposed_with_its_evidence(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    """A legacy source filed under `Other/<Domain>/` never got a `domain:` field."""
    legacy = vault / KB / "Sources" / "Other" / "Travel" / "2026-01-02-legacy.md"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(
        "---\ntype: source\ntitle: Legacy item\nsource_type: other\n"
        "captured: 2026-01-02T00:00:00Z\ntags: []\ningested_into: []\n---\n\n"
        "# Legacy item\n\nBody.\n",
        encoding="utf-8",
    )
    proposal = rc.propose(vault, f"{KB}/Sources/Other/Travel/2026-01-02-legacy.md")
    assert proposal.current_domain is None
    assert proposal.proposed_domain == "travel"
    assert any("Travel" in item for item in proposal.domain_evidence)


def test_an_undecidable_kind_is_reported_not_guessed(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    captured = _capture(vault, source_schema, title="Loose item", domain="media")
    proposal = rc.propose(vault, captured.path)
    assert proposal.proposed_kind is None
    assert (proposal.proposed_kind or "") not in st.UNCHOSEN_KINDS
    assert any("no kind is proposed" in item for item in proposal.kind_evidence)


def test_a_caller_previews_the_correction_it_has_decided_on(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    """The agent reads the source and decides; the preview is what it shows first.

    Without this the read-only mode can only preview its own (usually empty)
    proposal, so an agent that has judged the kind has no way to show the user
    where the file lands before writing -- which makes propose-then-confirm
    unusable for exactly the case it exists to serve.
    """
    captured = _capture(vault, source_schema, title="Airfare notes", domain="travel")
    before = (vault / captured.path).read_text(encoding="utf-8")

    proposal = rc.propose(vault, captured.path, source_kind="research-report")

    assert proposal.proposed_kind == "research-report"
    assert proposal.destination == f"{KB}/Sources/Reports/Travel/2026-08-18-airfare-notes.md"
    assert proposal.relocation_required is True
    assert "supplied by the caller" in proposal.kind_evidence
    assert (vault / captured.path).read_text(encoding="utf-8") == before


def test_a_previewed_domain_is_canonicalized_not_echoed(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    captured = _capture(vault, source_schema, title="Kelp survey", source_type="research-report")

    proposal = rc.propose(vault, captured.path, domain="Marine Biology")

    assert proposal.proposed_domain == "marine-biology"
    assert proposal.destination is not None
    assert proposal.destination.startswith(f"{KB}/Sources/Reports/Marine Biology/")


def test_a_previewed_destination_is_never_an_escape_hatch(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    """A hostile supplied value must not become a previewed path.

    The taxonomy normalizes rather than refuses whenever a safe canonical key
    survives, so the property to bind is the one that matters: whatever a caller
    supplies, the destination it is shown stays inside the source tree at the
    projection's fixed depth. Otherwise the preview is the place a traversal
    reaches the user as an approved-looking suggestion.
    """
    captured = _capture(vault, source_schema, title="Loose item", domain="media")

    for hostile in ("../../escape", "..", "/etc", "a\\b", "travel/reports"):
        try:
            proposal = rc.propose(vault, captured.path, source_kind=hostile)
        except st.TaxonomyError:
            continue  # refused outright is the other acceptable outcome
        destination = proposal.destination
        if destination is None:
            continue
        assert destination.startswith(f"{KB}/Sources/")
        assert ".." not in destination.split("/")
        assert destination.count("/") == 4
        assert (vault / destination).resolve().is_relative_to((vault / KB).resolve())


def test_the_preview_is_reachable_through_the_operation(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    from exomem import commands

    captured = _capture(vault, source_schema, title="Airfare notes", domain="travel")
    out = commands.op_manage_memory_file(
        vault,
        operation="propose-reclassification",
        path=captured.path,
        source_kind="research-report",
    )
    assert out["proposed_kind"] == "research-report"
    assert out["relocation_required"] is True


def test_a_proposal_reports_how_many_references_would_move(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    captured = _capture(vault, source_schema, title="Airfare notes", domain="travel")
    proposal = rc.propose(vault, captured.path)
    # The sources index links every capture, so at least one reference exists.
    assert proposal.references >= 1


# ---------------------------------------------------------------------------
# Never automatic
# ---------------------------------------------------------------------------
def test_capturing_more_material_relocates_nothing(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    first = _capture(vault, source_schema, title="Loose one", domain="media")
    for index in range(3):
        _capture(vault, source_schema, title=f"Loose {index}", domain="media")
    assert (vault / first.path).exists()
    assert _front(vault, first.path)["source_type"] == "unclassified"


def test_a_registry_path_label_rename_migrates_nothing(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    captured = _capture(
        vault, source_schema, title="Report", source_type="research-report",
        domain="travel",
    )
    st.registry_path(vault).write_text(
        "schema_version: 1\nsource_kinds:\n  research-report:\n"
        "    path_label: Investigations\ndomains: {}\n",
        encoding="utf-8",
    )
    assert (vault / captured.path).exists()
    assert captured.path.startswith(f"{KB}/Sources/Reports/")
    # The rename only takes effect for material corrected or captured afterwards.
    result = rc.reclassify(
        vault, path=captured.path, source_kind="research-report",
        reason="adopting the renamed folder", today=TODAY,
    )
    assert result.new_path.startswith(f"{KB}/Sources/Investigations/Travel/")


# ---------------------------------------------------------------------------
# The product surface
# ---------------------------------------------------------------------------
def test_the_correction_is_reachable_through_the_governed_file_operation(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    """No 30th tool: the file-lifecycle operation already owns governed moves."""
    from exomem import commands

    captured = _capture(vault, source_schema, title="Airfare notes", domain="travel")
    out = commands.op_manage_memory_file(
        vault,
        operation="reclassify",
        path=captured.path,
        source_kind="research-report",
        reason="a written investigation",
    )
    assert out["source_type"] == "research-report"
    assert out["path"].startswith(f"{KB}/Sources/Reports/Travel/")
    assert out["relocated"] is True


def test_the_proposal_is_reachable_and_writes_nothing(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    from exomem import commands

    captured = _capture(vault, source_schema, title="Loose item", domain="media")
    before = (vault / captured.path).read_text(encoding="utf-8")
    out = commands.op_manage_memory_file(
        vault, operation="propose-reclassification", path=captured.path
    )
    assert out["proposed_kind"] is None
    assert (vault / captured.path).read_text(encoding="utf-8") == before


def test_a_refusal_surfaces_its_code_through_the_operation(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    from exomem import commands

    captured = _capture(vault, source_schema, title="Loose item", domain="media")
    with pytest.raises(ValueError, match="REASON_REQUIRED"):
        commands.op_manage_memory_file(
            vault, operation="reclassify", path=captured.path,
            source_kind="research-report",
        )


def test_an_unknown_operation_still_names_the_new_ones(vault: Path) -> None:
    from exomem import commands

    with pytest.raises(ValueError, match="reclassify"):
        commands.op_manage_memory_file(vault, operation="nonsense")


def test_both_operations_are_release_covered_selectors() -> None:
    """A new `operation=` value is invisible to the leaf tests above.

    Every other test here calls `op_manage_memory_file` directly, which is not
    the path a client takes. The shared dispatcher classifies the invocation
    first, and an unregistered selector is refused with `RECEIPT_OUTCOME_MISSING`
    before the leaf ever runs -- so the operation can be fully implemented,
    fully tested, and still unreachable from MCP, REST, hosted, and the CLI
    alike. That is how this shipped broken once; this binds the registration.
    """
    from exomem.commands import invocation_is_read_only
    from exomem.product_invoke import product_command

    command = product_command("manage_memory_file")

    assert invocation_is_read_only(command, {"operation": "propose-reclassification"}) is True
    assert invocation_is_read_only(command, {"operation": "reclassify"}) is False


def test_a_correction_that_registers_new_vocabulary_says_so(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    """A correction is where a typo is most likely, so registration must be visible.

    The caller is naming a kind it just decided on, with no prior capture to
    compare against. Registering it silently is how a mistyped kind becomes a
    permanent category, so the correction reports it the same way capture does.
    """
    captured = _capture(vault, source_schema, title="Hedgerow notes", domain="media")

    result = rc.reclassify(
        vault,
        path=captured.path,
        source_kind="field-notebook",
        reason="A running observational log is a field notebook.",
        today=TODAY,
    )

    assert any(
        warning.startswith("NEW_SOURCE_KIND: registered 'field-notebook'")
        for warning in result.warnings
    ), result.warnings
    assert all(isinstance(warning, str) for warning in result.warnings)
    assert result.as_dict()["warnings"] == list(result.warnings)


def test_a_correction_into_known_vocabulary_stays_quiet(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    captured = _capture(vault, source_schema, title="Hedgerow notes", domain="media")
    lexstore.ensure_fresh(vault)

    result = rc.reclassify(
        vault,
        path=captured.path,
        source_kind="research-report",
        reason="A written investigation.",
        today=TODAY,
    )

    assert result.warnings == ()
    assert "warnings" not in result.as_dict()


# ---------------------------------------------------------------------------
# The episode kind is reserved in both directions
# ---------------------------------------------------------------------------
def _recap(vault: Path, source_schema: schema_module.SourceSchema) -> add_module.AddResult:
    return _capture(
        vault,
        source_schema,
        title="Harbor Lamp purchase",
        source_type=st.EPISODE_KIND,
        slug="harbor-lamp-purchase-epa1a1a1a1a1a1-20260818t091233000000-dddddddd",
        content="### Worked on\n\n- Compared two lamps for Project Alpha",
        extra_frontmatter={
            "summary": "Chose the brass lamp.",
            "episode": "ep-" + "a1" * 16,
            "episode_digest": "d" * 64,
        },
    )


def test_a_capture_cannot_be_reclassified_into_the_episode_kind(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    """A recap is only ever what `episode_memory` recorded and bound."""
    captured = _capture(
        vault, source_schema, title="Lamp report", source_type="research-report"
    )
    before = (vault / captured.path).read_bytes()

    for call in (
        lambda: rc.propose(vault, captured.path, source_kind=st.EPISODE_KIND),
        lambda: rc.reclassify(
            vault, path=captured.path, source_kind="episodes", reason="probe", today=TODAY
        ),
    ):
        with pytest.raises(rc.ReclassifyError) as error:
            call()
        assert error.value.code == "EPISODE_KIND_RESERVED"

    assert (vault / captured.path).read_bytes() == before
    assert not (vault / KB / "Sources" / "Episodes").exists()


@pytest.mark.parametrize(
    "change", [{"source_kind": "research-report"}, {"domain": "travel"}], ids=["kind", "domain"]
)
def test_a_recap_cannot_be_reclassified_out_of_the_episode_folder(
    vault: Path, source_schema: schema_module.SourceSchema, change: dict
) -> None:
    """Out of the kind, or into a domain subfolder: either way the recap leaves
    the one folder its revisions are found in."""
    recap = _recap(vault, source_schema)
    before = (vault / recap.path).read_bytes()

    with pytest.raises(rc.ReclassifyError) as proposed:
        rc.propose(vault, recap.path, **change)
    with pytest.raises(rc.ReclassifyError) as applied:
        rc.reclassify(vault, path=recap.path, reason="probe", today=TODAY, **change)

    assert proposed.value.code == applied.value.code == "EPISODE_KIND_RESERVED"
    assert (vault / recap.path).read_bytes() == before


# ---------------------------------------------------------------------------
# Draining legacy sources: append-only referrers, history and revert
# ---------------------------------------------------------------------------
def _legacy_source(vault: Path, name: str = "2026-01-02-tide-table") -> str:
    """A source filed under the retired catch-all, as the legacy drain finds it."""
    rel = f"{KB}/Sources/Other/{name}.md"
    page = vault / rel
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        "---\ntype: source\ntitle: Tide table\nsource_type: other\n"
        "captured: 2026-01-02T00:00:00Z\n"
        "# kept exactly as the capture wrote it\n"
        "tags: [ harbor,  tides ]\ningested_into: []\n---\n\n"
        "# Tide table\n\nHigh water 06:12, low water 12:31.\n",
        encoding="utf-8",
    )
    return rel


def _write(vault: Path, rel: str, text: str) -> Path:
    page = vault / rel
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(text, encoding="utf-8")
    return page


def test_a_legacy_source_is_previewed_moved_twice_and_reverted_through_the_operation(
    vault: Path,
) -> None:
    """The drain workflow, through the dispatcher every client uses.

    An Evidence page names the source by basename, alias and heading, which
    resolve unchanged after a move that keeps the basename, so it must neither
    be rewritten nor refuse the move. A mutable note names it by path and
    follows it there and back.
    """
    from exomem import commands, writer_lease

    command = next(c for c in commands.PRODUCT_COMMANDS if c.name == "manage_memory_file")
    source = _legacy_source(vault)
    stem = "2026-01-02-tide-table"
    original = (vault / source).read_text(encoding="utf-8")
    evidence = _write(
        vault,
        f"{KB}/Evidence/Harbor/tide-proof.md",
        f"---\ntype: evidence\n---\n\nTables: [[{stem}]], [[{stem}|the tides]], "
        f"[[{stem}#Tide table]].\n",
    )
    evidence_bytes = evidence.read_bytes()
    note = _write(
        vault,
        f"{KB}/Notes/Insights/tide-note.md",
        f"---\ntype: insight\nstatus: draft\n---\n\n# Tide note\n\n"
        f"See [[{source.removesuffix('.md')}]].\n",
    )
    note_bytes = note.read_bytes()

    preview = writer_lease.invoke_command(
        command, vault, operation="propose-reclassification", path=source,
        source_kind="dataset", domain="travel",
    )
    first = writer_lease.invoke_command(
        command, vault, operation="reclassify", path=source,
        source_kind="dataset", domain="travel", reason="a table of measurements",
    )
    second = writer_lease.invoke_command(
        command, vault, operation="reclassify", path=first["path"],
        source_kind="research-report", reason="the table is an appendix of a survey",
    )
    history = _front(vault, second["path"])["moved_from"]
    note_after_moves = note.read_text(encoding="utf-8")
    back_once = writer_lease.invoke_command(
        command, vault, operation="revert-reclassification", path=second["path"],
        reason="the survey attribution was wrong",
    )
    back_twice = writer_lease.invoke_command(
        command, vault, operation="revert-reclassification", path=back_once["path"],
        reason="keep it in the drain queue for now",
    )

    assert preview["destination"] == first["path"]
    assert preview["referrers"] == {
        "rewritten": [f"{KB}/Notes/Insights/tide-note.md"],
        "pointer_repointed": [],
        "append_only_unchanged": {"basename": [f"{KB}/Evidence/Harbor/tide-proof.md"]},
    }
    assert preview["refusals"] == []
    assert history == [
        {"path": source, "kind": "other", "domain": None, "operation": ANY},
        {"path": first["path"], "kind": "dataset-export", "domain": "travel", "operation": ANY},
    ]
    assert second["path"].removesuffix(".md") in note_after_moves
    assert back_once["path"] == first["path"]
    assert back_twice["path"] == source
    assert evidence.read_bytes() == evidence_bytes
    assert note.read_bytes() == note_bytes
    restored = (vault / source).read_text(encoding="utf-8")
    assert _body(vault, source) == original.split("---\n", 2)[2].removeprefix("\n")
    front_lines = restored.split("---\n", 2)[1].splitlines()
    # Only the correction record remains; every original byte is kept in place.
    for line in original.split("---\n", 2)[1].splitlines():
        assert line in front_lines
    # Each revert removed the entry it restored, so no entry names a path the source left.
    assert "moved_from" not in _front(vault, source)
    assert "reclassified_from" not in _front(vault, source)
    released = commands.op_get(vault, path=source, frontmatter_only=True)
    journal = released["frontmatter"]["classification_history"]
    assert journal["coverage"] == "retained-only"
    events = journal["events"]
    assert [event["operation"] for event in events] == [
        "reclassify", "reclassify", "revert", "revert",
    ]
    assert [event["reason"] for event in events] == [
        "a table of measurements", "the table is an appendix of a survey",
        "the survey attribution was wrong", "keep it in the drain queue for now",
    ]
    assert events[0]["before"] == events[-1]["after"]
    for earlier, later in zip(events, events[1:], strict=False):
        assert earlier["after"] == later["before"]
    assert all(dt.datetime.fromisoformat(event["at"]).utcoffset() == dt.timedelta(0)
               for event in events)


def test_an_evidence_path_link_refuses_the_move_and_nothing_changes(vault: Path) -> None:
    """A path-form link in an append-only page would dangle, so the source stays."""
    source = _legacy_source(vault)
    evidence = _write(
        vault,
        f"{KB}/Evidence/Harbor/tide-proof.md",
        f"---\ntype: evidence\n---\n\nTable: [[{source.removesuffix('.md')}]].\n",
    )
    paths = (source, evidence.relative_to(vault).as_posix())
    before = {rel: (vault / rel).read_bytes() for rel in paths}

    proposal = rc.propose(vault, source, source_kind="dataset", domain="travel")
    with pytest.raises(rc.ReclassifyError) as refused:
        rc.reclassify(
            vault, path=source, source_kind="dataset", domain="travel",
            reason="a table of measurements", today=TODAY,
        )

    assert [(item["code"], item["path"]) for item in proposal.refusals] == [
        ("APPEND_ONLY", f"{KB}/Evidence/Harbor/tide-proof.md")
    ]
    assert refused.value.code == "APPEND_ONLY"
    assert {rel: (vault / rel).read_bytes() for rel in before} == before
    assert not (vault / proposal.destination).exists()


def test_a_legacy_previous_path_is_kept_but_never_reverted_by_guessing(vault: Path) -> None:
    """A move that rewrites a legacy `reclassified_from` value instead of keeping its bytes, or a
    revert that guesses the classification a path-only entry left out."""
    source = _legacy_source(vault)
    page = vault / source
    legacy_path = f"{KB}/Sources/Unclassified/2026-01-02-tide-table.md"
    page.write_text(
        page.read_text(encoding="utf-8").replace(
            "ingested_into: []\n", f"ingested_into: []\nreclassified_from: {legacy_path}\n"
        ),
        encoding="utf-8",
    )
    before = page.read_bytes()

    with pytest.raises(rc.ReclassifyError) as refused:
        rc.revert(vault, path=source, reason="undo", today=TODAY)
    assert refused.value.code == "HISTORY_CLASSIFICATION_UNKNOWN"
    assert page.read_bytes() == before

    moved = rc.reclassify(
        vault, path=source, source_kind="dataset", domain="travel",
        reason="a table of measurements", today=TODAY,
    )
    legacy_line = f"reclassified_from: {legacy_path}\n"
    assert legacy_line in (vault / moved.new_path).read_text(encoding="utf-8")
    assert _front(vault, moved.new_path)["moved_from"] == [
        {"path": source, "kind": "other", "domain": None, "operation": ANY},
    ]
    reverted = rc.revert(vault, path=moved.new_path, reason="undo", today=TODAY)
    assert reverted.new_path == source
    assert legacy_line in page.read_text(encoding="utf-8")
    assert "moved_from" not in _front(vault, source)


def test_a_revert_refuses_a_previous_location_another_page_now_occupies(
    vault: Path,
) -> None:
    source = _legacy_source(vault)
    moved = rc.reclassify(
        vault, path=source, source_kind="dataset", domain="travel",
        reason="a table of measurements", today=TODAY,
    )
    _write(vault, source, "---\ntype: source\ntitle: A newer capture\n---\n\nOther.\n")
    before = (vault / moved.new_path).read_bytes()

    with pytest.raises(rc.ReclassifyError) as refused:
        rc.revert(vault, path=moved.new_path, reason="undo", today=TODAY)

    assert refused.value.code == "PREVIOUS_PATH_OCCUPIED"
    assert (vault / moved.new_path).read_bytes() == before


def test_a_reclassified_artifact_page_follows_its_bytes_and_keeps_its_other_fields(
    vault: Path,
) -> None:
    """The companion pointer is patched in place, not by re-serializing the page."""
    binary = f"{KB}/Sources/Other/scan.png"
    (vault / binary).parent.mkdir(parents=True, exist_ok=True)
    (vault / binary).write_bytes(b"\x89PNG\r\n\x1a\nbytes")
    page_rel = f"{binary}.md"
    original = (
        "---\ntype: source\ntitle: 'Harbour scan'\nsource_type: other\n"
        f"evidence_file: {binary}\n"
        "# scanned at the harbour office\n"
        "tags: [ harbor ]\n---\n\n# Harbour scan\n\nA scanned tide board.\n"
    )
    _write(vault, page_rel, original)

    result = rc.reclassify(
        vault, path=page_rel, source_kind="photograph", domain="travel",
        reason="it is a photograph of the tide board", today=TODAY,
    )

    new_binary = result.new_path.removesuffix(".md")
    assert (vault / new_binary).read_bytes() == b"\x89PNG\r\n\x1a\nbytes"
    assert _front(vault, result.new_path)["evidence_file"] == new_binary
    moved_lines = (vault / result.new_path).read_text(encoding="utf-8").splitlines()
    for line in original.splitlines():
        if not line.startswith(("source_type:", "evidence_file:")):
            assert line in moved_lines, line


def test_a_legacy_reason_is_retained_without_attributing_it_to_an_undo_entry(vault: Path) -> None:
    from exomem.record_formats import splice_markdown_frontmatter

    source = _legacy_source(vault)
    page = vault / source
    known = {
        "reclassified": "2026-01-02",
        "reclassified_reason": "confirmed the current classification after a reversal",
        "reclassified_from": [{"path": f"{KB}/Sources/Reports/earlier.md"}],
    }
    page.write_text(splice_markdown_frontmatter(page.read_text(), known))

    first = rc.reclassify(
        vault, path=source, source_kind="research-report", reason="a published survey",
        today=TODAY,
    )
    reverted = rc.revert(
        vault, path=first.new_path, reason="the attribution was wrong", today=TODAY
    )

    record = _front(vault, reverted.new_path)["classification_history"]
    assert record["legacy"] == known
    assert [event["reason"] for event in record["events"]] == [
        "a published survey", "the attribution was wrong",
    ]
    assert all("reason" not in entry for entry in record["legacy"]["reclassified_from"])


def test_malformed_event_history_refuses_without_discarding_the_source(vault: Path) -> None:
    from exomem.record_formats import splice_markdown_frontmatter

    source = _legacy_source(vault)
    page = vault / source
    page.write_text(splice_markdown_frontmatter(
        page.read_text(), {"classification_history": {"events": "unreadable history"}}
    ))
    before = page.read_bytes()

    with pytest.raises(rc.ReclassifyError, match="INVALID_HISTORY"):
        rc.reclassify(vault, path=source, source_kind="research-report", reason="a survey")

    assert page.read_bytes() == before


@pytest.mark.parametrize("relocating", [False, True], ids=["in-place", "relocated"])
def test_a_concurrent_metadata_edit_survives_a_correction(
    vault: Path, source_schema: schema_module.SourceSchema,
    monkeypatch: pytest.MonkeyPatch, relocating: bool,
) -> None:
    from exomem.record_formats import splice_markdown_frontmatter
    from exomem.vault import PathGuardError

    captured = _capture(vault, source_schema, title="Survey", source_type="research-report")
    page = vault / captured.path
    competing = splice_markdown_frontmatter(page.read_text(), {"tags": ["independent-update"]})
    destination = rc.propose(vault, captured.path, source_kind="photograph").destination
    commit = rc._write_correction

    def concurrent_write(*args, **kwargs):
        page.write_text(competing)
        return commit(*args, **kwargs)

    monkeypatch.setattr(rc, "_write_correction", concurrent_write)

    with pytest.raises((rc.ReclassifyError, PathGuardError)):
        rc.reclassify(
            vault, path=captured.path,
            source_kind="photograph" if relocating else "research-report",
            reason="reviewed the captured artifact", today=TODAY,
        )

    assert page.read_text() == competing
    assert "classification_history" not in _front(vault, captured.path)
    assert destination is not None and not (vault / destination).exists()


def test_a_failed_companion_move_transaction_commits_no_event(
    vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = f"{KB}/Sources/Other/survey.png"
    raw = vault / binary
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(b"\x89PNG\r\n\x1a\noriginal")
    source = f"{binary}.md"
    _write(vault, source,
           f"---\ntype: source\nsource_type: other\nevidence_file: {binary}\n---\n\nA survey.\n")
    before = (vault / source).read_bytes()
    destination = rc.propose(vault, source, source_kind="photograph").destination

    def interrupted(*args, **kwargs):
        raise OSError("injected transaction interruption")

    monkeypatch.setattr(rc.move_file_module, "batch_atomic_write", interrupted)

    with pytest.raises(OSError, match="injected transaction interruption"):
        rc.reclassify(vault, path=source, source_kind="photograph", reason="a scanned survey")

    assert (vault / source).read_bytes() == before
    assert raw.read_bytes() == b"\x89PNG\r\n\x1a\noriginal"
    assert destination is not None and not (vault / destination).exists()


def test_an_edit_after_preflight_survives_the_failed_relocation(
    vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import commands
    from exomem.record_formats import splice_markdown_frontmatter

    source = _legacy_source(vault)
    page = vault / source
    edited = splice_markdown_frontmatter(page.read_text(), {"tags": ["late-update"]})
    held_rename = rc.move_file_module._held_rename
    injected = False

    def edit_before_rename(root, old_path, new_path, **kwargs):
        nonlocal injected
        if old_path == source and not kwargs.get("rollback"):
            page.write_text(edited)
            injected = True
        return held_rename(root, old_path, new_path, **kwargs)

    monkeypatch.setattr(rc.move_file_module, "_held_rename", edit_before_rename)

    with pytest.raises(ValueError, match="SOURCE_CHANGED"):
        commands.op_manage_memory_file(
            vault, operation="reclassify", path=source,
            source_kind="photograph", reason="a captured image",
        )

    assert injected
    assert page.read_text() == edited
    assert "classification_history" not in _front(vault, source)


def test_a_reclassified_source_is_followed_by_prose_paths_in_notes_but_not_in_evidence(
    vault: Path,
) -> None:
    """A page that cites the source by vault path in prose must follow it, or say it stayed.

    The Note is rewritable and has to name the new location; the Evidence page is immutable, so
    it keeps its bytes, the move still goes ahead, and the preview names it as left unchanged.
    """
    source = _legacy_source(vault)
    evidence = _write(
        vault,
        f"{KB}/Evidence/Harbor/tide-proof.md",
        f"---\ntype: evidence\n---\n\nThe table is kept at `{source}`.\n",
    )
    evidence_bytes = evidence.read_bytes()
    note = _write(
        vault,
        f"{KB}/Notes/Insights/tide-note.md",
        f"---\ntype: insight\nstatus: draft\n---\n\n# Tide note\n\nThe table: `{source}`.\n"
        f"Read again from {source} on Monday.\n",
    )
    lexstore.ensure_fresh(vault)

    preview = rc.propose(vault, source, source_kind="dataset", domain="travel")
    result = rc.reclassify(
        vault, path=source, source_kind="dataset", domain="travel", reason="a table of readings"
    )

    assert preview.append_only_unchanged == {
        rc.MENTION_FORM: (f"{KB}/Evidence/Harbor/tide-proof.md",)
    }
    assert preview.rewritten == (f"{KB}/Notes/Insights/tide-note.md",)
    assert note.read_text(encoding="utf-8").count(result.new_path) == 2
    assert result.path_mentions_updated == 2
    assert any("append-only" in warning for warning in result.warnings)
    assert evidence.read_bytes() == evidence_bytes


def test_a_reclassification_says_so_when_it_could_not_check_path_mentions(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A catalogue that cannot answer must read as "not checked" in the preview and the result,
    never as "no page cites the source"."""
    # Hold the background repair back, so the catalogue stays behind for the whole correction.
    monkeypatch.setattr(lexstore, "_schedule_repair", lambda *_a, **_k: True)
    source = _legacy_source(vault)
    note = _write(
        vault,
        f"{KB}/Notes/Insights/tide-note.md",
        f"---\ntype: insight\nstatus: draft\n---\n\n# Tide note\n\nAt `{source}`.\n",
    )

    preview = rc.propose(vault, source, source_kind="dataset", domain="travel")
    result = rc.reclassify(
        vault, path=source, source_kind="dataset", domain="travel", reason="a table of readings"
    )

    assert any("path mentions were not checked" in warning for warning in preview.warnings)
    assert any("path mentions were not checked" in warning for warning in result.warnings)
    assert f"`{source}`" in note.read_text(encoding="utf-8")
