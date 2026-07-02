"""stale_review audit check: surface old + cold + low-inbound conclusions.

A measurement-only review queue — never decays or down-ranks `find`. Signals
are derived from frontmatter dates, the wikilink graph, and the query log (no
new sidecar). AND-gated as a filter (no confidence score). The access signal is
gated for determinism (`EXOMEM_DISABLE_RELEVANCE_CHECK`, set by the suite) and
treats a missing log as 'unknown', never fabricated zero-access.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path

import pytest

from exomem import audit as audit_module
from exomem import find as find_module

_TODAY = dt.date(2026, 6, 27)


def _seed_note(
    vault: Path,
    rel: str,
    *,
    type_: str = "research-note",
    status: str = "active",
    updated: str = "2024-01-01",
    tags: list[str] | None = None,
    body: str = "Body.",
) -> None:
    """Write a minimal parseable compiled page at KB-relative `rel` (with .md)."""
    p = vault / "Knowledge Base" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    tag_line = f"tags: [{', '.join(tags)}]\n" if tags else ""
    p.write_text(
        f"---\ntype: {type_}\nstatus: {status}\n"
        f"created: 2024-01-01\nupdated: {updated}\n{tag_line}---\n\n# {rel}\n\n{body}\n",
        encoding="utf-8",
    )


def _stale_findings(vault: Path, *, today: dt.date = _TODAY):
    report = audit_module.audit(vault, categories=["stale_review"], today=today)
    return report.findings


# ---------------- registration ----------------


def test_stale_review_registered() -> None:
    assert "stale_review" in audit_module.ALL_CATEGORIES


def test_unknown_category_still_rejected(vault: Path) -> None:
    with pytest.raises(ValueError):
        audit_module.audit(vault, categories=["does_not_exist"])


# ---------------- the AND-gate ----------------


def test_flags_old_cold_lowlink_conclusion(vault: Path) -> None:
    _seed_note(vault, "Notes/Insights/forgotten.md", type_="insight", updated="2024-01-01")
    find_module.clear_cache()
    f = [x for x in _stale_findings(vault) if "forgotten" in x.path]
    assert len(f) == 1, [x.as_dict() for x in _stale_findings(vault)]
    finding = f[0]
    assert finding.severity == "info"
    assert finding.meta["age_days"] > 365
    assert finding.meta["inbound_count"] == 0
    assert finding.meta["access_count"] is None  # relevance signal gated in-suite
    assert "Still true?" in finding.detail


def test_recent_note_not_flagged(vault: Path) -> None:
    _seed_note(vault, "Notes/Insights/fresh.md", type_="insight", updated="2026-06-01")
    find_module.clear_cache()
    assert not [x for x in _stale_findings(vault) if "fresh" in x.path]


def test_well_linked_note_not_flagged(vault: Path) -> None:
    _seed_note(vault, "Notes/Insights/hub-target.md", type_="insight", updated="2024-01-01")
    # Two inbound links → inbound_count 2 > max(1), so it drops out of the gate.
    _seed_note(
        vault, "Notes/Insights/linker-a.md", type_="insight", updated="2026-06-01",
        body="See [[Knowledge Base/Notes/Insights/hub-target]].",
    )
    _seed_note(
        vault, "Notes/Insights/linker-b.md", type_="insight", updated="2026-06-01",
        body="Also [[Knowledge Base/Notes/Insights/hub-target]].",
    )
    find_module.clear_cache()
    assert not [x for x in _stale_findings(vault) if "hub-target" in x.path]


def test_frequently_surfaced_note_not_flagged(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_note(vault, "Notes/Insights/popular.md", type_="insight", updated="2024-01-01")
    find_module.clear_cache()
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "queries.jsonl").write_text(
        "\n".join(
            json.dumps({
                "ts": "2026-06-01T10:00:00",
                "query": f"q{i}",
                "top_k": [{"path": "Knowledge Base/Notes/Insights/popular"}],
            })
            for i in range(3)
        ) + "\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("EXOMEM_DISABLE_RELEVANCE_CHECK", raising=False)
    monkeypatch.setattr(audit_module, "_RELEVANCE_LOGS_DIR", logs)
    # 3 surfacings > max_access(1) → excluded.
    assert not [x for x in _stale_findings(vault) if "popular" in x.path]


def test_no_logs_treats_access_as_unknown_not_zero(vault: Path) -> None:
    # Suite gate is on → access signal unavailable. The old, low-inbound note is
    # STILL flagged (gate falls back to age AND inbound), and meta is honest that
    # access is unknown (None), never a fabricated 0.
    _seed_note(vault, "Notes/Insights/cold.md", type_="insight", updated="2024-01-01")
    find_module.clear_cache()
    f = [x for x in _stale_findings(vault) if "cold" in x.path]
    assert len(f) == 1
    assert f[0].meta["access_count"] is None


# ---------------- scope / exclusions ----------------


def test_excludes_superseded_archived_sources_hubs_snapshots_readonly(
    vault: Path,
) -> None:
    _seed_note(
        vault, "Notes/Research/Personal/sup.md", status="superseded", updated="2024-01-01"
    )
    _seed_note(
        vault, "Notes/Research/Personal/arc.md", status="archived", updated="2024-01-01"
    )
    # Convention-named hub (slug suffix) + snapshot (slug suffix) — expected to drift.
    _seed_note(
        vault, "Notes/Research/Personal/engine-architecture.md", updated="2024-01-01"
    )
    _seed_note(
        vault, "Notes/Research/Personal/2024-old-snapshot.md", updated="2024-01-01"
    )
    # Hub by tag.
    _seed_note(
        vault, "Notes/Insights/hub-tagged.md", type_="insight",
        updated="2024-01-01", tags=["hub"],
    )
    # Raw source (not a conclusion type, and append-only tier).
    src = vault / "Knowledge Base" / "Sources" / "Other" / "old-src.md"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(
        "---\ntype: source\nstatus: active\ncreated: 2024-01-01\nupdated: 2024-01-01\n"
        "---\n\n# src\n\nBody.\n",
        encoding="utf-8",
    )
    # Readonly-tier conclusion (curated tree folded into the KB).
    (vault / "Knowledge Base" / "_access.yaml").write_text(
        "readonly:\n  - Products\nexcluded: []\n", encoding="utf-8"
    )
    _seed_note(vault, "Products/old-ro.md", type_="insight", updated="2024-01-01")
    find_module.clear_cache()

    paths = [x.path for x in _stale_findings(vault)]
    for needle in (
        "sup", "arc", "engine-architecture", "old-snapshot",
        "hub-tagged", "old-src", "old-ro",
    ):
        assert not any(needle in p for p in paths), f"{needle!r} leaked: {paths}"


def test_excludes_index_files(vault: Path) -> None:
    # An index.md is never a review candidate even if old.
    idx = vault / "Knowledge Base" / "Notes" / "Insights" / "index.md"
    idx.parent.mkdir(parents=True, exist_ok=True)
    idx.write_text(
        "---\ntype: insight\nstatus: active\ncreated: 2024-01-01\nupdated: 2024-01-01\n"
        "---\n\n# index\n",
        encoding="utf-8",
    )
    find_module.clear_cache()
    assert not [
        x for x in _stale_findings(vault) if x.path.endswith("Notes/Insights/index.md")
    ]


# ---------------- meta / determinism ----------------


def test_meta_carries_signals_and_oldest_first(vault: Path) -> None:
    _seed_note(vault, "Notes/Insights/older.md", type_="insight", updated="2023-01-01")
    _seed_note(vault, "Notes/Insights/newer.md", type_="insight", updated="2024-06-01")
    find_module.clear_cache()
    findings = [
        x for x in _stale_findings(vault)
        if x.path.endswith(("older.md", "newer.md"))
    ]
    by_path = {x.path.rsplit("/", 1)[-1]: x for x in findings}
    assert set(by_path) == {"older.md", "newer.md"}
    for x in by_path.values():
        assert set(x.meta) >= {"age_days", "age_bucket", "inbound_count", "access_count"}
        assert x.severity == "info"
    # Oldest first.
    paths = [x.path for x in findings]
    assert paths.index(by_path["older.md"].path) < paths.index(by_path["newer.md"].path)


def test_today_injection_age_math(vault: Path) -> None:
    _seed_note(vault, "Notes/Insights/dated.md", type_="insight", updated="2025-01-01")
    find_module.clear_cache()
    f = next(x for x in _stale_findings(vault) if "dated" in x.path)
    assert f.meta["age_days"] == (_TODAY - dt.date(2025, 1, 1)).days


def test_no_date_note_skipped(vault: Path) -> None:
    # A conclusion with no parseable updated/created can't be age-judged → skip
    # (never fabricate an age).
    p = vault / "Knowledge Base" / "Notes" / "Insights" / "undated.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "---\ntype: insight\nstatus: active\n---\n\n# undated\n\nBody.\n",
        encoding="utf-8",
    )
    find_module.clear_cache()
    assert not [x for x in _stale_findings(vault) if "undated" in x.path]


# ---------------- ACT-R dormancy ordering ----------------


def test_activation_formula() -> None:
    d = 0.5
    # No events → None (never accessed).
    assert audit_module._activation(None, d) is None
    assert audit_module._activation([], d) is None
    # Single event → ln(w · Δt^−d).
    assert audit_module._activation([(10.0, 1.0)], d) == math.log(1.0 * 10.0 ** -d)
    # Recent (small Δt) is MORE active (higher B) than an old (large Δt) event.
    assert (
        audit_module._activation([(1.0, 1.0)], d)
        > audit_module._activation([(100.0, 1.0)], d)
    )
    # A citation (w=3) is more active than a surfacing (w=1) at the same Δt.
    assert (
        audit_module._activation([(10.0, 3.0)], d)
        > audit_module._activation([(10.0, 1.0)], d)
    )


def test_dormancy_sort_most_dormant_first(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Three old, zero-inbound conclusions. With the access signal enabled, the
    # review queue reorders them most-dormant first:
    # never-accessed → surfaced-long-ago → surfaced-recently.
    _seed_note(vault, "Notes/Insights/never.md", type_="insight", updated="2024-01-01")
    _seed_note(vault, "Notes/Insights/longago.md", type_="insight", updated="2024-01-01")
    _seed_note(vault, "Notes/Insights/recent.md", type_="insight", updated="2024-01-01")
    find_module.clear_cache()
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "queries.jsonl").write_text(
        "\n".join([
            json.dumps({
                "ts": "2025-01-01T10:00:00",
                "query": "q-longago",
                "top_k": [{"path": "Knowledge Base/Notes/Insights/longago"}],
            }),
            json.dumps({
                "ts": "2026-06-20T10:00:00",
                "query": "q-recent",
                "top_k": [{"path": "Knowledge Base/Notes/Insights/recent"}],
            }),
        ]) + "\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("EXOMEM_DISABLE_RELEVANCE_CHECK", raising=False)
    monkeypatch.setattr(audit_module, "_RELEVANCE_LOGS_DIR", logs)

    findings = [
        x for x in _stale_findings(vault)
        if x.path.rsplit("/", 1)[-1] in ("never.md", "longago.md", "recent.md")
    ]
    order = [x.path.rsplit("/", 1)[-1] for x in findings]
    assert order == ["never.md", "longago.md", "recent.md"], order

    by = {x.path.rsplit("/", 1)[-1]: x for x in findings}
    # never-accessed → activation None, zero observations, sorts to the top.
    assert by["never.md"].meta["activation"] is None
    assert by["never.md"].meta["access_observations"] == 0
    # Both surfaced notes have a populated activation; recent is LESS dormant.
    assert by["longago.md"].meta["activation"] is not None
    assert by["recent.md"].meta["activation"] is not None
    assert by["recent.md"].meta["activation"] > by["longago.md"].meta["activation"]
    assert by["longago.md"].meta["access_observations"] == 1


def test_gated_fallback_activation_none_and_oldest_first(vault: Path) -> None:
    # Suite default has EXOMEM_DISABLE_RELEVANCE_CHECK set → no access signal.
    # activation/observations are None for all, and findings fall back to the
    # age-based oldest-first sort (no crash).
    _seed_note(vault, "Notes/Insights/g-older.md", type_="insight", updated="2023-01-01")
    _seed_note(vault, "Notes/Insights/g-newer.md", type_="insight", updated="2024-06-01")
    find_module.clear_cache()
    findings = [
        x for x in _stale_findings(vault)
        if x.path.endswith(("g-older.md", "g-newer.md"))
    ]
    by_path = {x.path.rsplit("/", 1)[-1]: x for x in findings}
    assert set(by_path) == {"g-older.md", "g-newer.md"}
    for x in by_path.values():
        assert x.meta["activation"] is None
        assert x.meta["access_observations"] is None
    paths = [x.path for x in findings]
    assert paths.index(by_path["g-older.md"].path) < paths.index(
        by_path["g-newer.md"].path
    )


# ---------------- docs guard ----------------


def test_server_audit_docstring_lists_stale_review_and_drops_five() -> None:
    # The audit tool description now lives in the command registry (op_audit's
    # docstring), which drives the MCP/REST/CLI/OpenAPI surfaces.
    import exomem.commands as commands

    src = Path(commands.__file__).read_text(encoding="utf-8")
    assert "stale_review" in src
    assert "one of the five above" not in src
