"""Unit tests for the thinking-evolution view (`evolution`).

Torch-free: builds a tmp vault with two supersession chains + a standalone note, plus a
`log.md` carrying the recorded transition reasons, and exercises `evolution()` directly.
Dates are deliberately out of pointer order to prove ordering follows the supersession
spine, not `updated:`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import evolution
from exomem import find as find_module

# --- chain 1: widget-cache  A -> B -> C  (A's date is LATER than C's, on purpose) ---

A = """\
---
type: insight
status: superseded
superseded_by: "[[Knowledge Base/Notes/Insights/widget-cache-v2]]"
updated: 2026-09-01
---
# Widget cache (v1)

We do not cache widget reads; every request hits the store.

## Claim
No caching keeps it simple and correct.
"""

B = """\
---
type: insight
status: superseded
superseded_by: "[[Knowledge Base/Notes/Insights/widget-cache-v3]]"
supersedes: "[[Knowledge Base/Notes/Insights/widget-cache]]"
updated: 2026-03-01
---
# Widget cache (v2)

We cache widget reads with a 60s TTL after the latency spike.

## Claim
A short TTL cuts p99 latency without much staleness.
"""

C = """\
---
type: insight
status: active
supersedes: "[[Knowledge Base/Notes/Insights/widget-cache-v2]]"
updated: 2026-03-15
---
# Widget cache (v3)

We cache widget reads with an LRU keyed by tenant; TTL was too blunt.

## Claim
LRU beats TTL on our measured hit-rate.
"""

# standalone widget note — matches the query but was never superseded
D = """\
---
type: insight
status: active
updated: 2026-02-20
---
# Widget metrics

Track widget read latency p50/p99.
"""

# --- chain 2: widget-rollout  E -> F ---

E = """\
---
type: insight
status: superseded
superseded_by: "[[Knowledge Base/Notes/Insights/widget-rollout-v2]]"
updated: 2026-02-15
---
# Widget rollout (v1)

Big-bang rollout of the widget service.
"""

F = """\
---
type: insight
status: active
supersedes: "[[Knowledge Base/Notes/Insights/widget-rollout]]"
updated: 2026-02-16
---
# Widget rollout (v2)

Phased rollout of the widget service, 5% at a time.
"""

# log.md — supersession reasons recorded under the NEW page (replace's contract)
LOG = """\
# Activity log

## [2026-04-01] edit | Notes/Insights/widget-cache-v3
Tidied prose; notes that v3 supersedes the TTL approach. No claim change.

## [2026-03-15] replace | Notes/Insights/widget-cache-v3
Supersedes `Notes/Insights/widget-cache-v2` via exomem. LRU beat TTL on the measured hit-rate.

## [2026-03-01] replace | Notes/Insights/widget-cache-v2
Supersedes `Notes/Insights/widget-cache` via exomem. Switched to a TTL after the latency spike.

## [2026-02-16] replace | Notes/Insights/widget-rollout-v2
Supersedes `Notes/Insights/widget-rollout` via exomem. Phased rollout replaced big-bang.
"""

INS = "Knowledge Base/Notes/Insights"
A_P, B_P, C_P = f"{INS}/widget-cache.md", f"{INS}/widget-cache-v2.md", f"{INS}/widget-cache-v3.md"
D_P = f"{INS}/widget-metrics.md"
E_P, F_P = f"{INS}/widget-rollout.md", f"{INS}/widget-rollout-v2.md"


def _write(vault: Path, rel: str, body: str) -> None:
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


@pytest.fixture
def chains(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    _write(vault, A_P, A)
    _write(vault, B_P, B)
    _write(vault, C_P, C)
    _write(vault, D_P, D)
    _write(vault, E_P, E)
    _write(vault, F_P, F)
    _write(vault, "Knowledge Base/log.md", LOG)
    find_module.clear_cache()
    return vault


def _timeline_for(result: dict, head: str) -> dict:
    return next(t for t in result["timelines"] if t["chain_id"] == head)


def test_chain_is_one_ordered_timeline(chains: Path) -> None:
    result = evolution.evolution(chains, query="widget cache")
    tl = _timeline_for(result, C_P)
    # Ordered by the supersession spine A -> B -> C, NOT by date (A's date is latest).
    assert [v["path"] for v in tl["versions"]] == [A_P, B_P, C_P]
    assert tl["span"]["n_versions"] == 3


def test_transitions_carry_recorded_reasons_head_is_null(chains: Path) -> None:
    tl = _timeline_for(evolution.evolution(chains, query="widget cache"), C_P)
    a, b, c = tl["versions"]
    assert "TTL after the latency spike" in a["transition"]["reason"]   # A -> B reason (from B's log)
    assert "LRU beat TTL" in b["transition"]["reason"]                  # B -> C reason (from C's log)
    # The `replace` op is picked, NOT the newer `edit` entry that merely mentions "supersede".
    assert "Tidied prose" not in b["transition"]["reason"]
    assert c["transition"] is None                                      # active head


def test_versions_carry_structural_claims(chains: Path) -> None:
    tl = _timeline_for(evolution.evolution(chains, query="widget cache"), C_P)
    head = tl["versions"][-1]
    assert head["claims"]["lede"].startswith("We cache widget reads with an LRU")
    assert any("Claim:" in s for s in head["claims"]["sections"])
    assert "Claim" in head["claims"]["outline"]


def test_standalone_note_is_excluded(chains: Path) -> None:
    result = evolution.evolution(chains, query="widget")
    all_paths = {v["path"] for t in result["timelines"] for v in t["versions"]}
    assert D_P not in all_paths  # never superseded → no evolution to show


def test_separate_chains_each_get_a_timeline(chains: Path) -> None:
    result = evolution.evolution(chains, query="widget")
    heads = {t["chain_id"] for t in result["timelines"]}
    assert C_P in heads and F_P in heads  # both chains surfaced
    assert len(result["timelines"]) == 2


def test_hits_on_same_chain_dedup(chains: Path) -> None:
    # "cache" matches A, B, and C — all one chain → exactly one timeline.
    result = evolution.evolution(chains, query="widget cache")
    cache_timelines = [t for t in result["timelines"] if t["chain_id"] == C_P]
    assert len(cache_timelines) == 1


def test_chains_cap_reports_truncation(chains: Path) -> None:
    result = evolution.evolution(chains, query="widget", limit=1)
    assert len(result["timelines"]) == 1
    assert any("chain" in t.lower() for t in result["truncation"])


def test_no_supersession_match_is_empty(chains: Path) -> None:
    result = evolution.evolution(chains, query="nonexistent-topic-zzz")
    assert result["timelines"] == []


def test_deterministic_on_rerun(chains: Path) -> None:
    assert evolution.evolution(chains, query="widget") == evolution.evolution(
        chains, query="widget"
    )


def test_path_specific_evolution_uses_only_selected_pointer_chain(chains: Path) -> None:
    result = evolution.evolution_for_path(chains, path=C_P)

    assert result["target_path"] == C_P
    assert len(result["timelines"]) == 1
    timeline = result["timelines"][0]
    assert timeline["chain_id"] == C_P
    assert [version["path"] for version in timeline["versions"]] == [A_P, B_P, C_P]
    assert F_P not in {version["path"] for version in timeline["versions"]}


def test_path_specific_evolution_single_version_is_honestly_empty(chains: Path) -> None:
    result = evolution.evolution_for_path(chains, path=D_P)

    assert result == {"target_path": D_P, "timelines": [], "truncation": []}


def test_path_specific_evolution_reports_version_cap(chains: Path) -> None:
    result = evolution.evolution_for_path(chains, path=C_P, max_versions=2)

    timeline = result["timelines"][0]
    assert [version["path"] for version in timeline["versions"]] == [B_P, C_P]
    assert timeline["span"]["n_versions"] == 3
    assert result["truncation"] == [
        f"timeline {C_P} capped at 2 versions (1 older not shown; raise max_versions)"
    ]
