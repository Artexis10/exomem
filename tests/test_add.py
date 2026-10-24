"""add tool tests — full SKILL.md rule-7 enforcement against the fixture KB."""

from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from pathlib import Path

import pytest
import yaml

from exomem import add as add_module
from exomem import audit, indexes
from exomem import schema as schema_module

TODAY = dt.date(2026, 5, 18)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_add_article_writes_source_file(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    result = add_module.add(
        vault,
        source_schema,
        content="Long-form article body about retrieval-augmented generation.",
        source_type="article",
        title="Agentic RAG explained",
        url="https://example.com/agentic-rag",
        tags=["rag", "agentic"],
        why_captured="Useful for the Q retrieval roadmap.",
        today=TODAY,
    )
    expected = vault / "Knowledge Base" / "Sources" / "Articles" / "2026-05-18-agentic-rag-explained.md"
    assert expected.exists()
    assert result.path == "Knowledge Base/Sources/Articles/2026-05-18-agentic-rag-explained.md"

    text = _read(expected)
    fm, _, body = text.partition("\n---\n")
    parsed = yaml.safe_load(fm.removeprefix("---\n"))
    assert parsed["type"] == "source"
    assert parsed["title"] == "Agentic RAG explained"
    assert parsed["source_type"] == "article"
    assert parsed["captured"] == TODAY  # date type round-trips
    assert parsed["url"] == "https://example.com/agentic-rag"
    assert parsed["tags"] == ["rag", "agentic"]
    assert parsed["ingested_into"] == []
    assert "# Agentic RAG explained" in body
    assert "Useful for the Q retrieval roadmap" in body
    assert "## Capture" in body


def test_add_session_no_url_ok(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    result = add_module.add(
        vault,
        source_schema,
        content="Pasted conversation about exomem scope.",
        source_type="session",
        title="exomem scope session",
        today=TODAY,
    )
    assert result.path.endswith(".md")
    assert "Sources/Sessions/" in result.path


def test_add_paper_auto_creates_folder(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    papers_dir = vault / "Knowledge Base" / "Sources" / "Papers"
    assert not papers_dir.exists()
    add_module.add(
        vault,
        source_schema,
        content="Abstract: novel retrieval method...",
        source_type="paper",
        title="A new retrieval method",
        url="https://arxiv.org/abs/2026.05001",
        today=TODAY,
    )
    assert papers_dir.is_dir()
    assert (papers_dir / "2026-05-18-a-new-retrieval-method.md").exists()


def test_add_video_auto_creates_folder(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    add_module.add(
        vault,
        source_schema,
        content="Video transcript content.",
        source_type="video",
        title="Talk on retrieval",
        url="https://youtube.com/watch?v=xyz",
        today=TODAY,
    )
    assert (vault / "Knowledge Base" / "Sources" / "Videos").is_dir()


def test_add_filename_collision_appends_suffix(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    common = dict(
        source_type="session",
        title="Conflict session",
        content="body",
        today=TODAY,
    )
    r1 = add_module.add(vault, source_schema, **common)
    r2 = add_module.add(vault, source_schema, **common)
    r3 = add_module.add(vault, source_schema, **common)
    assert r1.path.endswith("2026-05-18-conflict-session.md")
    assert r2.path.endswith("2026-05-18-conflict-session-2.md")
    assert r3.path.endswith("2026-05-18-conflict-session-3.md")


def test_add_accepts_an_unfamiliar_but_meaningful_source_type(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    """An unseen kind is not an error — it is the open vocabulary working.

    This assertion is the inverse of the one it replaces. The old contract
    refused anything outside a six-token markdown table, which is what forced
    clearly classifiable material into `Sources/Other`.
    """
    result = add_module.add(
        vault,
        source_schema,
        content="Observations from three site visits.",
        source_type="field-notebook",
        title="Hedgerow establishment notes",
        today=TODAY,
    )
    assert result.path == (
        "Knowledge Base/Sources/Field Notebook/"
        "2026-05-18-hedgerow-establishment-notes.md"
    )


def test_add_rejects_a_source_type_that_cannot_become_a_safe_key(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    with pytest.raises(add_module.AddError) as exc:
        add_module.add(
            vault,
            source_schema,
            content="x",
            source_type="..",
            title="t",
            today=TODAY,
        )
    assert exc.value.code == "INVALID_SOURCE"
    assert exc.value.missing == ["source_type"]


def test_add_rejects_article_without_url(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    with pytest.raises(add_module.AddError) as exc:
        add_module.add(
            vault,
            source_schema,
            content="x",
            source_type="article",
            title="t",
            today=TODAY,
        )
    assert "url" in exc.value.missing


def test_add_updates_sources_index_count(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    sources_index = vault / "Knowledge Base" / "Sources" / "index.md"
    before = _read(sources_index)
    assert "Articles]] — captured web/PDF content (2)" in before

    add_module.add(
        vault,
        source_schema,
        content="body",
        source_type="article",
        title="New article",
        url="https://example.com/x",
        today=TODAY,
    )
    after = _read(sources_index)
    assert "Articles]] — captured web/PDF content (3)" in after
    assert "2026-05-18 — [[Knowledge Base/Sources/Articles/2026-05-18-new-article]]" in after


def test_pending_capture_does_not_change_an_independent_index_audit(
    vault: Path,
    source_schema: schema_module.SourceSchema,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = audit.audit(vault, categories=["index_drift"]).findings
    observed = []
    compute_updates = indexes.compute_updates
    audit_context = copy_context()

    with ThreadPoolExecutor(max_workers=1) as pool:
        def audit_during_planning(*args, **kwargs):
            report = pool.submit(audit_context.run, audit.audit, vault, categories=["index_drift"])
            observed.append(report.result(timeout=10).findings)
            return compute_updates(*args, **kwargs)

        monkeypatch.setattr(indexes, "compute_updates", audit_during_planning)
        add_module.add(
            vault,
            source_schema,
            content="A captured discussion.",
            source_type="session",
            title="Independent count audit",
            today=TODAY,
        )

    assert observed
    assert all(findings == before for findings in observed)


def test_add_renders_generated_indexes_for_nested_obsidian_root(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    (vault / "Knowledge Base" / ".obsidian").mkdir()

    add_module.add(
        vault,
        source_schema,
        content="body",
        source_type="article",
        title="Nested root article",
        url="https://example.com/nested",
        today=TODAY,
    )

    sources_index = _read(vault / "Knowledge Base" / "Sources" / "index.md")
    assert "[[Sources/Articles/|Articles]]" in sources_index
    assert "[[Sources/Articles/2026-05-18-nested-root-article]]" in sources_index
    assert "[[Knowledge Base/" not in sources_index


def test_add_updates_sources_index_for_new_folder(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    add_module.add(
        vault,
        source_schema,
        content="body",
        source_type="paper",
        title="New paper",
        url="https://arxiv.org/abs/x",
        today=TODAY,
    )
    after = _read(vault / "Knowledge Base" / "Sources" / "index.md")
    # By-type list should now include Papers with count 1
    assert "Papers]] — academic papers (1)" in after


def test_add_updates_top_index_recent_activity(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    top_index = vault / "Knowledge Base" / "index.md"
    add_module.add(
        vault,
        source_schema,
        content="body",
        source_type="session",
        title="Big session",
        today=TODAY,
    )
    text = _read(top_index)
    # New bullet at the top of Recent activity
    recent_idx = text.find("## Recent activity")
    assert recent_idx != -1
    next_h2 = text.find("\n## ", recent_idx + 1)
    section = text[recent_idx:next_h2]
    # First bullet under the section should be ours
    first_bullet = next(line for line in section.splitlines() if line.startswith("- "))
    assert "2026-05-18" in first_bullet
    assert "Big session" in first_bullet


def test_add_updates_top_index_counts(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    add_module.add(
        vault,
        source_schema,
        content="body",
        source_type="session",
        title="Counts test",
        today=TODAY,
    )
    text = _read(vault / "Knowledge Base" / "index.md")
    # Fixture baseline: Sources: 4 (articles: 2, books: 1, sessions: 1)
    # After adding one session: total 5, sessions 2
    assert "- Sources: 5" in text
    assert "sessions: 2" in text


def test_add_appends_to_log(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    log_file = vault / "Knowledge Base" / "log.md"
    add_module.add(
        vault,
        source_schema,
        content="body",
        source_type="session",
        title="Log test",
        why_captured="Testing log append",
        today=TODAY,
    )
    after = _read(log_file)
    # New entry should appear before the previous most-recent (2026-05-10)
    new_idx = after.find("## [2026-05-18] add | Sources/Sessions/2026-05-18-log-test")
    old_idx = after.find("## [2026-05-10] note")
    assert new_idx != -1
    assert new_idx < old_idx
    # Header lines preserved
    assert after.startswith("# Knowledge Base — Activity Log")
    assert "Testing log append" in after


def test_add_full_rule7_atomic_all_four_files_updated(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    """One add → four files mutated."""
    src_index = vault / "Knowledge Base" / "Sources" / "index.md"
    top_index = vault / "Knowledge Base" / "index.md"
    log_file = vault / "Knowledge Base" / "log.md"

    before = {p: _read(p) for p in (src_index, top_index, log_file)}
    result = add_module.add(
        vault,
        source_schema,
        content="atomicity check",
        source_type="book",
        title="Atomicity",
        today=TODAY,
    )
    source_file = vault / result.path

    assert source_file.exists()
    for p, prev in before.items():
        assert _read(p) != prev, f"{p.name} should have been updated"


def test_add_tag_normalization(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    result = add_module.add(
        vault,
        source_schema,
        content="body",
        source_type="session",
        title="Tag test",
        tags=["UPPER", "with space", "with_under", "dupe", "DUPE"],
        today=TODAY,
    )
    text = _read(vault / result.path)
    fm = text.split("\n---\n")[0].removeprefix("---\n")
    parsed = yaml.safe_load(fm)
    assert parsed["tags"] == ["upper", "with-space", "with-under", "dupe"]


@pytest.fixture
def capture_owner():
    from exomem.governance import principal

    with principal.request_scope(principal.owner_principal(surface="mcp")):
        yield


def _capture_policy(vault: Path, *, path: str = "Sources/**", ceiling: int = 0) -> None:
    from exomem.governance import policy, principal

    root = policy.governance_root(vault)
    scope = root / "scopes" / "capture.yaml"
    scope.parent.mkdir(parents=True, exist_ok=True)
    scope.write_text(yaml.safe_dump({
        "governance_version": 1, "id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "name": "Capture policy", "paths": [path],
    }))
    rule = root / "rules" / "capture.yaml"
    rule.parent.mkdir(parents=True, exist_ok=True)
    rule.write_text(yaml.safe_dump({
        "governance_version": 1, "id": "01ARZ3NDEKTSV4RRFFQ69G5FB0",
        "scope_ids": ["01ARZ3NDEKTSV4RRFFQ69G5FAV"],
        "audience": principal.OWNER_AUDIENCE, "ceiling": ceiling,
    }))


def test_capture_identity_reuses_original_after_metadata_update(vault, source_schema, capture_owner):
    from exomem.commands import op_capture_source

    inputs = dict(title="A captured record", content="Original text", source_kind="session",
                  source_identity={"system": "archive", "record_id": "item-7", "revision": "r2"})
    first = op_capture_source(vault, source_schema, **inputs)
    page = vault / first["source"]["path"]
    original = page.read_text()
    page.write_text(original.replace("ingested_into: []", "ingested_into: [compiled-note]"))
    before = {p: p.read_bytes() for p in vault.rglob("*.md")}

    second = op_capture_source(vault, source_schema, **inputs)

    assert first["capture_outcome"] == "created"
    assert second["capture_outcome"] == "reused"
    assert second["source"]["ref"] == first["source"]["ref"]
    assert {p: p.read_bytes() for p in vault.rglob("*.md")} == before


def test_capture_identity_conflict_never_writes(vault, source_schema, capture_owner):
    from exomem.commands import op_capture_source

    inputs = dict(title="A captured record", source_kind="session",
                  source_identity={"system": "archive", "record_id": "item-7", "revision": "r2"})
    op_capture_source(vault, source_schema, content="Original text", **inputs)
    before = {p: p.read_bytes() for p in vault.rglob("*.md")}
    with pytest.raises(ValueError, match="SOURCE_IDENTITY_CONFLICT"):
        op_capture_source(vault, source_schema, content="Different text", **inputs)
    assert {p: p.read_bytes() for p in vault.rglob("*.md")} == before


def test_capture_identity_keeps_records_and_out_of_order_revisions_separate(vault, source_schema, capture_owner):
    from exomem.commands import op_capture_source

    captures = [op_capture_source(
        vault, source_schema, title="A captured record", content="Same text", source_kind="session",
        source_identity={"system": "archive", "record_id": record, "revision": revision},
    ) for record, revision in (("item-7", "r2"), ("item-8", "r2"), ("item-7", "r1"))]

    assert len({item["source"]["ref"] for item in captures}) == 3
    for item in captures:
        fields, _, _ = add_module.parse_frontmatter((vault / item["source"]["path"]).read_text())
        assert "superseded_by" not in fields
        assert fields.get("status") != "superseded"


@pytest.mark.parametrize("damage", ["unreadable", "malformed"])
def test_capture_identity_cannot_claim_absence_after_incomplete_scan(vault, source_schema, monkeypatch, damage, capture_owner):
    from exomem import source_capture
    from exomem.commands import op_capture_source

    page = vault / "Knowledge Base/Sources/Sessions/damaged.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("---\ntype: [\n---\nBody")
    if damage == "unreadable":
        original = source_capture.vault.read_bounded_guarded_bytes

        def unreadable(root, target, **kwargs):
            if root / target == page:
                raise PermissionError("unreadable candidate")
            return original(root, target, **kwargs)

        monkeypatch.setattr(source_capture.vault, "read_bounded_guarded_bytes", unreadable)
    before = {p: p.read_bytes() for p in vault.rglob("*.md")}
    with pytest.raises(ValueError, match="SOURCE_IDENTITY_UNAVAILABLE"):
        op_capture_source(vault, source_schema, title="New record", content="Body", source_kind="session",
                          source_identity={"system": "archive", "record_id": "new", "revision": "r1"})
    assert {p: p.read_bytes() for p in vault.rglob("*.md")} == before


@pytest.mark.parametrize("restricted_path", ["Sources/**", "Evidence/Nested/**", "Notes/**"])
def test_capture_identity_policy_hides_matches_conflicts_and_absence(vault, source_schema, capture_owner, restricted_path):
    from exomem.commands import op_capture_source
    from exomem.governance import policy

    inputs = dict(title="A captured record", content="Original text", source_kind="session",
                  source_identity={"system": "archive", "record_id": "saved", "revision": "r1"})
    op_capture_source(vault, source_schema, **inputs)
    _capture_policy(vault, path=restricted_path)
    assert not policy.load(vault).blocked
    before = {p: p.read_bytes() for p in vault.rglob("*.md")}
    errors = []
    for probe in (inputs, {**inputs, "content": "Changed text"},
                  {**inputs, "source_identity": {"system": "archive", "record_id": "absent", "revision": "r1"}}):
        with pytest.raises(ValueError, match="SOURCE_IDENTITY_FORBIDDEN") as refused:
            op_capture_source(vault, source_schema, **probe)
        errors.append(str(refused.value))
    assert len(set(errors)) == 1
    assert {p: p.read_bytes() for p in vault.rglob("*.md")} == before
    ordinary = op_capture_source(vault, source_schema, title="Ordinary capture", content="Text", source_kind="session")
    assert (vault / ordinary["source"]["path"]).is_file()


@pytest.mark.parametrize("standing_grant", [False, True])
def test_capture_identity_allows_full_policy_and_standing_grants(vault, source_schema, capture_owner, standing_grant):
    from exomem.commands import op_capture_source
    from exomem.governance import policy, principal

    _capture_policy(vault, ceiling=0 if standing_grant else 6)
    if standing_grant:
        grant = policy.governance_root(vault) / "grants" / "capture.yaml"
        grant.parent.mkdir(parents=True, exist_ok=True)
        grant.write_text(yaml.safe_dump({
            "governance_version": 1, "id": "01ARZ3NDEKTSV4RRFFQ69G5FB1", "kind": "standing",
            "scope_ids": ["01ARZ3NDEKTSV4RRFFQ69G5FAV"],
            "audience": principal.OWNER_AUDIENCE, "ceiling": 6,
        }))
    compiled = policy.load(vault)
    assert not compiled.empty and not compiled.blocked
    inputs = dict(title="Allowed record", content="Text", source_kind="session",
                  source_identity={"system": "archive", "record_id": "new", "revision": "r1"})
    first = op_capture_source(vault, source_schema, **inputs)
    repeated = op_capture_source(vault, source_schema, **inputs)
    assert first["capture_outcome"] == "created"
    assert repeated["capture_outcome"] == "reused"
    assert repeated["source"]["ref"] == first["source"]["ref"]


def test_capture_identity_refuses_a_cold_blocked_policy(vault, source_schema, capture_owner):
    from exomem.commands import op_capture_source
    from exomem.governance import policy

    _capture_policy(vault, ceiling=9)
    assert policy.load(vault).blocked
    before = {p: p.read_bytes() for p in vault.rglob("*.md")}
    with pytest.raises(ValueError, match="SOURCE_IDENTITY_FORBIDDEN"):
        op_capture_source(vault, source_schema, title="Blocked record", content="Text", source_kind="session",
                          source_identity={"system": "archive", "record_id": "new", "revision": "r1"})
    assert {p: p.read_bytes() for p in vault.rglob("*.md")} == before


def test_capture_identity_refuses_malformed_verified_session_context(vault, source_schema):
    from dataclasses import replace

    from exomem.commands import op_capture_source
    from exomem.governance import principal

    who = replace(principal.owner_principal(surface="mcp"), verified_authorization_session=object())
    before = {p: p.read_bytes() for p in vault.rglob("*.md")}
    with principal.request_scope(who), pytest.raises(ValueError, match="SOURCE_IDENTITY_FORBIDDEN"):
        op_capture_source(vault, source_schema, title="Invalid context", content="Text", source_kind="session",
                          source_identity={"system": "archive", "record_id": "new", "revision": "r1"})
    assert {p: p.read_bytes() for p in vault.rglob("*.md")} == before


def test_capture_identity_refuses_unavailable_purpose_authority(vault, source_schema, capture_owner, monkeypatch):
    from exomem.commands import op_capture_source
    from exomem.governance import egress

    _capture_policy(vault, ceiling=6)

    def unavailable(*_args):
        raise egress.AuthorizationSessionDecisionUnavailable

    monkeypatch.setattr(egress, "_declared_purpose", unavailable)
    before = {p: p.read_bytes() for p in vault.rglob("*.md")}
    with pytest.raises(ValueError, match="SOURCE_IDENTITY_FORBIDDEN"):
        op_capture_source(vault, source_schema, title="Unavailable purpose", content="Text", source_kind="session",
                          source_identity={"system": "archive", "record_id": "new", "revision": "r1"})
    assert {p: p.read_bytes() for p in vault.rglob("*.md")} == before


def test_capture_identity_refuses_attachments_before_fetch(vault, source_schema, monkeypatch):
    from exomem import client_artifacts
    from exomem.commands import op_capture_source

    monkeypatch.setattr(client_artifacts, "capture_source_artifacts", lambda *_a, **_k: pytest.fail("file fetch"))
    with pytest.raises(ValueError, match="SOURCE_IDENTITY_TEXT_ONLY"):
        op_capture_source(vault, source_schema, title="New record", source_kind="session",
                          files=[{"file_id": "file-1", "download_url": "https://example.com/file"}],
                          source_identity={"system": "archive", "record_id": "new", "revision": "r1"})


def test_capture_identity_concurrent_requests_create_once(vault, source_schema, capture_owner):
    from exomem.commands import op_capture_source

    def capture():
        return op_capture_source(vault, source_schema, title="Concurrent record", content="Text",
                                 source_kind="session",
                                 source_identity={"system": "archive", "record_id": "new", "revision": "r1"})

    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [pool.submit(copy_context().run, capture) for _ in range(2)]
        results = [item.result(timeout=30) for item in pending]
    assert {item["capture_outcome"] for item in results} == {"created", "reused"}
    assert len({item["source"]["ref"] for item in results}) == 1


def test_raw_capture_identity_keeps_the_original_text_digest(vault, source_schema, capture_owner):
    from exomem.commands import op_capture_source

    inputs = dict(title="Protected record", source_kind="session", raw_protection=True,
                  source_identity={"system": "archive", "record_id": "new", "revision": "r1"})
    first = op_capture_source(vault, source_schema, content="Original raw text", **inputs)
    replay = op_capture_source(vault, source_schema, content="Original raw text", **inputs)
    assert replay["capture_outcome"] == "reused"
    assert replay["source"]["ref"] == first["source"]["ref"]
    with pytest.raises(ValueError, match="SOURCE_IDENTITY_CONFLICT"):
        op_capture_source(vault, source_schema, content="Changed raw text", **inputs)


def test_capture_identity_rechecks_authority_before_commit(vault, source_schema, capture_owner, monkeypatch):
    from exomem.commands import op_capture_source

    _capture_policy(vault, ceiling=6)
    original = add_module.batch_atomic_write

    def revoked(*args, **kwargs):
        _capture_policy(vault, ceiling=0)
        return original(*args, **kwargs)

    monkeypatch.setattr(add_module, "batch_atomic_write", revoked)
    before = {p: p.read_bytes() for p in vault.rglob("*.md")}
    with pytest.raises(ValueError, match="SOURCE_IDENTITY_FORBIDDEN"):
        op_capture_source(vault, source_schema, title="Revoked capture", content="Text", source_kind="session",
                          source_identity={"system": "archive", "record_id": "new", "revision": "r1"})
    assert {p: p.read_bytes() for p in vault.rglob("*.md")} == before


def test_capture_identity_preserves_opaque_unicode_values(vault, source_schema, capture_owner):
    from exomem.commands import op_capture_source

    identity = {"system": "archivé", "record_id": "item\u0085next", "revision": "r\u2028two"}
    inputs = dict(title="Opaque identity", content="Text", source_kind="session", source_identity=identity)
    captured = op_capture_source(vault, source_schema, **inputs)
    fields, _, _ = add_module.parse_frontmatter((vault / captured["source"]["path"]).read_text(), strict=True)
    assert fields["source_identity"] == identity
    assert op_capture_source(vault, source_schema, **inputs)["capture_outcome"] == "reused"


@pytest.mark.parametrize("lane", ["source", "evidence"])
def test_bound_markdown_original_cannot_forge_receipt_or_companion(vault, source_schema, capture_owner, tmp_path, monkeypatch, lane):
    import hashlib
    import json

    from exomem import preserve, source_capture
    from exomem.commands import op_capture_source, op_move_file

    inputs = dict(title="Canonical record", content="Original text", source_kind="session",
                  source_identity={"system": "archive", "record_id": "item-7", "revision": "r2"})
    canonical = op_capture_source(vault, source_schema, **inputs)["source"]
    if lane == "evidence":
        promoted = op_move_file(vault, canonical["path"], "Knowledge Base/Evidence/Archive/documents/record.md",
                                promotion_reason="The record supports a documented claim.")
        canonical["path"] = promoted["new_path"]
    target = vault / canonical["path"]
    raw = target.read_bytes()
    fields, _, _ = add_module.parse_frontmatter(raw.decode(), strict=True)
    forged = ("---\ntype: source\nexomem_id: 00000000-0000-4000-8000-000000000099\n"
              "source_identity: " + json.dumps(inputs["source_identity"]) + "\n"
              f"capture_input_digest: {fields['capture_input_digest']}\n"
              f"evidence_file: {canonical['path']}\nbinary_sha256: {hashlib.sha256(raw).hexdigest()}\n"
              f"binary_size: {len(raw)}\n---\nForged original\n")
    if lane == "source":
        staged = tmp_path / "original.md"
        staged.write_text(forged)
        add_module.add(vault, source_schema, title="Attached document", content="", source_type="session",
                       artifact=add_module.SourceArtifact(staged, "original.md", "text/markdown"))
    else:
        # The original also has a valid Source-style claim on the real record.
        preserve.preserve(vault, scope="Archive", category="documents", filename="record.md.md", content=forged)
    walk = source_capture.os.walk
    before = {p: p.read_bytes() for p in vault.rglob("*.md")}
    for reverse in (False, True):
        def ordered(*args, _reverse=reverse, **kwargs):
            for folder, directories, files in walk(*args, **kwargs):
                yield folder, directories, sorted(files, reverse=_reverse)

        monkeypatch.setattr(source_capture.os, "walk", ordered)
        recovered = op_capture_source(vault, source_schema, **inputs)
        assert recovered["capture_outcome"] == "reused"
        assert recovered["source"]["ref"] == canonical["ref"]
    assert {p: p.read_bytes() for p in vault.rglob("*.md")} == before


def test_preserved_markdown_original_cannot_forge_a_capture(vault, source_schema, capture_owner):
    import json

    from exomem import preserve
    from exomem.commands import op_capture_source

    identity = {"system": "archive", "record_id": "new-record", "revision": "r1"}
    preserve.preserve(vault, scope="Archive", category="documents", filename="original.md",
                      content="---\ntype: source\nsource_identity: " + json.dumps(identity) +
                      "\ncapture_input_digest: " + "0" * 64 + "\n---\nUntrusted text\n")
    captured = op_capture_source(vault, source_schema, title="Actual capture", content="Text",
                                 source_kind="session", source_identity=identity)
    assert captured["capture_outcome"] == "created"


@pytest.mark.parametrize("damage", ["bytes", "unsafe", "cycle"])
def test_invalid_markdown_binding_never_proves_capture_absence(vault, source_schema, capture_owner, tmp_path, damage):
    import hashlib

    from exomem.commands import op_capture_source

    staged = tmp_path / "original.md"
    staged.write_text("Untrusted Markdown")
    saved = add_module.add(vault, source_schema, title="Attached document", content="", source_type="session",
                           artifact=add_module.SourceArtifact(staged, "original.md", "text/markdown"))
    if damage == "bytes":
        (vault / saved.artifact_path).write_text("Changed bytes")
    elif damage == "unsafe":
        page = vault / saved.path
        page.write_text(page.read_text().replace(saved.artifact_path, "../unavailable.md"))
    else:
        page = vault / saved.path
        original = vault / saved.artifact_path
        data = page.read_bytes()
        original.write_text(f"---\ntype: source\nevidence_file: {saved.path}\n"
                            f"binary_sha256: {hashlib.sha256(data).hexdigest()}\nbinary_size: {len(data)}\n---\nCyclic declaration")
    before = {p: p.read_bytes() for p in vault.rglob("*.md")}
    with pytest.raises(ValueError, match="SOURCE_IDENTITY_UNAVAILABLE"):
        op_capture_source(vault, source_schema, title="New capture", content="Text", source_kind="session",
                          source_identity={"system": "archive", "record_id": "new-record", "revision": "r1"})
    assert {p: p.read_bytes() for p in vault.rglob("*.md")} == before
