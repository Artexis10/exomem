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

import os
import sqlite3
import time
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


def test_graph_sync_recovery_required_fails_with_augmented_canned_remediation(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Correction round (MINOR): committed_graph_failure()'s own default text
    ("Run reconcile to recover the derived graph.") names an internal op, not
    a runnable command. The printed remediation must always include the
    actual runnable command too, without discarding the canned text."""
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
    canned = graph_sync.committed_graph_failure(checkpoint)["graph_sync_remediation"]
    assert check.remediation is not None
    assert canned in check.remediation
    assert "exomem maintain --reconcile" in check.remediation
    assert check.details["generation"] == 4


def test_graph_sync_recovery_required_keeps_an_already_specific_remediation(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the canned remediation already names the runnable command (e.g. a
    threaded GraphRebuildRegistrationError.remediation), it is used as-is
    rather than duplicated."""
    checkpoint = graph_sync.GraphSyncCheckpoint.create(
        generation=7,
        mutation_id="0123456789abcdef01234567",
        paths=(),
        created_paths=(),
        scope="full",
    )
    specific = "Run `exomem maintain --reconcile` after clearing the stuck lease."
    monkeypatch.setattr(
        graph_sync, "status", lambda _root: {"state": "recovery_required", "generation": 7}
    )
    monkeypatch.setattr(
        graph_sync, "checkpoint_state", lambda _root: ("valid", checkpoint)
    )
    monkeypatch.setattr(
        graph_sync,
        "committed_graph_failure",
        lambda _checkpoint: {"graph_sync_remediation": specific},
    )

    check = doctor_module._check_graph_sync_state(vault)

    assert check.status == "fail"
    assert check.remediation == specific


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
# Live state: 74 files / 5.84 GiB across 30 abandoned rebuilds spanning
# July 25-Aug 15 — nothing reaps them. This check covers both families and
# reports each separately.
#
# Correction round (MAJOR): a matching name alone is not evidence of an
# orphan — a legitimate in-flight rebuild matches the same name pattern and
# can legitimately be large (the incident's own abandoned files averaged
# ~79 MB). Every test below is explicit about mtime: `_age_file` back-dates a
# file past `_REBUILD_TEMP_STALE_AGE_SECONDS` to simulate an abandoned
# rebuild; a file left un-aged simulates one still in flight.


def _rebuild_name(seed: int, suffix: str = "") -> str:
    return f".graph-rebuild-{seed:064x}-{seed:024x}.sqlite{suffix}"


def _lexical_rebuild_name(digest: str, suffix: str = "") -> str:
    return f".lexical.sqlite.rebuild-{digest}.tmp{suffix}"


def _age_file(path: Path, *, minutes_ago: float) -> None:
    """Back-date a file's mtime (and atime) by `minutes_ago` minutes."""
    ts = time.time() - minutes_ago * 60
    os.utime(path, (ts, ts))


# A comfortable margin past doctor_module._REBUILD_TEMP_STALE_AGE_SECONDS
# (60 minutes), used throughout to mark a file as an abandoned orphan.
_STALE_MINUTES = 120


def test_orphan_count_excludes_user_copy_and_sums_bytes(vault: Path) -> None:
    kb = vault / kb_dirname()
    a = kb / _rebuild_name(1)
    b = kb / _rebuild_name(2, "-wal")
    a.write_bytes(b"x" * 1000)
    b.write_bytes(b"y" * 2000)
    (kb / ".graph-rebuild-user-copy.sqlite").write_bytes(b"z" * 5000)
    _age_file(a, minutes_ago=_STALE_MINUTES)
    _age_file(b, minutes_ago=_STALE_MINUTES)

    check = doctor_module._check_rebuild_temp_orphans(vault)

    assert check.details["count"] == 2
    assert check.details["total_bytes"] == 3000
    assert check.details["stale_count"] == 2
    assert check.details["stale_bytes"] == 3000
    assert check.details["graph_rebuild"] == {
        "count": 2,
        "total_bytes": 3000,
        "stale_count": 2,
        "stale_bytes": 3000,
    }
    assert check.details["lexical_rebuild"]["count"] == 0


def test_single_stale_small_orphan_passes(vault: Path) -> None:
    kb = vault / kb_dirname()
    path = kb / _rebuild_name(3)
    path.write_bytes(b"x" * 10)
    _age_file(path, minutes_ago=_STALE_MINUTES)

    check = doctor_module._check_rebuild_temp_orphans(vault)

    assert check.status == "pass"
    assert check.details["count"] == 1
    assert check.details["stale_count"] == 1


def test_a_couple_of_stale_orphans_warn(vault: Path) -> None:
    kb = vault / kb_dirname()
    a = kb / _rebuild_name(4)
    b = kb / _rebuild_name(5, "-journal")
    a.write_bytes(b"x" * 10)
    b.write_bytes(b"y" * 10)
    _age_file(a, minutes_ago=_STALE_MINUTES)
    _age_file(b, minutes_ago=_STALE_MINUTES)

    check = doctor_module._check_rebuild_temp_orphans(vault)

    assert check.status == "warn"
    assert check.details["stale_count"] == 2


def test_many_stale_orphans_fail_by_count(vault: Path) -> None:
    """The incident: 18 stale orphans (well below the byte threshold each) fail."""
    kb = vault / kb_dirname()
    for i in range(18):
        path = kb / _rebuild_name(100 + i)
        path.write_bytes(b"\0" * 16)
        _age_file(path, minutes_ago=_STALE_MINUTES)

    check = doctor_module._check_rebuild_temp_orphans(vault)

    assert check.status == "fail"
    assert check.details["count"] == 18
    assert check.details["stale_count"] == 18


def test_one_large_stale_orphan_fails_by_bytes(vault: Path) -> None:
    kb = vault / kb_dirname()
    path = kb / _rebuild_name(200)
    size = 60 * 1024 * 1024  # 60 MB, above the 50 MB fail threshold
    with path.open("wb") as fh:
        fh.seek(size - 1)
        fh.write(b"\0")
    _age_file(path, minutes_ago=_STALE_MINUTES)

    check = doctor_module._check_rebuild_temp_orphans(vault)

    assert check.status == "fail"
    assert check.details["total_bytes"] == size
    assert check.details["stale_bytes"] == size


def test_large_fresh_rebuild_temp_passes(vault: Path) -> None:
    """MAJOR fix, red-first: a large temp with a FRESH mtime is routine for an
    in-flight rebuild (the incident's abandoned files averaged ~79 MB), not an
    orphan. Size alone must never fail a temp that is still being written.
    (Pre-fix this failed identically to test_one_large_stale_orphan_fails_by_bytes
    above, since size was the only signal — see .task/RESULT.md for the
    verbatim red run.)"""
    kb = vault / kb_dirname()
    path = kb / _rebuild_name(201)
    size = 60 * 1024 * 1024  # 60 MB — above the 50 MB fail-by-bytes threshold
    with path.open("wb") as fh:
        fh.seek(size - 1)
        fh.write(b"\0")
    # Freshly written: mtime is "now" (no aging applied).

    check = doctor_module._check_rebuild_temp_orphans(vault)

    assert check.status == "pass"
    assert check.details["total_bytes"] == size
    assert check.details["stale_bytes"] == 0
    assert check.details["stale_count"] == 0


def test_no_orphans_passes(vault: Path) -> None:
    check = doctor_module._check_rebuild_temp_orphans(vault)

    assert check.status == "pass"
    assert check.details["count"] == 0
    assert check.details["stale_count"] == 0
    assert check.details["graph_rebuild"]["count"] == 0
    assert check.details["lexical_rebuild"]["count"] == 0


def test_orphans_no_vault_configured_passes() -> None:
    check = doctor_module._check_rebuild_temp_orphans(None)

    assert check.status == "pass"


def test_single_fresh_lexical_rebuild_temp_passes(vault: Path) -> None:
    """A single in-flight lexical rebuild temporary must not FAIL."""
    kb = vault / kb_dirname()
    (kb / _lexical_rebuild_name("a" * 32)).write_bytes(b"x" * 10)

    check = doctor_module._check_rebuild_temp_orphans(vault)

    assert check.status == "pass"
    assert check.details["lexical_rebuild"]["count"] == 1
    assert check.details["lexical_rebuild"]["total_bytes"] == 10
    assert check.details["lexical_rebuild"]["stale_count"] == 0
    assert check.details["graph_rebuild"]["count"] == 0


def test_fresh_lexical_rebuild_temps_of_any_count_or_size_do_not_warn_or_fail(
    vault: Path,
) -> None:
    """A fresh in-flight temporary must pass regardless of count or size —
    neither alone is evidence of an orphan without staleness."""
    kb = vault / kb_dirname()
    for i, size in enumerate((1_000, 2_000, 70 * 1024 * 1024)):
        path = kb / _lexical_rebuild_name(f"{i:032x}")
        with path.open("wb") as fh:
            if size > 1_000_000:
                fh.seek(size - 1)
                fh.write(b"\0")
            else:
                fh.write(b"x" * size)
        # No aging: every file keeps a fresh "just written" mtime.

    check = doctor_module._check_rebuild_temp_orphans(vault)

    assert check.status == "pass"
    assert check.details["lexical_rebuild"]["count"] == 3
    assert check.details["stale_count"] == 0


def test_stale_lexical_rebuild_orphans_counted_separately_and_summed(
    vault: Path,
) -> None:
    kb = vault / kb_dirname()
    a = kb / _lexical_rebuild_name("b" * 32)
    b = kb / _lexical_rebuild_name("c" * 32, "-wal")
    a.write_bytes(b"x" * 1000)
    b.write_bytes(b"y" * 2000)
    _age_file(a, minutes_ago=_STALE_MINUTES)
    _age_file(b, minutes_ago=_STALE_MINUTES)

    check = doctor_module._check_rebuild_temp_orphans(vault)

    assert check.details["lexical_rebuild"]["count"] == 2
    assert check.details["lexical_rebuild"]["total_bytes"] == 3000
    assert check.details["lexical_rebuild"]["stale_count"] == 2
    assert check.details["graph_rebuild"]["count"] == 0
    assert check.details["count"] == 2
    assert check.details["stale_count"] == 2
    assert check.status == "warn"


def test_mixed_fresh_and_stale_files_count_only_the_stale(vault: Path) -> None:
    """Extend tests per the correction: a mix of fresh and stale temporaries in
    the same family must count only the stale ones toward WARN/FAIL."""
    kb = vault / kb_dirname()
    stale_a = kb / _lexical_rebuild_name("1" * 32)
    stale_b = kb / _lexical_rebuild_name("2" * 32, "-wal")
    fresh = kb / _lexical_rebuild_name("3" * 32, "-shm")
    stale_a.write_bytes(b"x" * 100)
    stale_b.write_bytes(b"y" * 200)
    fresh.write_bytes(b"z" * 300)
    _age_file(stale_a, minutes_ago=_STALE_MINUTES)
    _age_file(stale_b, minutes_ago=_STALE_MINUTES)
    # `fresh` is left un-aged.

    check = doctor_module._check_rebuild_temp_orphans(vault)

    assert check.details["lexical_rebuild"]["count"] == 3
    assert check.details["lexical_rebuild"]["total_bytes"] == 600
    assert check.details["lexical_rebuild"]["stale_count"] == 2
    assert check.details["lexical_rebuild"]["stale_bytes"] == 300
    assert check.details["count"] == 3
    assert check.details["stale_count"] == 2
    assert check.details["stale_bytes"] == 300
    assert check.status == "warn"  # 2 stale -> warn, the fresh one is invisible to it


def test_lexical_rebuild_many_stale_files_fail(vault: Path) -> None:
    """The live-diagnosis state: 74 abandoned lexical-rebuild temporaries
    (production incident spanned July 25-Aug 15 — days to weeks old, comfortably
    past the 60-minute staleness threshold; 2 hours is used here for speed)."""
    kb = vault / kb_dirname()
    for i in range(74):
        path = kb / _lexical_rebuild_name(f"{i:032x}")
        path.write_bytes(b"\0" * 16)
        _age_file(path, minutes_ago=_STALE_MINUTES)

    check = doctor_module._check_rebuild_temp_orphans(vault)

    assert check.status == "fail"
    assert check.details["lexical_rebuild"]["count"] == 74
    assert check.details["lexical_rebuild"]["stale_count"] == 74
    assert check.details["graph_rebuild"]["count"] == 0


def test_mixed_graph_and_lexical_orphans_report_both_families(vault: Path) -> None:
    kb = vault / kb_dirname()
    a = kb / _rebuild_name(6)
    b = kb / _lexical_rebuild_name("d" * 32)
    a.write_bytes(b"x" * 500)
    b.write_bytes(b"y" * 700)
    _age_file(a, minutes_ago=_STALE_MINUTES)
    _age_file(b, minutes_ago=_STALE_MINUTES)

    check = doctor_module._check_rebuild_temp_orphans(vault)

    assert check.details["graph_rebuild"]["count"] == 1
    assert check.details["graph_rebuild"]["total_bytes"] == 500
    assert check.details["lexical_rebuild"]["count"] == 1
    assert check.details["lexical_rebuild"]["total_bytes"] == 700
    assert check.details["count"] == 2
    assert check.details["total_bytes"] == 1200
    assert check.details["stale_count"] == 2


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
    assert ids[i + 1 : i + 5] == [
        "graph_sync.state",
        "state.placement",
        "rebuild_temp.orphans",
        "write_path.env_flags",
    ]
    assert report.success is True
