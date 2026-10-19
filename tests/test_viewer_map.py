"""The viewer sees one bounded, admitted page map with truthful coverage."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest
import yaml
from starlette.testclient import TestClient
from test_connector_boundary import SCOPE
from test_connector_boundary import configured_boundary as configured_boundary
from test_governance_egress import _external, write_rule, write_scope
from test_private_vocabulary_instances import private_instances as private_instances

from exomem import (
    commands,
    epistemic_graph,
    find,
    freshness,
    memory_refs,
    request_budget,
    server,
    vault,
)
from exomem.__main__ import main
from exomem.governance import egress
from exomem.governance.principal import library_scope, request_scope
from exomem.vocabulary import instances, registry_spec

NOTES = "Knowledge Base/Notes/Insights"


def _write(root: Path, path: str, body: str) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


def _build(root: Path) -> epistemic_graph.EpistemicGraphIndex:
    index = epistemic_graph.EpistemicGraphIndex(root)
    index.rebuild_all()
    assert index.available()  # Maintenance proves the snapshot before an interactive read.
    find.evict_resolver_caches(root)
    find.recall_resolver_snapshot(root)  # Maintenance warms the exact resolver too.
    return index


def _small(root: Path) -> None:
    _write(root, f"{NOTES}/source.md", f"---\ntype: insight\n"
           "exomem_id: 12345678-1234-4234-8234-123456789012\n---\n# Source\n\n"
           f"## Claim\n- id: observation\n- relations: supports: [[{NOTES}/target]]\n\n"
           "A supported observation.\n")
    _write(root, f"{NOTES}/target.md", "---\ntype: insight\n---\n# Target\n")
    _write(root, f"{NOTES}/isolated.md", "---\ntype: insight\n---\n# Isolated\n")
    _build(root)


def test_isolated_pages_and_unit_edges_reach_the_same_registered_leaf(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    _small(root)
    canonical = next(c for c in commands.COMMANDS if c.name == "viewer_map")
    product = next(c for c in commands.PRODUCT_COMMANDS if c.name == "viewer_map")
    assert canonical.leaf is product.leaf is commands.op_viewer_map
    assert canonical.surfaces == product.surfaces == {"mcp", "rest", "cli"}
    with library_scope():
        result = product.leaf(root)
    assert result["state"] == "complete"
    assert result["counts"] == {
        "eligible_nodes": 3, "eligible_edges": 2, "observed_nodes": 3,
        "observed_edges": 2, "drawn_nodes": 3, "drawn_edges": 2,
    }
    assert result["nodes"][0]["node_key"] == f"file:{NOTES}/target.md"
    assert result["nodes"][0]["incoming_edges"] == 2
    assert result["edges"][0]["src_key"] == f"file:{NOTES}/source.md"
    assert result["edges"][0]["dst_key"] == f"file:{NOTES}/target.md"
    assert result["snapshot"]["instance"]
    assert result["ranking_complete"] is True
    source = next(node for node in result["nodes"] if node["path"] == f"{NOTES}/source.md")
    assert source["memory_ref"] == memory_refs.memory_ref("12345678-1234-4234-8234-123456789012")


def test_output_caps_preserve_exact_totals_and_stable_keys(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    _small(root)
    with library_scope():
        exact = commands.op_viewer_map(root, max_nodes=3, max_edges=2)
        capped = commands.op_viewer_map(root, max_nodes=2, max_edges=0)
        capped_edges = commands.op_viewer_map(root, max_nodes=3, max_edges=1)
        redrawn = commands.op_viewer_map(root, max_nodes=2, max_edges=0)
    assert exact["truncation"] == []
    assert capped["state"] == "complete"
    assert capped["counts"]["eligible_nodes"] == 3
    assert capped["counts"]["eligible_edges"] == 2
    assert capped["counts"]["drawn_nodes"] == 2
    assert capped["counts"]["drawn_edges"] == 0
    assert {item["stage"] for item in capped["truncation"]} == {"output_nodes", "output_edges"}
    assert capped["nodes"] == redrawn["nodes"]
    assert capped["snapshot"] == redrawn["snapshot"]
    assert capped_edges["counts"]["eligible_edges"] == 2
    assert capped_edges["counts"]["drawn_edges"] == 1
    assert capped_edges["truncation"] == [{"stage": "output_edges", "reason": "drawing_limit", "limit": 1}]


def test_map_returns_stored_note_metadata_and_source_presentation_role(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    _write(root, f"{NOTES}/source.md", "---\ntype: insight\n"
           "created: '2026-09-01'\nupdated: '2026-10-01'\n---\n# Source\n\n"
           f"## Claim\n- relations: contradicts: [[{NOTES}/target]]\n\nA conflict.\n")
    _write(root, f"{NOTES}/target.md", "---\ntype: insight\n---\n# Target\n")
    _build(root)
    with library_scope():
        result = commands.op_viewer_map(root)
    source = next(node for node in result["nodes"] if node["path"] == f"{NOTES}/source.md")
    assert (source["page_type"], source["origin_date"], source["updated_date"]) == (
        "insight", "2026-09-01", "2026-10-01",
    )
    meaning = next(row for row in result["vocabulary"] if row["definition_key"] == "contradicts")
    assert meaning["presentation_role"] == "conflict"
    assert meaning["family"] == "contradiction"
    assert meaning["description"] == "Conflicts with"


def test_real_mcp_rest_cli_return_the_admitted_page_map(
    vault: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    root = vault
    _small(root)
    monkeypatch.setattr(server, "load_dotenv", lambda *_a, **_k: None)
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "fixture-map-key")
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(root))
    monkeypatch.setenv("EXOMEM_DISABLE_FILE_WATCHER", "1")
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "python")
    mcp = server.build_server(require_auth=False)
    denied = TestClient(mcp.http_app()).post("/api/viewer_map", json={})
    assert denied.status_code == 401
    with library_scope():
        expected = commands.op_viewer_map(root, max_nodes=2, max_edges=0)
    mcp_result = asyncio.run(mcp.call_tool(
        "viewer_map", {"max_nodes": 2, "max_edges": 0}, run_middleware=True,
    )).structured_content
    if isinstance(mcp_result, dict) and set(mcp_result) == {"result"}:
        mcp_result = mcp_result["result"]
    response = TestClient(mcp.http_app()).post(
        "/api/viewer_map", json={"max_nodes": 2, "max_edges": 0},
        headers={"Authorization": "Bearer fixture-map-key"},
    )
    assert response.status_code == 200, response.text
    assert main(["viewer_map", "--max-nodes", "2", "--max-edges", "0", "--json"]) == 0
    cli_result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert cli_result["success"] is True
    assert response.json()["success"] is True
    for actual in (mcp_result, response.json()["data"], cli_result["data"]):
        for field in ("state", "nodes", "edges", "counts", "vocabulary", "ranking_complete"):
            assert actual[field] == expected[field], field


def test_hidden_pages_and_authored_edges_do_not_change_counts_or_rank(
    tmp_path: Path, warm_managed_cell,
) -> None:
    root = tmp_path / "vault"
    _small(root)
    hidden = "Knowledge Base/Notes/Patterns/hidden.md"
    _write(root, hidden, f"---\ntype: pattern\n---\n# Hidden\n\n## Relations\n"
           f"- supports [[{NOTES}/isolated]]\n- supports [[{NOTES}/source]]\n")
    write_scope(root)
    write_rule(root, ceiling=egress.LEVEL_NONE)
    warm_managed_cell(root)
    _build(root)
    with request_scope(_external()):
        present = commands.op_viewer_map(root)
    (root / hidden).unlink()
    warm_managed_cell(root)
    _build(root)
    with request_scope(_external()):
        absent = commands.op_viewer_map(root)
    assert present["counts"] == absent["counts"], (present, absent)
    assert present["state"] == absent["state"] == "complete"
    for field in ("nodes", "edges", "counts", "vocabulary", "ranking_complete", "truncation"):
        assert present[field] == absent[field], field


@pytest.mark.parametrize("bound", ["pages", "edges"])
def test_hidden_rows_do_not_spend_the_three_admitted_inspection_slots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, warm_managed_cell, bound: str,
) -> None:
    root = tmp_path / "vault"
    _small(root)
    _write(root, f"{NOTES}/isolated.md", "---\ntype: insight\n---\n# Isolated\n\nSee [[target]].\n")
    hidden = "Knowledge Base/Notes/Generated/hidden.md"
    _write(root, hidden, f"---\ntype: insight\n---\n# Hidden\n\n## Relations\n"
           f"- supports [[{NOTES}/source]]\n- supports [[{NOTES}/target]]\n")
    write_scope(root, paths="Notes/Generated/**")
    write_rule(root, ceiling=egress.LEVEL_NONE)
    monkeypatch.setenv(f"EXOMEM_VIEWER_MAP_MAX_INSPECTED_{bound.upper()}", "3")
    warm_managed_cell(root)
    _build(root)
    with request_scope(_external()):
        present = commands.op_viewer_map(root)
    (root / hidden).unlink()
    warm_managed_cell(root)
    _build(root)
    with request_scope(_external()):
        absent = commands.op_viewer_map(root)
    assert absent["state"] == present["state"] == "complete"
    assert absent["counts"]["eligible_nodes"] == 3
    assert absent["counts"]["eligible_edges"] == 3
    for field in ("nodes", "edges", "counts", "vocabulary", "ranking_complete", "truncation"):
        assert present[field] == absent[field], field


def test_hidden_link_candidates_resolve_as_if_absent(tmp_path: Path, warm_managed_cell) -> None:
    root = tmp_path / "vault"
    _small(root)
    _write(root, f"{NOTES}/links.md", "---\ntype: insight\n---\n# Links\n\n"
           "See [[target]] and [[isolated]].\n")
    hidden = "Knowledge Base/Notes/Patterns/target.md"
    _write(root, hidden, "---\ntype: pattern\n---\n# Other target\n")
    write_scope(root)
    write_rule(root, ceiling=egress.LEVEL_NONE)
    warm_managed_cell(root)
    _build(root)
    with request_scope(_external()):
        present = commands.op_viewer_map(root)
    (root / hidden).unlink()
    warm_managed_cell(root)
    _build(root)
    with request_scope(_external()):
        absent = commands.op_viewer_map(root)
    assert present["counts"] == absent["counts"], (present, absent)
    for field in ("nodes", "edges", "counts", "vocabulary", "ranking_complete", "truncation"):
        assert present[field] == absent[field], field
    assert any(edge["src_key"] == f"file:{NOTES}/links.md" for edge in present["edges"])

    _write(root, f"{NOTES}/links.md", "---\ntype: insight\n---\n# Links\n\nSee [[target]].\n")
    _write(root, hidden, "---\ntype: pattern\n---\n# Other target\n")
    warm_managed_cell(root)
    _build(root)
    with request_scope(_external()):
        no_stored_edge = commands.op_viewer_map(root)
    assert no_stored_edge["counts"]["eligible_edges"] == 3
    assert no_stored_edge["nodes"][0]["incoming_edges"] == 3


@pytest.mark.parametrize("extra_byte", [False, True])
def test_source_byte_boundary_accepts_supported_work_and_keeps_partial_pages(
    tmp_path: Path, warm_managed_cell, extra_byte: bool,
) -> None:
    root = tmp_path / "vault"
    _small(root)
    source = "---\ntype: insight\n---\n# Source\n\n## Relations\n- supports [[target]]\n\n"
    source += "x" * (65536 + int(extra_byte) - len(source.encode()))
    _write(root, f"{NOTES}/source.md", source)
    _write(root, "Knowledge Base/Notes/Patterns/target.md", "---\ntype: pattern\n---\n# Other target\n")
    write_scope(root)
    write_rule(root, ceiling=egress.LEVEL_NONE)
    warm_managed_cell(root)
    _build(root)
    with request_scope(_external()):
        result = commands.op_viewer_map(root)
    assert result["counts"]["drawn_nodes"] == 3
    if extra_byte:
        assert result["state"] == "partial", result
        assert result["counts"]["eligible_nodes"] is None
        assert result["counts"]["eligible_edges"] is None
        assert all(node["incoming_edges"] is None for node in result["nodes"])
        assert {"stage": "inspection_edges", "reason": "source_work_limit", "limit": 65536} in result["truncation"]
    else:
        assert result["state"] == "complete", result
        assert result["counts"]["eligible_nodes"] == 3
        assert result["counts"]["eligible_edges"] == 1


def test_changed_source_bytes_cannot_be_rederived_against_an_old_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, warm_managed_cell,
) -> None:
    root = tmp_path / "vault"
    _small(root)
    text = "---\ntype: insight\n---\n# Source\n\nSee [[target]] and [[isolated]].\n"
    _write(root, f"{NOTES}/source.md", text)
    _write(root, "Knowledge Base/Notes/Patterns/target.md", "---\ntype: pattern\n---\n# Other target\n")
    write_scope(root)
    write_rule(root, ceiling=egress.LEVEL_NONE)
    warm_managed_cell(root)
    _build(root)
    path = root / NOTES / "source.md"
    before = path.stat()
    original = vault.read_bounded_guarded_bytes
    changed = False

    def change_bytes(vault_root, target, **kwargs):
        nonlocal changed
        if target == f"{NOTES}/source.md":
            changed = True
            path.write_text(text.replace("Source", "Change"), encoding="utf-8")
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        return original(vault_root, target, **kwargs)

    monkeypatch.setattr(vault, "read_bounded_guarded_bytes", change_bytes)
    with request_scope(_external()):
        result = commands.op_viewer_map(root)
    assert changed
    assert result["state"] == "partial", result
    assert result["counts"]["eligible_edges"] is None
    assert result["counts"]["drawn_nodes"] == 3
    assert result["edges"] == []
    assert any(item["reason"] == "source_changed" for item in result["truncation"])


def test_interrupted_link_stream_cannot_fall_back_to_stored_author_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, warm_managed_cell,
) -> None:
    root = tmp_path / "vault"
    _small(root)
    _write(root, f"{NOTES}/source.md", "---\ntype: insight\n---\n# Source\n\nSee [[target]] and [[isolated]].\n")
    _write(root, "Knowledge Base/Notes/Patterns/target.md", "---\ntype: pattern\n---\n# Other target\n")
    write_scope(root)
    write_rule(root, ceiling=egress.LEVEL_NONE)
    warm_managed_cell(root)
    _build(root)
    original = vault.iter_body_wikilinks

    def interrupt(text, *, check=None):
        for match in original(text, check=check):
            yield match
            if check is not None:
                raise TimeoutError("request interrupted")

    monkeypatch.setattr(vault, "iter_body_wikilinks", interrupt)
    with request_scope(_external()):
        result = commands.op_viewer_map(root)
    assert result["state"] == "partial", result
    assert result["counts"]["eligible_edges"] is None
    assert result["counts"]["drawn_nodes"] == 3
    assert result["edges"] == []
    assert any(item["reason"] == "deadline" for item in result["truncation"])


def test_fragment_rederivation_uses_the_validated_target_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, warm_managed_cell,
) -> None:
    root = tmp_path / "vault"
    _small(root)
    _write(root, f"{NOTES}/source.md", "---\ntype: insight\n---\n# Source\n\n"
           "## Relations\n- supports [[target#observation]]\n")
    _write(root, f"{NOTES}/target.md", "---\ntype: insight\n"
           "exomem_id: 12345678-1234-4234-8234-123456789013\n---\n# Target\n\n"
           "## Claim\n- id: observation\n\nAn admitted observation.\n")
    _write(root, "Knowledge Base/Notes/Patterns/target.md", "---\ntype: pattern\n---\n# Other target\n")
    write_scope(root)
    write_rule(root, ceiling=egress.LEVEL_NONE)
    warm_managed_cell(root)
    _build(root)
    original = vault.read_bytes_without_pinning

    def no_target_source_read(path, *args, **kwargs):
        assert Path(path) != root / NOTES / "target.md", "interactive fragment read opened target source"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(vault, "read_bytes_without_pinning", no_target_source_read)
    with request_scope(_external()):
        result = commands.op_viewer_map(root)
    assert result["state"] == "complete", result
    assert result["counts"]["eligible_edges"] == 1
    assert result["edges"][0]["dst_key"] == f"file:{NOTES}/target.md"
    assert result["nodes"][0]["incoming_edges"] == 1


def test_drawn_vocabulary_keeps_two_source_selected_meanings(private_instances, vault: Path) -> None:
    with request_scope(private_instances("full")):
        for scope, description in ((None, "Public applicability."), (SCOPE, "Private applicability.")):
            overlay = instances.select(vault, registry_spec("relations"), scope).overlay(vault)
            overlay.parent.mkdir(parents=True, exist_ok=True)
            overlay.write_text(yaml.safe_dump({"schema_version": 1, "extensions": {
                "vault.applies_to": {"parent": "relates_to", "description": description},
                "vault.undrawn": {"parent": "relates_to", "description": "Not drawn."},
            }}), encoding="utf-8")
        for name, project in (("public-map", "public-project"), ("private-map", "private-project")):
            _write(vault, f"{NOTES}/{name}.md", f"---\ntype: insight\nproject: {project}\n---\n"
                   f"# {name}\n\n## Relations\n- vault.applies_to [[{NOTES}/map-target]]\n")
        _write(vault, f"{NOTES}/map-target.md", "---\ntype: insight\n---\n# Target\n")
        _build(vault)
        result = commands.op_viewer_map(vault)
    assert result["state"] == "complete", result
    meanings = [entry for entry in result["vocabulary"] if entry["definition_key"] == "vault.applies_to"]
    assert {(entry["instance"], entry["description"]) for entry in meanings} == {
        ("public", "Public applicability."), (SCOPE, "Private applicability."),
    }, result
    assert not any(entry["definition_key"] == "vault.undrawn" for entry in result["vocabulary"])


def test_cold_restricted_resolver_returns_partial_without_a_walk_or_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, walk_sentinel, warm_managed_cell,
) -> None:
    root = tmp_path / "vault"
    _small(root)
    write_scope(root)
    write_rule(root, ceiling=egress.LEVEL_NONE)
    warm_managed_cell(root)
    _build(root)
    find.evict_resolver_caches(root)
    sentinel = walk_sentinel(root / "Knowledge Base/Notes", current_thread_only=True)
    monkeypatch.setattr(find, "recall_resolver_snapshot", lambda *_a, **_k: pytest.fail("cold resolver rebuild"))
    with request_scope(_external()):
        result = commands.op_viewer_map(root)
    assert result["state"] == "partial"
    assert result["counts"]["eligible_nodes"] is None
    assert result["counts"]["eligible_edges"] is None
    assert result["counts"]["observed_nodes"] == 3
    assert result["ranking_complete"] is False
    assert any(item["reason"] == "resolver_unavailable" for item in result["truncation"])
    assert result["counts"]["drawn_nodes"] == 3
    assert all(node["incoming_edges"] is None for node in result["nodes"])
    assert sentinel.count == 0, sentinel.report()


def test_cold_registry_is_unavailable_without_walking_notes(tmp_path: Path, walk_sentinel) -> None:
    root = tmp_path / "vault"
    _small(root)
    freshness.clear()  # A restarted process has not seeded its live registry yet.
    epistemic_graph.clear_snapshot_proofs()
    sentinel = walk_sentinel(root / "Knowledge Base/Notes", current_thread_only=True)
    with library_scope():
        result = commands.op_viewer_map(root)
    assert result["state"] == "unavailable"
    assert result["counts"]["eligible_nodes"] is None
    assert result["counts"]["eligible_edges"] is None
    assert sentinel.count == 0, sentinel.report()


def test_checkpointless_proved_snapshot_uses_live_projection_without_a_walk(
    tmp_path: Path, walk_sentinel,
) -> None:
    root = tmp_path / "vault"
    _small(root)
    checkpoint = freshness.live_recall_checkpoint(root, "vault")
    assert checkpoint is not None
    index = epistemic_graph.EpistemicGraphIndex(root)
    identity = checkpoint.triple, checkpoint.policy_version, checkpoint.access_policy_fingerprint
    assert index._mark_available(identity, checkpoint=None)
    assert index.available()  # The existing maintenance proof covers this publication.
    sentinel = walk_sentinel(root / "Knowledge Base/Notes", current_thread_only=True)
    with library_scope():
        result = commands.op_viewer_map(root)
    assert result["state"] == "complete", result
    assert result["counts"]["eligible_nodes"] == 3
    assert result["counts"]["eligible_edges"] == 2
    assert sentinel.count == 0, sentinel.report()


@pytest.mark.parametrize("loss", ["opening", "validated"])
def test_registry_loss_refuses_without_walking_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, walk_sentinel, loss: str,
) -> None:
    root = tmp_path / "vault"
    _small(root)
    if loss == "opening":
        original = epistemic_graph.EpistemicGraphIndex._connect_existing

        def lose_registry(index, *args, **kwargs):
            connection = original(index, *args, **kwargs)
            freshness.clear()
            return connection

        monkeypatch.setattr(epistemic_graph.EpistemicGraphIndex, "_connect_existing", lose_registry)
    else:
        original_adopt = freshness.adopt_recall_origin

        def lose_registry(*args, **kwargs):
            accepted = original_adopt(*args, **kwargs)
            freshness.clear()
            return accepted

        monkeypatch.setattr(freshness, "adopt_recall_origin", lose_registry)
    sentinel = walk_sentinel(root / "Knowledge Base/Notes", current_thread_only=True)
    with library_scope():
        result = commands.op_viewer_map(root)
    assert result["state"] == "unavailable", result
    assert result["counts"]["eligible_nodes"] is None
    assert result["counts"]["eligible_edges"] is None
    assert sentinel.count == 0, sentinel.report()


def test_proved_inherited_snapshot_uses_current_resident_topology(
    tmp_path: Path, warm_managed_cell, walk_sentinel,
) -> None:
    root = tmp_path / "vault"
    _small(root)
    write_scope(root)
    write_rule(root, ceiling=egress.LEVEL_NONE)
    warm_managed_cell(root)
    index = _build(root)
    stored = freshness.live_recall_checkpoint(root, "vault")
    with request_scope(_external()):
        expected = commands.op_viewer_map(root)
    assert expected["state"] == "complete", expected
    freshness.clear()
    epistemic_graph.clear_snapshot_proofs()
    find.evict_resolver_caches(root)
    warm_managed_cell(root)
    assert index.adopt_published_snapshot().adopted is True
    with request_scope(_external()):
        assert commands.op_viewer_map(root)["state"] == "unavailable"
    assert index.available()  # Adoption alone does not remember the public reader's proof.
    live = freshness.live_recall_checkpoint(root, "vault")
    assert live is not None and live != stored
    assert find.recall_resolver_snapshot_at_checkpoint(root, live) is not None
    sentinel = walk_sentinel(root / "Knowledge Base/Notes", current_thread_only=True)
    with request_scope(_external()):
        result = commands.op_viewer_map(root)
    assert result["state"] == "complete", result
    for field in ("nodes", "edges", "counts", "vocabulary", "ranking_complete"):
        assert result[field] == expected[field], field
    assert sentinel.count == 0, sentinel.report()


def test_topology_change_during_reduction_withholds_edges_and_exact_totals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, warm_managed_cell,
) -> None:
    root = tmp_path / "vault"
    _small(root)
    write_scope(root)
    write_rule(root, ceiling=egress.LEVEL_NONE)
    warm_managed_cell(root)
    _build(root)
    original = epistemic_graph.GraphView.edge
    changed = False

    def advance_topology(view, row):
        nonlocal changed
        served = original(view, row)
        if not changed:
            changed = True
            added = root / NOTES / "new-page.md"
            _write(root, f"{NOTES}/new-page.md", "---\ntype: insight\n---\n# New page\n")
            freshness.on_files_changed(root, [added])
        return served

    monkeypatch.setattr(epistemic_graph.GraphView, "edge", advance_topology)
    with request_scope(_external()):
        result = commands.op_viewer_map(root)
    assert result["state"] == "partial", result
    assert result["counts"]["eligible_nodes"] is None
    assert result["counts"]["eligible_edges"] is None
    assert result["counts"]["drawn_nodes"] == 3
    assert result["edges"] == []
    assert all(node["incoming_edges"] is None for node in result["nodes"])
    assert any(item["reason"] == "resolver_checkpoint_changed" for item in result["truncation"])


@pytest.mark.query_deadline
def test_sqlite_contention_is_unavailable_within_the_viewer_deadline(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    _small(root)
    with sqlite3.connect(epistemic_graph.sidecar_path(root)) as publisher:
        publisher.execute("PRAGMA journal_mode=DELETE")
        publisher.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        with library_scope():
            result = commands.op_viewer_map(root)
        elapsed = time.monotonic() - started
        publisher.rollback()
    assert elapsed < 5
    assert result["state"] == "unavailable", result
    assert result["counts"]["eligible_nodes"] is None
    assert result["counts"]["eligible_edges"] is None


@pytest.mark.query_deadline
@pytest.mark.timeout(8)
def test_publication_hold_does_not_outlast_the_viewer_deadline(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    _small(root)
    held = epistemic_graph._acquire_publication_hold(epistemic_graph.sidecar_path(root))
    assert held is not None
    try:
        started = time.monotonic()
        with library_scope():
            result = commands.op_viewer_map(root)
        elapsed = time.monotonic() - started
    finally:
        epistemic_graph._release_publication_hold(held)
    assert elapsed < 5
    assert result["state"] in {"complete", "partial"}


@pytest.mark.parametrize("bound", ["pages", "edges", "deadline"])
def test_stopped_inspection_never_claims_exact_totals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bound: str,
) -> None:
    root = tmp_path / "vault"
    _small(root)
    if bound == "deadline":
        budget = request_budget.RequestBudget(seconds=0.001, entry=time.monotonic() - 1)
        token = request_budget.set_current(budget)
    else:
        if bound == "edges":
            _write(root, f"{NOTES}/another.md", f"---\ntype: insight\n---\n# Another\n\n"
                   f"## Relations\n- supports [[{NOTES}/target]]\n")
            _build(root)
        monkeypatch.setenv(f"EXOMEM_VIEWER_MAP_MAX_INSPECTED_{bound.upper()}", "1")
        token = request_budget.set_current(None)
    try:
        with library_scope():
            result = commands.op_viewer_map(root)
    finally:
        request_budget.reset_current(token)
    assert result["state"] == "partial"
    assert result["counts"]["eligible_nodes"] is None
    assert result["counts"]["eligible_edges"] is None
    assert result["ranking_complete"] is False
    assert result["truncation"]
    assert result["counts"]["drawn_nodes"] == len(result["nodes"])
    assert result["counts"]["drawn_edges"] == len(result["edges"])


@pytest.mark.parametrize("failure", ["missing", "incompatible", "unproven", "stale"])
def test_unavailable_snapshot_is_not_an_empty_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    root = tmp_path / "vault"
    if failure != "missing":
        _small(root)
        if failure == "incompatible":
            with sqlite3.connect(epistemic_graph.sidecar_path(root)) as connection:
                connection.execute("UPDATE graph_meta SET value = '0' WHERE key = 'schema_version'")
        elif failure == "unproven":
            epistemic_graph.clear_snapshot_proofs()
            monkeypatch.setattr(freshness, "recall_is_live", lambda *_a, **_k: False)
        else:
            _write(root, f"{NOTES}/source.md", "---\ntype: insight\n---\n# Changed\n")
            freshness.on_files_changed(root, [root / f"{NOTES}/source.md"])
    with library_scope():
        result = commands.op_viewer_map(root)
    assert result["state"] == "unavailable"
    assert result["available"] is False
    assert result["counts"]["eligible_nodes"] is None
    assert result["counts"]["eligible_edges"] is None
    assert result["snapshot"] is None
    assert result["reason"]


@pytest.mark.query_deadline
@pytest.mark.timeout(180)
def test_large_managed_vault_bounds_transport_without_a_source_walk(
    tmp_path: Path, warm_managed_cell, walk_sentinel,
) -> None:
    root = tmp_path / "vault"
    for number in range(4000):
        next_number = (number + 1) % 4000
        _write(root, f"{NOTES}/page-{number:04d}.md",
               f"---\ntype: insight\n---\n# Page {number}\n\n## Relations\n"
               f"- supports [[{NOTES}/page-{next_number:04d}]]\n")
    warm_managed_cell(root)
    _build(root)
    sentinel = walk_sentinel(root, current_thread_only=True)
    start = time.monotonic()
    with library_scope():
        result = commands.op_viewer_map(root)
    elapsed = time.monotonic() - start
    wire_bytes = len(json.dumps(result).encode())
    print(f"viewer_map pages=4000 state={result['state']} seconds={elapsed:.3f} bytes={wire_bytes}")
    assert elapsed < 5
    assert wire_bytes < 512_000
    assert result["state"] in {"complete", "partial"}
    assert len(result["nodes"]) <= 200 and len(result["edges"]) <= 400
    assert sentinel.count == 0, sentinel.report()
