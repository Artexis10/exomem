"""Governed bulk upsert for Records: one guard, per-row outcomes, chained events."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from record_fixtures import (
    LEDGER_COLLECTION_PATH,
    LEDGER_MANIFEST_TEXT,
    ledger_item,
    setup_ledger_collection,
)

from exomem import record_formats, records, vault
from exomem import structured_collections as collections
from exomem.cli_ops import OpError
from exomem.governance.principal import library_scope
from exomem.record_memory import record_memory

EVIDENCE = "Knowledge Base/Evidence/import-a.md"
EVIDENCE_B = "Knowledge Base/Evidence/import-b.md"
_METRICS_TAIL = "    metrics:\n      type: array\n      items:\n        type: object\n"


def _evidence(vault_root: Path, *paths: str) -> None:
    for relative in paths or (EVIDENCE,):
        target = vault_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("---\ntype: evidence\n---\n\nPreserved import.\n", encoding="utf-8")


def _manifest(vault_root: Path) -> collections.CollectionManifest:
    return collections.load_manifest(vault_root, vault_root / LEDGER_COLLECTION_PATH)


def _hash(vault_root: Path) -> str:
    from exomem.collection_store.preview import bound_writer

    writer = bound_writer(vault_root)
    if writer is not None:
        return writer.inspect_collection(LEDGER_COLLECTION_PATH)["lifecycle_guards"]["expected_container_hash"]
    manifest = _manifest(vault_root)
    return record_formats.load_adapter(vault_root, manifest).read().snapshot


def _rows(count: int, *, start: int = 0, **overrides: object) -> list[dict[str, Any]]:
    return [
        {"item": ledger_item(slug=f"entry-{start + index:03d}", **overrides)}
        for index in range(count)
    ]


def _bulk(vault_root: Path, rows: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("why", "import a normalised export")
    kwargs.setdefault("expected_container_hash", _hash(vault_root))
    kwargs.setdefault("source", EVIDENCE)
    return records.bulk_upsert_records(vault_root, LEDGER_COLLECTION_PATH, rows=rows, **kwargs)


def _state(vault_root: Path) -> dict[str, bytes]:
    state = {
        path.relative_to(vault_root).as_posix(): path.read_bytes()
        for path in sorted(vault_root.rglob("*"))
        if path.is_file()
    }
    from exomem.collection_store.preview import bound_writer

    writer = bound_writer(vault_root)
    if writer is not None:
        state["canonical-store"] = "\n".join(writer.connection.iterdump()).encode()
    return state


def _with_sources_field(vault_root: Path) -> None:
    text = LEDGER_MANIFEST_TEXT.replace(
        _METRICS_TAIL,
        _METRICS_TAIL + "    sources:\n      type: array\n      items:\n        type: link\n",
    )
    from exomem.collection_store.preview import bound_writer

    writer = bound_writer(vault_root)
    if writer is not None:
        writer.revise_collection(LEDGER_COLLECTION_PATH, manifest_text=text, why="declare provenance",
                                 **writer.inspect_collection(LEDGER_COLLECTION_PATH)["lifecycle_guards"])
        return
    path = vault_root / LEDGER_COLLECTION_PATH
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def vault_root(tmp_path: Path, request, monkeypatch):
    _evidence(tmp_path, EVIDENCE, EVIDENCE_B)
    if getattr(request, "param", "files") == "files":
        setup_ledger_collection(tmp_path)
        yield tmp_path
    else:
        from exomem.collection_store import connection
        from exomem.collection_store.preview import preview_store

        monkeypatch.setenv("EXOMEM_COLLECTION_STORE_PREVIEW", "1")
        with connection.open_writer(tmp_path.with_suffix(".sqlite"), lease_check=lambda: True) as handle, library_scope():
            with preview_store(tmp_path, handle) as writer:
                writer.create_collection(LEDGER_COLLECTION_PATH, LEDGER_MANIFEST_TEXT, why="capture")
                yield tmp_path


@pytest.fixture
def scalar_vault_root(vault_root):
    """Row/source-policy comparisons need complete fields, independently of the ledger's open-object tests."""
    from exomem.collection_store.preview import bound_writer

    text = LEDGER_MANIFEST_TEXT.replace("    details:\n      type: object\n" + _METRICS_TAIL, "")
    writer = bound_writer(vault_root)
    if writer is None:
        (vault_root / LEDGER_COLLECTION_PATH).write_text(text, encoding="utf-8")
    else:
        writer.revise_collection(LEDGER_COLLECTION_PATH, manifest_text=text, why="declare scalar source-policy fixture",
                                 **writer.inspect_collection(LEDGER_COLLECTION_PATH)["lifecycle_guards"])
    return vault_root


def _scalar_rows(count, *, start=0):
    return [{"item": {key: value for key, value in row["item"].items() if key not in {"details", "metrics"}}}
            for row in _rows(count, start=start)]


def _outcomes(result: dict[str, Any]) -> list[str]:
    return [row["outcome"] for row in result["rows"]]


def _audit_gap(vault_root, manifest):
    from exomem.collection_store.preview import bound_writer

    writer = bound_writer(vault_root)
    return writer.inspect_collection(manifest)["audit"] if writer else records.inspect_audit_gap(vault_root, manifest)


def test_twenty_four_rows_commit_under_one_guard_as_one_verified_chain(vault_root: Path) -> None:
    start = _hash(vault_root)
    result = _bulk(vault_root, _rows(24), expected_container_hash=start)

    assert result["committed"] is True
    assert _outcomes(result) == ["inserted"] * 24
    assert result["counts"] == {"inserted": 24, "updated": 0, "unchanged": 0, "rejected": 0}
    assert [row["index"] for row in result["rows"]] == list(range(24))
    assert result["batch_id"]
    assert len({row["transition_id"] for row in result["rows"]}) == 24
    assert result["first_transition"] == result["rows"][0]["transition_id"]
    assert result["last_transition"] == result["rows"][-1]["transition_id"]
    assert result["before_container_hash"] == start
    assert result["after_container_hash"] == _hash(vault_root)

    manifest = _manifest(vault_root)
    assert manifest.audit_head == result["last_transition"]
    assert records.inspect_audit_gap(vault_root, manifest) == {"status": "ok", "gaps": []}
    assert len(record_formats.load_adapter(vault_root, manifest).read().records) == 24
    log = (vault_root / "Knowledge Base/log.md").read_text(encoding="utf-8")
    for row in result["rows"]:
        assert row["transition_id"] in log


@pytest.mark.parametrize("vault_root", ["files", "store"], indirect=True)
def test_the_response_hash_chains_the_next_guarded_call(vault_root: Path) -> None:
    first = _bulk(vault_root, _rows(3))
    second = _bulk(
        vault_root, _rows(3, start=3), expected_container_hash=first["after_container_hash"]
    )
    assert second["committed"] is True
    assert _audit_gap(vault_root, _manifest(vault_root))["status"] == "ok"


@pytest.mark.parametrize("vault_root", ["files", "store"], indirect=True)
def test_a_stale_guard_refuses_the_whole_batch(vault_root: Path) -> None:
    before = _state(vault_root)
    with pytest.raises(collections.CollectionError, match="STALE_RECORD"):
        _bulk(vault_root, _rows(3), expected_container_hash="0" * 64)
    assert _state(vault_root) == before


@pytest.mark.parametrize("vault_root", ["files", "store"], indirect=True)
def test_resubmitting_committed_rows_is_all_unchanged_and_writes_nothing(
    vault_root: Path,
) -> None:
    first = _bulk(vault_root, _rows(5))
    before = _state(vault_root)
    again = _bulk(
        vault_root, _rows(5), expected_container_hash=first["after_container_hash"]
    )
    assert _outcomes(again) == ["unchanged"] * 5
    assert again["committed"] is False
    assert _state(vault_root) == before


@pytest.mark.parametrize("vault_root", ["files", "store"], indirect=True)
def test_a_changed_row_is_updated_without_the_single_append_conflict(vault_root: Path) -> None:
    first = _bulk(vault_root, _rows(3))
    rows = _rows(3)
    rows[1]["item"]["word_count"] = 999
    result = _bulk(vault_root, rows, expected_container_hash=first["after_container_hash"])

    assert _outcomes(result) == ["unchanged", "updated", "unchanged"]
    assert result["committed"] is True
    assert result["counts"]["updated"] == 1
    manifest = _manifest(vault_root)
    stored = {
        record.values["slug"]: record
        for record in record_formats.load_adapter(vault_root, manifest).read().records
    }
    assert stored["entry-001"].values["word_count"] == 999
    assert stored["entry-000"].values["word_count"] == 42
    assert _audit_gap(vault_root, manifest)["status"] == "ok"


def test_an_existing_row_appends_three_optional_fields_in_request_order(
    vault_root: Path,
) -> None:
    initial = ledger_item()
    appended = {name: initial.pop(name) for name in ("metrics", "channels", "details")}
    first = _bulk(vault_root, [{"item": initial}])
    manifest = _manifest(vault_root)
    record = record_formats.load_adapter(vault_root, manifest).read().records[0]
    item_path = vault_root / record.source.path
    original, _body, _marker = vault.parse_frontmatter(
        item_path.read_text(encoding="utf-8"), strict=True
    )
    assert all(not manifest.schema.fields[name].required for name in appended)
    assert all(name not in original for name in appended)
    rows = [{"item": {**initial, **appended}}]

    result = _bulk(vault_root, rows, expected_container_hash=first["after_container_hash"])

    assert _outcomes(result) == ["updated"]
    assert result["committed"] is True
    assert result["counts"]["updated"] == 1
    frontmatter, _body, _marker = vault.parse_frontmatter(
        item_path.read_text(encoding="utf-8"), strict=True
    )
    assert frontmatter == {**original, **appended}
    assert list(frontmatter) == list(original) + ["metrics", "channels", "details"]
    updated_hash = result["after_container_hash"]
    assert updated_hash == _hash(vault_root)
    before_replay = _state(vault_root)

    replay = _bulk(vault_root, rows, expected_container_hash=updated_hash)

    assert _outcomes(replay) == ["unchanged"]
    assert replay["committed"] is False
    assert replay["before_container_hash"] == replay["after_container_hash"] == updated_hash
    assert _hash(vault_root) == updated_hash
    assert _state(vault_root) == before_replay


@pytest.mark.parametrize("vault_root", ["files", "store"], indirect=True)
def test_an_update_replaces_values_and_keeps_the_body_unless_one_is_supplied(
    vault_root: Path,
) -> None:
    first = _bulk(vault_root, [{"item": ledger_item(), "body": "Original body."}])
    changed = ledger_item(word_count=7)
    second = _bulk(
        vault_root,
        [{"item": changed}],
        expected_container_hash=first["after_container_hash"],
    )
    assert _outcomes(second) == ["updated"]
    manifest = _manifest(vault_root)
    record = record_formats.load_adapter(vault_root, manifest).read().records[0]
    assert record.values["word_count"] == 7
    assert "Original body." in record.body

    third = _bulk(
        vault_root,
        [{"item": changed, "body": "New body."}],
        expected_container_hash=second["after_container_hash"],
    )
    assert _outcomes(third) == ["updated"]
    record = record_formats.load_adapter(vault_root, manifest).read().records[0]
    assert "New body." in record.body and "Original body." not in record.body


@pytest.mark.parametrize("vault_root", ["files", "store"], indirect=True)
def test_a_repeated_natural_key_in_one_request_rejects_the_later_row(vault_root: Path) -> None:
    rows = _rows(2) + _rows(1)
    result = _bulk(vault_root, rows, on_reject="skip")
    assert _outcomes(result) == ["inserted", "inserted", "rejected"]
    assert result["rows"][2]["code"] == "DUPLICATE_ROW_KEY"
    assert result["rows"][2]["duplicate_of"] == 0


@pytest.mark.parametrize("vault_root", ["files", "store"], indirect=True)
def test_abort_writes_nothing_and_reports_every_would_be_outcome(vault_root: Path) -> None:
    rows = _rows(4)
    rows[2]["item"]["bogus"] = "undeclared"
    before = _state(vault_root)
    result = _bulk(vault_root, rows)

    assert result["committed"] is False
    assert _outcomes(result) == ["inserted", "inserted", "rejected", "inserted"]
    assert result["rows"][2]["code"] == "SCHEMA_UNKNOWN_FIELD"
    assert any(field["field"] == "bogus" for field in result["rows"][2]["fields"])
    assert all("transition_id" not in row for row in result["rows"])
    assert _state(vault_root) == before


@pytest.mark.parametrize("vault_root", ["files", "store"], indirect=True)
def test_skip_commits_the_accepted_rows_once_and_reports_the_rejected(vault_root: Path) -> None:
    rows = _rows(4)
    rows[2]["item"]["bogus"] = "undeclared"
    result = _bulk(vault_root, rows, on_reject="skip")

    assert result["committed"] is True
    assert result["counts"] == {"inserted": 3, "updated": 0, "unchanged": 0, "rejected": 1}
    assert result["rows"][2]["outcome"] == "rejected"
    manifest = _manifest(vault_root)
    assert len(record_formats.load_adapter(vault_root, manifest).read().records) == 3
    assert _audit_gap(vault_root, manifest)["status"] == "ok"


@pytest.mark.parametrize("vault_root", ["files", "store"], indirect=True)
def test_a_batch_that_would_change_nothing_leaves_the_audit_head_alone(vault_root: Path) -> None:
    head = _manifest(vault_root).audit_head
    rows = _rows(2)
    rows[0]["item"]["bogus"] = "x"
    rows[1]["item"]["bogus"] = "y"
    result = _bulk(vault_root, rows, on_reject="skip")
    assert result["committed"] is False
    assert _manifest(vault_root).audit_head == head


def test_a_publication_failure_leaves_nothing_and_the_retry_succeeds(
    vault_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _state(vault_root)
    real = vault.batch_atomic_write

    def failing(*args: Any, **kwargs: Any) -> Any:
        raise OSError("disk full")

    monkeypatch.setattr(vault, "batch_atomic_write", failing)
    with pytest.raises(collections.CollectionError):
        _bulk(vault_root, _rows(3))
    monkeypatch.setattr(vault, "batch_atomic_write", real)
    assert _state(vault_root) == before
    assert _bulk(vault_root, _rows(3))["committed"] is True


@pytest.mark.parametrize("vault_root", ["files", "store"], indirect=True)
def test_a_row_without_provenance_is_rejected(vault_root: Path) -> None:
    result = _bulk(vault_root, _rows(2), source=None, on_reject="skip")
    assert _outcomes(result) == ["rejected", "rejected"]
    assert {row["code"] for row in result["rows"]} == {"SOURCE_REQUIRED"}


@pytest.mark.parametrize("vault_root", ["files", "store"], indirect=True)
def test_a_row_source_overrides_the_batch_source(vault_root: Path) -> None:
    rows = _rows(2)
    rows[1]["source"] = EVIDENCE_B
    result = _bulk(vault_root, rows)
    assert result["rows"][0]["source"] == EVIDENCE
    assert result["rows"][1]["source"] == EVIDENCE_B


@pytest.mark.parametrize("vault_root", ["files", "store"], indirect=True)
def test_missing_and_non_preserved_sources_reject_identically(vault_root: Path) -> None:
    (vault_root / "Notes").mkdir()
    (vault_root / "Notes/plain.md").write_text("# plain\n", encoding="utf-8")
    rows = _rows(2)
    rows[0]["source"] = "Knowledge Base/Evidence/absent.md"
    rows[1]["source"] = "Notes/plain.md"
    result = _bulk(vault_root, rows, on_reject="skip")
    assert _outcomes(result) == ["rejected", "rejected"]
    first, second = result["rows"]
    assert first["code"] == second["code"] == "SOURCE_NOT_FOUND"
    assert {key: value for key, value in first.items() if key not in {"index", "source"}} == {
        key: value for key, value in second.items() if key not in {"index", "source"}
    }


@pytest.mark.parametrize("vault_root", ["files", "store"], indirect=True)
def test_a_declared_sources_field_receives_the_verified_reference(vault_root: Path) -> None:
    _with_sources_field(vault_root)
    result = _bulk(vault_root, _rows(2))
    assert result["committed"] is True
    manifest = _manifest(vault_root)
    for record in record_formats.load_adapter(vault_root, manifest).read().records:
        assert record.values["sources"] == [f"[[{EVIDENCE.removesuffix('.md')}]]"]


def test_without_a_sources_field_the_reference_rides_each_event_rationale(
    vault_root: Path,
) -> None:
    result = _bulk(vault_root, _rows(2))
    log = (vault_root / "Knowledge Base/log.md").read_text(encoding="utf-8")
    assert log.count(f"bulk {result['batch_id']} ") == 2
    assert log.count(f"src {EVIDENCE}") == 2
    assert "entry-000" not in log and "word_count" not in log


def test_events_and_receipt_never_carry_row_values(vault_root: Path) -> None:
    rows = _rows(2, exact_text="Distinctive private-looking text.")
    result = _bulk(vault_root, rows)
    log = (vault_root / "Knowledge Base/log.md").read_text(encoding="utf-8")
    assert "Distinctive private-looking text" not in log
    assert "Distinctive private-looking text" not in repr(result)


def test_rows_without_a_complete_natural_key_insert_only_and_rerun_duplicates(
    tmp_path: Path,
) -> None:
    setup_ledger_collection(tmp_path)
    _evidence(tmp_path)
    (tmp_path / LEDGER_COLLECTION_PATH).write_text(
        LEDGER_MANIFEST_TEXT.replace("natural_key: [published_on, slug]", "natural_key: [slug]")
        .replace("    slug:\n      type: string\n      required: true\n", "    slug:\n      type: string\n"),
        encoding="utf-8",
    )
    rows = [{"item": {k: v for k, v in ledger_item().items() if k != "slug"}} for _ in range(2)]
    first = _bulk(tmp_path, rows)
    assert _outcomes(first) == ["inserted", "inserted"]
    assert {row["identity"] for row in first["rows"]} == {"generated"}
    second = _bulk(tmp_path, rows, expected_container_hash=first["after_container_hash"])
    assert _outcomes(second) == ["inserted", "inserted"]
    manifest = _manifest(tmp_path)
    assert len(record_formats.load_adapter(tmp_path, manifest).read().records) == 4


@pytest.mark.parametrize("vault_root", ["files", "store"], indirect=True)
def test_natural_keyed_rows_report_their_identity_kind(vault_root: Path) -> None:
    assert {row["identity"] for row in _bulk(vault_root, _rows(2))["rows"]} == {"natural-key"}


def test_more_rows_than_the_cap_refuse_whole(vault_root: Path) -> None:
    before = _state(vault_root)
    rows = records.BULK_UPSERT_MAX_ROWS * 2 + 1
    with pytest.raises(collections.CollectionError) as info:
        _bulk(vault_root, _rows(rows))
    error = info.value
    assert error.code == "BULK_UPSERT_TOO_MANY_ROWS"
    assert "split" in str(error) and "after_container_hash" in str(error)
    assert error.details == {
        "rows": rows,
        "maximum": records.BULK_UPSERT_MAX_ROWS,
        "batches_needed": 3,
        "chain_with": "after_container_hash",
    }
    assert _state(vault_root) == before


def test_a_full_cap_batch_commits_within_the_lease_budget(vault_root: Path) -> None:
    # The cap exists to keep one call's hold of the single writer lease near 5 s.
    # This runs a full-cap publish and fails loudly on a gross regression; its
    # duration in the CI log is the measurement on the runner class.
    import time

    started = time.perf_counter()
    result = _bulk(vault_root, _rows(records.BULK_UPSERT_MAX_ROWS))
    held = time.perf_counter() - started
    assert result["committed"] is True
    assert records.inspect_audit_gap(vault_root, _manifest(vault_root))["status"] == "ok"
    assert held < 12.0, f"a {records.BULK_UPSERT_MAX_ROWS}-row call held the lease {held:.1f}s"


def test_the_row_cap_admits_exactly_its_bound(vault_root: Path) -> None:
    # No source, so every row is rejected in planning: the cap admits exactly its
    # bound without publishing that many files.
    cap = records.BULK_UPSERT_MAX_ROWS
    result = _bulk(vault_root, _rows(cap), source=None, on_reject="skip")
    assert result["counts"]["rejected"] == cap
    assert result["committed"] is False


@pytest.mark.parametrize("rows", [[], "rows", [1], [{"nope": 1}], [{}]])
@pytest.mark.parametrize("vault_root", ["files", "store"], indirect=True)
def test_a_malformed_row_list_is_refused_or_rejected(vault_root: Path, rows: Any) -> None:
    before = _state(vault_root)
    try:
        result = _bulk(vault_root, rows, on_reject="skip")
    except collections.CollectionError as error:
        assert error.code in {"INVALID_BULK_ROWS", "BULK_UPSERT_EMPTY"}
    else:
        assert result["committed"] is False
        assert {row["outcome"] for row in result["rows"]} == {"rejected"}
    assert _state(vault_root) == before


def test_a_batch_past_the_chain_depth_budget_refuses_up_front(
    vault_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(records, "_MAX_AUDIT_CHAIN_DEPTH", 6)
    first = _bulk(vault_root, _rows(4))
    before = _state(vault_root)
    with pytest.raises(collections.CollectionError, match="BULK_UPSERT_AUDIT_DEPTH") as info:
        _bulk(vault_root, _rows(3, start=10), expected_container_hash=first["after_container_hash"])
    assert info.value.details == {"events_used": 4, "events_needed": 3, "budget": 6}
    assert _state(vault_root) == before
    exactly = _bulk(
        vault_root, _rows(2, start=10), expected_container_hash=first["after_container_hash"]
    )
    assert exactly["committed"] is True


def test_the_item_ceiling_refuses_whole(vault_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(records, "_MAX_ITEM_FILES", 3)
    before = _state(vault_root)
    with pytest.raises(collections.CollectionError, match="COLLECTION_ITEM_LIMIT"):
        _bulk(vault_root, _rows(4))
    assert _state(vault_root) == before


@pytest.mark.parametrize("vault_root", ["files", "store"], indirect=True)
def test_an_overlong_rationale_rejects_only_that_row(vault_root: Path) -> None:
    rows = _rows(2)
    rows[1]["source"] = "Knowledge Base/Evidence/" + "x" * 200 + ".md"
    _evidence(vault_root, rows[1]["source"])
    result = _bulk(vault_root, rows, why="w" * 430, on_reject="skip")
    assert _outcomes(result) == ["inserted", "rejected"]
    assert result["rows"][1]["code"] == "AUDIT_RATIONALE_TOO_LONG"


@pytest.mark.parametrize("vault_root", ["files", "store"], indirect=True)
def test_the_command_action_runs_a_bulk_upsert(vault_root: Path) -> None:
    result = record_memory(
        vault_root,
        action="bulk_upsert",
        collection=LEDGER_COLLECTION_PATH,
        rows=_rows(3),
        source=EVIDENCE,
        why="import a normalised export",
        expected_container_hash=_hash(vault_root),
    )
    assert result["counts"]["inserted"] == 3


@pytest.mark.parametrize("vault_root", ["files", "store"], indirect=True)
def test_the_command_action_refuses_arguments_of_other_actions(vault_root: Path) -> None:
    with pytest.raises(OpError, match="arguments do not match"):
        record_memory(
            vault_root,
            action="bulk_upsert",
            collection=LEDGER_COLLECTION_PATH,
            rows=_rows(1),
            item=ledger_item(),
            why="x",
            expected_container_hash=_hash(vault_root),
        )
    with pytest.raises(OpError, match="arguments do not match"):
        record_memory(
            vault_root,
            action="bulk_upsert",
            collection=LEDGER_COLLECTION_PATH,
            rows=_rows(1),
            why="x",
        )


def test_describe_teaches_bulk_upsert(vault_root: Path) -> None:
    text = repr(record_memory(vault_root, action="describe"))
    for needle in ("bulk_upsert", "2048", "unchanged", "on_reject", "max_rows_why", "target_rows"):
        assert needle in text
    limits = record_memory(vault_root, action="describe")["bulk_upsert"]["limits"]
    assert limits["max_rows"] == 50 and limits["target_rows"] == 500


def _x3_rows() -> list[dict[str, Any]]:
    return [
        {
            "item": {
                "occurred_on": f"2026-08-{day:02d}",
                "title": f"Session {day}",
                "status": "completed",
                "movements": [{"movement": "Deadlift", "band": "grey", "repetitions": "22"}],
            }
        }
        for day in (3, 4, 5)
    ]


def test_a_markdown_log_takes_inserts_then_updates_as_one_verified_chain(tmp_path: Path) -> None:
    from record_fixtures import copy_x3_fixture

    fixture = copy_x3_fixture(tmp_path)
    (tmp_path / "Knowledge Base/log.md").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "Knowledge Base/log.md").write_text("# Activity\n", encoding="utf-8")
    _evidence(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    start = record_formats.load_adapter(tmp_path, manifest).read().source_versions[-1].hash
    baseline = len(record_formats.load_adapter(tmp_path, manifest).read().records)

    first = records.bulk_upsert_records(
        tmp_path, manifest.path, rows=_x3_rows(), why="import sessions",
        expected_container_hash=start, source=EVIDENCE,
    )
    assert _outcomes(first) == ["inserted"] * 3
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    assert records.inspect_audit_gap(tmp_path, manifest)["status"] == "ok"
    assert len(record_formats.load_adapter(tmp_path, manifest).read().records) == baseline + 3

    rows = _x3_rows()
    rows[0]["item"]["movements"][0]["repetitions"] = "23"
    rows.append(
        {
            "item": {
                "occurred_on": "2026-08-06",
                "title": "Session 6",
                "status": "completed",
                "movements": [{"movement": "Row", "band": "grey", "repetitions": "10"}],
            }
        }
    )
    second = records.bulk_upsert_records(
        tmp_path, manifest.path, rows=rows, why="import sessions",
        expected_container_hash=first["after_container_hash"], source=EVIDENCE,
    )
    assert _outcomes(second) == ["updated", "unchanged", "unchanged", "inserted"]
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    assert records.inspect_audit_gap(tmp_path, manifest)["status"] == "ok"
    stored = record_formats.load_adapter(tmp_path, manifest).read()
    assert len(stored.records) == baseline + 4
    assert stored.source_versions[-1].hash == second["after_container_hash"]
    revised = next(r for r in stored.records if r.values.get("title") == "Session 3")
    assert revised.values["movements"][0]["repetitions"] == "23"


def _write_rule(vault_root: Path, name: str, scope_id: str, rule_id: str, paths: str, ceiling: int) -> None:
    root = vault_root / "Knowledge Base" / "_Governance"
    (root / "scopes").mkdir(parents=True, exist_ok=True)
    (root / "rules").mkdir(parents=True, exist_ok=True)
    (root / "scopes" / f"{name}.yaml").write_text(
        f"governance_version: 1\nid: {scope_id}\nname: {name}\npaths: [\"{paths}\"]\n",
        encoding="utf-8",
    )
    (root / "rules" / f"{name}.yaml").write_text(
        f"governance_version: 1\nid: {rule_id}\nscope_ids: [\"{scope_id}\"]\n"
        f"audience: external\nceiling: {ceiling}\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize("vault_root", ["files", "store"], indirect=True)
def test_a_withheld_item_makes_the_whole_collection_read_as_absent(scalar_vault_root: Path) -> None:
    from exomem.governance.principal import RequestPrincipal, request_scope

    vault_root = scalar_vault_root
    first = _bulk(vault_root, _scalar_rows(2))
    _write_rule(
        vault_root, "records", "01ARZ3NDEKTSV4RRFFQ69G5FAV", "01ARZ3NDEKTSV4RRFFQ69G5FB0",
        "Records/**", 6,
    )
    _write_rule(
        vault_root, "hidden", "01ARZ3NDEKTSV4RRFFQ69G5FZZ", "01ARZ3NDEKTSV4RRFFQ69G5FZY",
        "Records/Publications/Entries/**", 0,
    )
    before = _state(vault_root)
    with request_scope(RequestPrincipal(audience_id="external", surface="mcp")):
        with pytest.raises(collections.CollectionError, match="COLLECTION_NOT_FOUND"):
            _bulk(
                vault_root, _scalar_rows(2, start=10), expected_container_hash=first["after_container_hash"]
            )
    assert _state(vault_root) == before


@pytest.mark.parametrize("vault_root", ["files", "store"], indirect=True)
def test_a_withheld_source_reads_exactly_like_an_absent_one(scalar_vault_root: Path) -> None:
    from exomem.governance.principal import RequestPrincipal, owner_principal, request_scope

    vault_root = scalar_vault_root
    _write_rule(
        vault_root, "records", "01ARZ3NDEKTSV4RRFFQ69G5FAV", "01ARZ3NDEKTSV4RRFFQ69G5FB0",
        "**", 6,
    )
    _write_rule(
        vault_root, "hidden", "01ARZ3NDEKTSV4RRFFQ69G5FZZ", "01ARZ3NDEKTSV4RRFFQ69G5FZY",
        "Evidence/import-b.md", 0,
    )
    rows = _scalar_rows(3)
    rows[1]["source"] = EVIDENCE_B
    rows[2]["source"] = "Knowledge Base/Evidence/absent.md"
    with request_scope(owner_principal(surface="mcp")):
        visible = _bulk(vault_root, [rows[1]], on_reject="skip")
    assert visible["rows"][0]["outcome"] == "inserted"
    with request_scope(RequestPrincipal(audience_id="external", surface="mcp")):
        result = _bulk(vault_root, rows[:1] + [{**rows[1], "item": _scalar_rows(1, start=10)[0]["item"]}, rows[2]],
                       on_reject="skip", expected_container_hash=visible["after_container_hash"])
    assert result["rows"][0]["outcome"] == "inserted"
    hidden, absent = result["rows"][1], result["rows"][2]
    assert hidden["outcome"] == absent["outcome"] == "rejected"
    assert hidden["code"] == absent["code"] == "SOURCE_NOT_FOUND"
    assert {k: v for k, v in hidden.items() if k not in {"index", "source"}} == {
        k: v for k, v in absent.items() if k not in {"index", "source"}
    }


def test_an_unparsed_history_reports_the_depth_check_as_unknown(
    vault_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = records._audit_events
    monkeypatch.setattr(
        records,
        "_audit_events",
        lambda *a, **k: records._AuditEvents((), False),
    )
    result = _bulk(vault_root, _rows(2))
    monkeypatch.setattr(records, "_audit_events", real)
    assert result["committed"] is True
    assert result["depth_check"] == "unknown"
    assert result["depth_check_reason"] == "history_incomplete"
    healthy = _bulk(
        vault_root, _rows(2, start=10), expected_container_hash=result["after_container_hash"]
    )
    assert "depth_check" not in healthy
