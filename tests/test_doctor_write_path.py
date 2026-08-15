"""Doctor write-path observability: deferred-queue FAIL tier, graph_sync state,
orphaned graph-rebuild temporaries, and write-path env kill-switch facts.

Incident context: the deferred-index queue reached 3,724 items (2,116 semantic
+ 1,608 full) on a 2,872-page vault and doctor only WARNed, with remediation
text claiming reconciliation drains it automatically — which is false
(reconcile.reconcile() never calls index_sync.drain_deferred_work). At that
depth the vault degrades from ~300 ms to ~60 s per operation.

This file adds the new write-path checks. The pre-existing WARN-tier
regression test lives in tests/test_deferred_work_drain.py
(test_doctor_warns_on_deferred_queue_fraction) and is intentionally NOT
touched here (out of scope) — it is re-verified by the acceptance run instead.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from exomem import deferred_index
from exomem import doctor as doctor_module
from exomem import graph_sync
from exomem.kbdir import kb_dirname


def _seed_lexical_pages(vault: Path, count: int) -> None:
    from exomem import lexstore

    sidecar = lexstore.lexical_path(vault)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(sidecar) as connection:
        connection.execute("CREATE TABLE pages (id INTEGER PRIMARY KEY)")
        connection.executemany(
            "INSERT INTO pages DEFAULT VALUES", [() for _ in range(count)]
        )


# --- 1. Deferred-queue FAIL tier --------------------------------------------


def test_deep_queue_fails_names_index_command_and_never_claims_automatic_drain(
    vault: Path,
) -> None:
    """The incident scenario: 2,116 semantic + 1,608 full queued on 2,872 pages."""
    _seed_lexical_pages(vault, 2872)
    deferred_index.add_full(
        vault, [f"Knowledge Base/deferred/full-{i}.md" for i in range(1608)]
    )
    deferred_index.add(
        vault, [f"Knowledge Base/deferred/sem-{i}.md" for i in range(2116)]
    )

    check = doctor_module._check_deferred_index_backlog(vault)

    assert check.status == "fail"
    assert check.remediation is not None
    assert f'exomem index --vault "{vault}" --scope vault' in check.remediation
    haystack = f"{check.message} {check.remediation}".lower()
    assert "automatic" not in haystack
    assert check.details["semantic_upserts"] == 2116
    assert check.details["full_upserts"] == 1608


def test_small_healthy_queue_still_warns_not_fails(vault: Path) -> None:
    """No regression for the existing check id: 11/100 pages (11%) stays WARN."""
    _seed_lexical_pages(vault, 100)
    deferred_index.add_full(
        vault, [f"Knowledge Base/deferred/{i}.md" for i in range(11)]
    )

    check = doctor_module._check_deferred_index_backlog(vault)

    assert check.status == "warn"
    assert "automatic" not in check.message.lower()
    assert "automatic" not in (check.remediation or "").lower()


def test_a_few_dozen_backlog_on_a_small_vault_does_not_fail(vault: Path) -> None:
    """A healthy transient of a few dozen items must never hit the FAIL tier,
    even on a small vault where it blows the 10% relative WARN fraction."""
    _seed_lexical_pages(vault, 300)
    deferred_index.add_full(
        vault, [f"Knowledge Base/deferred/{i}.md" for i in range(40)]
    )

    check = doctor_module._check_deferred_index_backlog(vault)

    assert check.status == "warn"  # 40/300 = 13.3% > 10% warn fraction, but not FAIL


def test_tiny_backlog_passes(vault: Path) -> None:
    _seed_lexical_pages(vault, 500)
    deferred_index.add_full(vault, ["Knowledge Base/deferred/one.md"])

    check = doctor_module._check_deferred_index_backlog(vault)

    assert check.status == "pass"


def test_no_vault_configured_passes(vault: Path) -> None:
    check = doctor_module._check_deferred_index_backlog(None)

    assert check.status == "pass"


# --- 2. graph_sync state -----------------------------------------------------


def test_graph_sync_current_passes(vault: Path) -> None:
    check = doctor_module._check_graph_sync_state(vault)

    assert check.status == "pass"
    assert check.details["generation"] == 0


def test_graph_sync_recovery_required_fails_with_canned_remediation(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoint = graph_sync.GraphSyncCheckpoint.create(
        generation=4,
        mutation_id="0123456789abcdef01234567",
        paths=(),
        created_paths=(),
        scope="full",
    )
    monkeypatch.setattr(
        graph_sync, "status", lambda _root: {"state": "recovery_required", "generation": 4}
    )
    monkeypatch.setattr(
        graph_sync, "checkpoint_state", lambda _root: ("valid", checkpoint)
    )

    check = doctor_module._check_graph_sync_state(vault)

    assert check.status == "fail"
    expected = graph_sync.committed_graph_failure(checkpoint)["graph_sync_remediation"]
    assert check.remediation == expected
    assert check.details["generation"] == 4


def test_graph_sync_unavailable_fails_with_honest_message(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        graph_sync, "status", lambda _root: {"state": "unavailable", "generation": 2}
    )
    monkeypatch.setattr(graph_sync, "checkpoint_state", lambda _root: ("absent", None))

    check = doctor_module._check_graph_sync_state(vault)

    assert check.status == "fail"
    assert "unavailable" in check.message.lower()


def test_graph_sync_malformed_checkpoint_fails_even_if_state_looks_current(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        graph_sync, "status", lambda _root: {"state": "current", "generation": 1}
    )
    monkeypatch.setattr(graph_sync, "checkpoint_state", lambda _root: ("malformed", None))

    check = doctor_module._check_graph_sync_state(vault)

    assert check.status == "fail"
    assert "malformed" in check.message.lower()


def test_graph_sync_no_vault_configured_passes() -> None:
    check = doctor_module._check_graph_sync_state(None)

    assert check.status == "pass"


# --- 3. Orphaned rebuild temporaries (graph + lexical families) -------------
#
# Live diagnosis (brief amendment): `.graph-rebuild-*` behaved correctly in
# production (transient, always cleaned; final count 0). The actual unbounded
# leak is the OTHER rebuild-temp family lexstore.rebuild_atomic() creates
# (lexstore.py:2469): `.lexical.sqlite.rebuild-<32-hex>.tmp[-wal|-shm|-journal]`.
# Live state: 74 files / 5.84 GiB across 30 abandoned rebuilds — nothing reaps
# them. This check covers both families and reports each separately.


def _rebuild_name(seed: int, suffix: str = "") -> str:
    return f".graph-rebuild-{seed:064x}-{seed:024x}.sqlite{suffix}"


def _lexical_rebuild_name(digest: str, suffix: str = "") -> str:
    return f".lexical.sqlite.rebuild-{digest}.tmp{suffix}"


def test_orphan_count_excludes_user_copy_and_sums_bytes(vault: Path) -> None:
    kb = vault / kb_dirname()
    (kb / _rebuild_name(1)).write_bytes(b"x" * 1000)
    (kb / _rebuild_name(2, "-wal")).write_bytes(b"y" * 2000)
    (kb / ".graph-rebuild-user-copy.sqlite").write_bytes(b"z" * 5000)

    check = doctor_module._check_rebuild_temp_orphans(vault)

    assert check.details["count"] == 2
    assert check.details["total_bytes"] == 3000
    assert check.details["graph_rebuild"] == {"count": 2, "total_bytes": 3000}
    assert check.details["lexical_rebuild"] == {"count": 0, "total_bytes": 0}


def test_single_small_orphan_passes(vault: Path) -> None:
    kb = vault / kb_dirname()
    (kb / _rebuild_name(3)).write_bytes(b"x" * 10)

    check = doctor_module._check_rebuild_temp_orphans(vault)

    assert check.status == "pass"
    assert check.details["count"] == 1


def test_a_couple_of_orphans_warns(vault: Path) -> None:
    kb = vault / kb_dirname()
    (kb / _rebuild_name(4)).write_bytes(b"x" * 10)
    (kb / _rebuild_name(5, "-journal")).write_bytes(b"y" * 10)

    check = doctor_module._check_rebuild_temp_orphans(vault)

    assert check.status == "warn"


def test_many_orphans_fail_by_count(vault: Path) -> None:
    """The incident: 18 orphans (well below the byte threshold each) must FAIL."""
    kb = vault / kb_dirname()
    for i in range(18):
        (kb / _rebuild_name(100 + i)).write_bytes(b"\0" * 16)

    check = doctor_module._check_rebuild_temp_orphans(vault)

    assert check.status == "fail"
    assert check.details["count"] == 18


def test_one_large_orphan_fails_by_bytes(vault: Path) -> None:
    kb = vault / kb_dirname()
    path = kb / _rebuild_name(200)
    size = 60 * 1024 * 1024  # 60 MB, above the 50 MB fail threshold
    with path.open("wb") as fh:
        fh.seek(size - 1)
        fh.write(b"\0")

    check = doctor_module._check_rebuild_temp_orphans(vault)

    assert check.status == "fail"
    assert check.details["total_bytes"] == size


def test_no_orphans_passes(vault: Path) -> None:
    check = doctor_module._check_rebuild_temp_orphans(vault)

    assert check.status == "pass"
    assert check.details["count"] == 0
    assert check.details["graph_rebuild"] == {"count": 0, "total_bytes": 0}
    assert check.details["lexical_rebuild"] == {"count": 0, "total_bytes": 0}


def test_orphans_no_vault_configured_passes() -> None:
    check = doctor_module._check_rebuild_temp_orphans(None)

    assert check.status == "pass"


def test_single_fresh_lexical_rebuild_temp_passes(vault: Path) -> None:
    """A single in-flight lexical rebuild temporary must not FAIL."""
    kb = vault / kb_dirname()
    (kb / _lexical_rebuild_name("a" * 32)).write_bytes(b"x" * 10)

    check = doctor_module._check_rebuild_temp_orphans(vault)

    assert check.status == "pass"
    assert check.details["lexical_rebuild"] == {"count": 1, "total_bytes": 10}
    assert check.details["graph_rebuild"] == {"count": 0, "total_bytes": 0}


def test_lexical_rebuild_orphans_counted_separately_and_summed(vault: Path) -> None:
    kb = vault / kb_dirname()
    (kb / _lexical_rebuild_name("b" * 32)).write_bytes(b"x" * 1000)
    (kb / _lexical_rebuild_name("c" * 32, "-wal")).write_bytes(b"y" * 2000)

    check = doctor_module._check_rebuild_temp_orphans(vault)

    assert check.details["lexical_rebuild"] == {"count": 2, "total_bytes": 3000}
    assert check.details["graph_rebuild"] == {"count": 0, "total_bytes": 0}
    assert check.details["count"] == 2
    assert check.details["total_bytes"] == 3000
    assert check.status == "warn"


def test_lexical_rebuild_many_stale_files_fail(vault: Path) -> None:
    """The live-diagnosis state: 74 abandoned lexical-rebuild temporaries."""
    kb = vault / kb_dirname()
    for i in range(74):
        (kb / _lexical_rebuild_name(f"{i:032x}")).write_bytes(b"\0" * 16)

    check = doctor_module._check_rebuild_temp_orphans(vault)

    assert check.status == "fail"
    assert check.details["lexical_rebuild"]["count"] == 74
    assert check.details["graph_rebuild"] == {"count": 0, "total_bytes": 0}


def test_mixed_graph_and_lexical_orphans_report_both_families(vault: Path) -> None:
    kb = vault / kb_dirname()
    (kb / _rebuild_name(6)).write_bytes(b"x" * 500)
    (kb / _lexical_rebuild_name("d" * 32)).write_bytes(b"y" * 700)

    check = doctor_module._check_rebuild_temp_orphans(vault)

    assert check.details["graph_rebuild"] == {"count": 1, "total_bytes": 500}
    assert check.details["lexical_rebuild"] == {"count": 1, "total_bytes": 700}
    assert check.details["count"] == 2
    assert check.details["total_bytes"] == 1200


# --- 4. Write-path env kill-switch facts -------------------------------------


def test_write_path_flags_all_enabled_passes(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_CORPUS_CACHE", raising=False)
    monkeypatch.delenv("EXOMEM_DISABLE_EVENT_INDEXES", raising=False)
    monkeypatch.delenv("EXOMEM_DISABLE_GRAPH_SCHEDULING", raising=False)

    check = doctor_module._check_write_path_env_flags(vault)

    assert check.status == "pass"
    assert check.details["corpus_context_cache_enabled"] is True
    assert check.details["event_indexes_enabled"] is True
    assert check.details["graph_scheduling_enabled"] is True


def test_corpus_cache_disabled_alone_warns(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_DISABLE_CORPUS_CACHE", "1")
    monkeypatch.delenv("EXOMEM_DISABLE_EVENT_INDEXES", raising=False)
    monkeypatch.delenv("EXOMEM_DISABLE_GRAPH_SCHEDULING", raising=False)

    check = doctor_module._check_write_path_env_flags(vault)

    assert check.status == "warn"
    assert "EXOMEM_DISABLE_CORPUS_CACHE" in check.message


def test_graph_scheduling_disabled_alone_warns(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_CORPUS_CACHE", raising=False)
    monkeypatch.delenv("EXOMEM_DISABLE_EVENT_INDEXES", raising=False)
    monkeypatch.setenv("EXOMEM_DISABLE_GRAPH_SCHEDULING", "1")

    check = doctor_module._check_write_path_env_flags(vault)

    assert check.status == "warn"
    assert "EXOMEM_DISABLE_GRAPH_SCHEDULING" in check.message


def test_event_indexes_disabled_with_nonempty_queue_surfaces(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_DISABLE_EVENT_INDEXES", "1")
    deferred_index.add_full(vault, ["Knowledge Base/deferred/x.md"])

    check = doctor_module._check_write_path_env_flags(vault)

    assert check.status in {"warn", "fail"}
    assert "EXOMEM_DISABLE_EVENT_INDEXES" in check.message
    assert check.details["full_upserts"] == 1


def test_event_indexes_disabled_with_deep_queue_fails(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_DISABLE_EVENT_INDEXES", "1")
    deferred_index.add_full(
        vault, [f"Knowledge Base/deferred/{i}.md" for i in range(400)]
    )

    check = doctor_module._check_write_path_env_flags(vault)

    assert check.status == "fail"


def test_write_path_flags_without_vault_reports_env_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_CORPUS_CACHE", raising=False)
    monkeypatch.delenv("EXOMEM_DISABLE_EVENT_INDEXES", raising=False)
    monkeypatch.delenv("EXOMEM_DISABLE_GRAPH_SCHEDULING", raising=False)

    check = doctor_module._check_write_path_env_flags(None)

    assert check.status == "pass"


# --- Wiring into the ordered report ------------------------------------------


def test_new_checks_are_wired_into_the_ordered_report(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_EVENT_INDEXES", raising=False)
    monkeypatch.delenv("EXOMEM_DISABLE_GRAPH_SCHEDULING", raising=False)

    report = doctor_module.doctor(vault=str(vault))

    ids = [c.id for c in report.checks]
    assert "graph_sync.state" in ids
    assert "rebuild_temp.orphans" in ids
    assert "write_path.env_flags" in ids
    i = ids.index("deferred_index_backlog")
    assert ids[i + 1 : i + 4] == [
        "graph_sync.state",
        "rebuild_temp.orphans",
        "write_path.env_flags",
    ]
    assert report.success is True
