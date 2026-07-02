"""usage-aware-find-ranking: opt-in prefer_used ACT-R boost.

Default ranking must stay byte-identical and usage-blind; the boost is
bounded, positive-only, reads+citations only, and fully explained in
signals. Tests write JSONL fixtures directly (the suite env disables live
log writing) and lift the suite's relevance gate per-test.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path

import pytest

from exomem import audit, commands, query_log, usage
from exomem import find as find_module


@pytest.fixture()
def logs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Tmp logs dir wired as the usage source, with the suite gate lifted."""
    d = tmp_path / "logs"
    d.mkdir()
    monkeypatch.delenv("EXOMEM_DISABLE_RELEVANCE_CHECK", raising=False)
    monkeypatch.setattr(query_log, "_LOG_DIR", d)
    usage.reset_usage_cache()
    yield d
    usage.reset_usage_cache()


def _log_read(logs_dir: Path, rel_path: str, ts: str) -> None:
    with (logs_dir / "reads.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": ts, "tool": "get", "read_path": rel_path}) + "\n")


def _log_cite(logs_dir: Path, rel_path: str, ts: str) -> None:
    with (logs_dir / "writes.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": ts, "tool": "note", "written_path": "x",
            "cited_sources": [rel_path],
        }) + "\n")


def _seed_tie_pages(vault: Path) -> tuple[str, str]:
    notes = vault / "Knowledge Base" / "Notes"
    notes.mkdir(parents=True, exist_ok=True)
    body = "# {t}\n\nusageprobe marker shared body text\n"
    (notes / "usage-tie-alpha.md").write_text(body.format(t="Alpha"), encoding="utf-8")
    (notes / "usage-tie-beta.md").write_text(body.format(t="Beta"), encoding="utf-8")
    return (
        "Knowledge Base/Notes/usage-tie-alpha.md",
        "Knowledge Base/Notes/usage-tie-beta.md",
    )


# ---- formula / primitives ----

def test_multiplier_bounds_and_monotonicity() -> None:
    cfg = find_module.RankingConfig()
    assert usage.usage_multiplier(None, cfg) == 1.0
    lo = usage.usage_multiplier(-5.0, cfg)
    mid = usage.usage_multiplier(0.0, cfg)
    hi = usage.usage_multiplier(8.0, cfg)
    assert 1.0 < lo < mid < hi <= cfg.usage_boost
    assert math.isclose(mid, 1.0 + (cfg.usage_boost - 1.0) * 0.5)


def test_dominance_invariants() -> None:
    cfg = find_module.RankingConfig()
    # Usage can never override the epistemic hierarchy or resurrect a
    # superseded tombstone above its active successor.
    assert cfg.usage_boost < cfg.compiled_boost
    assert cfg.superseded_penalty * cfg.usage_boost < 1.0


def test_horizon_cuts_old_events(logs_dir: Path) -> None:
    cfg = find_module.RankingConfig()
    old_ts = (dt.datetime.now() - dt.timedelta(days=400)).isoformat(timespec="seconds")
    _log_read(logs_dir, "Knowledge Base/Notes/old.md", old_ts)
    amap = usage.activation_map(cfg, logs_dir=logs_dir)
    assert amap == {}  # beyond usage_horizon_days=90 → no events at all


def test_malformed_log_lines_skipped(logs_dir: Path) -> None:
    ts = dt.datetime.now().isoformat(timespec="seconds")
    (logs_dir / "reads.jsonl").write_text(
        f'not json\n{{"ts": "{ts}", "read_path": "Knowledge Base/Notes/ok.md"}}\n',
        encoding="utf-8",
    )
    amap = usage.activation_map(find_module.RankingConfig(), logs_dir=logs_dir)
    assert list(amap) == ["notes/ok"]


def test_surfaced_events_carry_zero_weight(logs_dir: Path) -> None:
    """The rich-get-richer guard: find-surfacings alone produce NO activation."""
    ts = dt.datetime.now().isoformat(timespec="seconds")
    with (logs_dir / "queries.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": ts, "query": "q",
            "top_k": [{"path": "Knowledge Base/Notes/surfaced-only.md"}],
        }) + "\n")
    amap = usage.activation_map(find_module.RankingConfig(), logs_dir=logs_dir)
    assert amap == {}


def test_audit_delegation_parity(logs_dir: Path) -> None:
    """audit._stale_access_events must equal usage.access_events with audit's
    weights (the extraction is behavior-preserving)."""
    ts = dt.datetime.now().isoformat(timespec="seconds")
    _log_read(logs_dir, "Knowledge Base/Notes/parity.md", ts)
    _log_cite(logs_dir, "Knowledge Base/Notes/parity.md", ts)
    got = audit._stale_access_events(logs_dir=logs_dir)
    d, w_s, w_r, w_c = audit._stale_activation_params()
    want = usage.access_events(logs_dir, w_surfaced=w_s, w_read=w_r, w_cited=w_c)
    assert got == want
    assert audit._activation(got["notes/parity"], d) == usage.activation(
        want["notes/parity"], d
    )


# ---- find integration ----

def test_default_ranking_is_usage_blind(vault: Path, logs_dir: Path) -> None:
    alpha, beta = _seed_tie_pages(vault)
    ts = dt.datetime.now().isoformat(timespec="seconds")
    for _ in range(5):
        _log_read(logs_dir, beta, ts)
        _log_cite(logs_dir, beta, ts)
    hits = find_module.find(vault, query="usageprobe marker")
    ours = [h for h in hits if "usage-tie-" in h.path]
    assert [h.path for h in ours] == [alpha, beta]  # path-asc tie, no boost
    assert all(h.activation is None for h in hits)


def test_prefer_used_boosts_read_and_cited_page(vault: Path, logs_dir: Path) -> None:
    alpha, beta = _seed_tie_pages(vault)
    ts = dt.datetime.now().isoformat(timespec="seconds")
    for _ in range(5):
        _log_read(logs_dir, beta, ts)
        _log_cite(logs_dir, beta, ts)
    hits = find_module.find(vault, query="usageprobe marker", prefer_used=True)
    ours = [h for h in hits if "usage-tie-" in h.path]
    assert [h.path for h in ours] == [beta, alpha]  # boosted past the tie
    boosted = next(h for h in ours if h.path == beta)
    assert boosted.activation is not None
    assert boosted.usage_boost_applied is not None
    assert 1.0 < boosted.usage_boost_applied <= find_module.DEFAULT_RANKING.usage_boost
    d = boosted.as_dict()
    assert "activation" in d["signals"] and "usage_boost" in d["signals"]


def test_usage_never_creates_candidates(vault: Path, logs_dir: Path) -> None:
    notes = vault / "Knowledge Base" / "Notes"
    notes.mkdir(parents=True, exist_ok=True)
    hot = notes / "hot-but-irrelevant.md"
    hot.write_text("# Hot\n\ncompletely different topic entirely\n", encoding="utf-8")
    ts = dt.datetime.now().isoformat(timespec="seconds")
    for _ in range(10):
        _log_read(logs_dir, "Knowledge Base/Notes/hot-but-irrelevant.md", ts)
        _log_cite(logs_dir, "Knowledge Base/Notes/hot-but-irrelevant.md", ts)
    hits = find_module.find(vault, query="metabolism", prefer_used=True)
    assert not any(h.path.endswith("hot-but-irrelevant.md") for h in hits)


def test_cold_start_is_noop(vault: Path, logs_dir: Path) -> None:
    _seed_tie_pages(vault)
    plain = find_module.find(vault, query="usageprobe marker")
    used = find_module.find(vault, query="usageprobe marker", prefer_used=True)
    assert [h.as_dict() for h in plain] == [h.as_dict() for h in used]


def test_kill_switch(vault: Path, logs_dir: Path, monkeypatch) -> None:
    alpha, beta = _seed_tie_pages(vault)
    ts = dt.datetime.now().isoformat(timespec="seconds")
    _log_read(logs_dir, beta, ts)
    monkeypatch.setenv("EXOMEM_DISABLE_USAGE_BOOST", "1")
    hits = find_module.find(vault, query="usageprobe marker", prefer_used=True)
    ours = [h for h in hits if "usage-tie-" in h.path]
    assert [h.path for h in ours] == [alpha, beta]


def test_prefer_used_bypasses_hot_cache(vault: Path, logs_dir: Path, monkeypatch) -> None:
    _seed_tie_pages(vault)
    calls = {"n": 0}
    orig = find_module._find_semantic

    def counting(*args, **kwargs):
        calls["n"] += 1
        return orig(*args, **kwargs)

    monkeypatch.setattr(find_module, "_find_semantic", counting)
    find_module.find(vault, query="usageprobe marker", prefer_used=True)
    find_module.find(vault, query="usageprobe marker", prefer_used=True)
    assert calls["n"] == 2


def test_snapshot_refresh_sees_new_log_lines(vault: Path, logs_dir: Path, monkeypatch) -> None:
    alpha, beta = _seed_tie_pages(vault)
    monkeypatch.setenv("EXOMEM_USAGE_REFRESH_S", "0")  # re-stat logs every call
    hits = find_module.find(vault, query="usageprobe marker", prefer_used=True)
    ours = [h.path for h in hits if "usage-tie-" in h.path]
    assert ours == [alpha, beta]
    ts = dt.datetime.now().isoformat(timespec="seconds")
    for _ in range(5):
        _log_read(logs_dir, beta, ts)
        _log_cite(logs_dir, beta, ts)
    hits = find_module.find(vault, query="usageprobe marker", prefer_used=True)
    ours = [h.path for h in hits if "usage-tie-" in h.path]
    assert ours == [beta, alpha]


def test_ranking_config_roundtrip_of_usage_knobs() -> None:
    cfg = find_module.RankingConfig(usage_boost=1.2, usage_w_read=3.0)
    rt = find_module.ranking_config_from_jsonable(
        find_module.ranking_config_to_jsonable(cfg)
    )
    assert rt == cfg


def test_query_log_accepts_prefer_used() -> None:
    query_log.log_find_call(
        query="q", mode="hybrid", scope="kb", types=None, projects=None,
        tags=None, limit=5, rerank=False, prefer_compiled=True, graph=True,
        hits=[], prefer_used=True,
    )


def test_op_find_surface(vault: Path, logs_dir: Path) -> None:
    alpha, beta = _seed_tie_pages(vault)
    ts = dt.datetime.now().isoformat(timespec="seconds")
    for _ in range(5):
        _log_read(logs_dir, beta, ts)
        _log_cite(logs_dir, beta, ts)
    out = commands.op_find(vault, query="usageprobe marker", prefer_used=True)
    ours = [h for h in out if "usage-tie-" in h["path"]]
    assert ours[0]["path"] == beta
    assert "usage_boost" in ours[0]["signals"]
