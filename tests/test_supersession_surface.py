"""Surface what the substrate already records.

Two pieces, one principle — the server *records* both the supersession
relationship and each edit's `why`, and these tests pin that it now *surfaces*
them at read time:

- Piece B: `find` soft-demotes `status: superseded` pages (so a replaced
  conclusion can't outrank its successor) and flags `status`/`superseded_by`
  on the hit. Demotion is gated by `prefer_active`; exposure is not.
- Piece A: `vault.read_log_entries` / `get(include_history=True)` return a
  page's edit-`why` history from the append-only `log.md`.

Runs under the suite-wide EXOMEM_DISABLE_EMBEDDINGS (see conftest): hybrid
degrades to BM25 + keyword + graph, which still exercises the fusion + the new
demotion pass. The conflict-pair notes match a rare token, so the keyword
contribution carries them even if the BM25 index is stale.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from pathlib import Path

import pytest

from exomem import edit as edit_module
from exomem import find as find_module
from exomem import get_page as get_module
from exomem import server as server_module
from exomem import vault as vault_module

TODAY = dt.date(2026, 6, 23)
TOKEN = "zqxconflicttoken"  # rare → only the notes we plant here match it
EXISTING = (
    "Knowledge Base/Notes/Insights/"
    "progressive-disclosure-without-mode-fragmentation.md"
)


def _write_insight(vault: Path, name: str, body: str, *, extra_fm: str = "") -> str:
    rel = f"Knowledge Base/Notes/Insights/{name}"
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "---\n"
        "type: insight\n"
        "created: 2026-06-01\n"
        "updated: 2026-06-01\n"
        "tags: []\n"
        f"{extra_fm}"
        "---\n" + body,
        encoding="utf-8",
    )
    find_module.clear_cache()
    return rel


def _write_conflict_pair(vault: Path) -> tuple[str, str]:
    """An active conclusion and its superseded predecessor with IDENTICAL bodies.

    Identical content means equal base ranking scores, so the only thing that
    can separate them is the supersession demotion.
    """
    body = f"# Heading\n\nThe {TOKEN} is the distinctive search term here.\n"
    new_rel = _write_insight(vault, "zqx-current.md", body)
    old_rel = _write_insight(
        vault,
        "zqx-old.md",
        body,
        extra_fm=(
            "status: superseded\n"
            'superseded_by:\n  - "[[Knowledge Base/Notes/Insights/zqx-current]]"\n'
        ),
    )
    return new_rel, old_rel


# ---------------- Piece B: supersession-aware find ----------------


def test_status_multiplier_only_demotes_superseded() -> None:
    cfg = find_module.RankingConfig()
    assert find_module._status_multiplier("superseded", cfg) == cfg.superseded_penalty
    assert find_module._status_multiplier("active", cfg) == 1.0
    assert find_module._status_multiplier("draft", cfg) == 1.0
    assert find_module._status_multiplier(None, cfg) == 1.0


def test_apply_status_demotion_reorders(vault: Path) -> None:
    """Mirror of test_type_boost_honors_config: equal base scores, the demotion
    decides order, and the config value (not a constant) drives it."""
    new_rel, old_rel = _write_conflict_pair(vault)
    fused = [(old_rel, 1.0), (new_rel, 1.0)]  # superseded listed first
    order = [p for p, _ in find_module._apply_status_demotion(fused, vault)]
    assert order[0] == new_rel  # active overtakes the demoted predecessor

    zero = find_module.RankingConfig(superseded_penalty=0.0)
    order0 = [p for p, _ in find_module._apply_status_demotion(fused, vault, zero)]
    assert order0[-1] == old_rel


def test_find_demotes_and_flags_superseded(vault: Path) -> None:
    new_rel, old_rel = _write_conflict_pair(vault)
    hits = find_module.find(vault, query=TOKEN, prefer_active=True)
    paths = [h.path for h in hits]
    assert new_rel in paths and old_rel in paths
    # Equal base scores → demotion makes the successor rank strictly first.
    assert paths.index(new_rel) < paths.index(old_rel)

    old_hit = next(h for h in hits if h.path == old_rel)
    od = old_hit.as_dict()
    assert od["status"] == "superseded"
    assert od["superseded_by"] == ["[[Knowledge Base/Notes/Insights/zqx-current]]"]

    # The live conclusion carries no status/superseded_by noise.
    new_hit = next(h for h in hits if h.path == new_rel)
    nd = new_hit.as_dict()
    assert "status" not in nd
    assert "superseded_by" not in nd


def test_find_exposure_independent_of_prefer_active(vault: Path) -> None:
    """status/superseded_by surface even when the demotion is turned off."""
    _, old_rel = _write_conflict_pair(vault)
    hits = find_module.find(vault, query=TOKEN, prefer_active=False)
    old_hit = next(h for h in hits if h.path == old_rel)
    od = old_hit.as_dict()
    assert od["status"] == "superseded"
    assert od["superseded_by"]


def test_archived_dir_excluded_from_find(vault: Path) -> None:
    rel = "Knowledge Base/_archive/zqx-archived.md"
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "---\ntype: insight\nstatus: archived\ncreated: 2026-06-01\n"
        "updated: 2026-06-01\ntags: []\n---\n"
        f"# Archived\n\nThe {TOKEN} is here too but archived.\n",
        encoding="utf-8",
    )
    find_module.clear_cache()
    hits = find_module.find(vault, query=TOKEN, mode="keyword")
    assert all("/_archive/" not in h.path for h in hits)


# ---------------- Piece A: edit-`why` history ----------------


def _edit_with_why(vault: Path, why: str, suffix: str) -> None:
    body = get_module.get_page(vault, path=EXISTING).body
    edit_module.edit(
        vault, path=EXISTING, why=why, new_body=body + f"\n{suffix}\n", today=TODAY
    )


def test_read_log_entries_returns_why_newest_first(vault: Path) -> None:
    _edit_with_why(vault, "WHY_ALPHA_marker", "alpha")
    _edit_with_why(vault, "WHY_BETA_marker", "beta")

    entries = vault_module.read_log_entries(vault, EXISTING)
    assert len(entries) >= 2
    # Entries are prepended → newest first. BETA was the last edit.
    assert "WHY_BETA_marker" in entries[0]["summary"]
    assert any("WHY_ALPHA_marker" in e["summary"] for e in entries)
    assert entries[0]["op"] == "edit"
    assert entries[0]["date"] == TODAY.isoformat()


def test_read_log_entries_empty_for_unlogged(vault: Path) -> None:
    assert (
        vault_module.read_log_entries(
            vault, "Knowledge Base/Notes/Insights/never-logged.md"
        )
        == []
    )


def test_read_log_entries_missing_log_returns_empty(tmp_path: Path) -> None:
    # A vault root with no log.md → [] (best-effort, never an error).
    assert vault_module.read_log_entries(tmp_path, EXISTING) == []


# ---------------- Piece A: get(include_history) server tool ----------------


def _build_server(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(server_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    return server_module.build_server(require_auth=False)


def _call(mcp, name: str, args: dict) -> dict:
    result = asyncio.run(mcp.call_tool(name, args, run_middleware=False))
    sc = getattr(result, "structured_content", None)
    if isinstance(sc, dict):
        return sc
    for c in getattr(result, "content", []) or []:
        text = getattr(c, "text", None)
        if text:
            return json.loads(text)
    return {}


def test_get_include_history_surfaces_why(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _edit_with_why(vault, "WHY_GET_marker", "x")
    mcp = _build_server(monkeypatch)

    out = _call(mcp, "read_memory", {"path": EXISTING, "include_history": True})
    assert "history" in out
    assert any("WHY_GET_marker" in e["summary"] for e in out["history"])

    # Without the flag, no history key is attached.
    plain = _call(mcp, "read_memory", {"path": EXISTING})
    assert "history" not in plain
