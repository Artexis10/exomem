"""Deterministic browser-model fixtures without making Node a runtime dependency."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
MODEL = ROOT / "src/exomem/studio/model.v1.js"
STATE = ROOT / "src/exomem/studio/state.v2.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node is not installed")


def _node(source: str) -> dict:
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", source],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_worklist_model_preserves_server_order_filters_and_honest_counts() -> None:
    source = f"""
      import {{visibleItems, categoriesFor, reportStatus}} from {MODEL.as_uri()!r};
      const report = {{
        items: [
          {{ref: 'exomem://review/c', categories: ['contradiction']}},
          {{ref: 'exomem://review/s', categories: ['stale_review', 'relation_debt']}},
          {{ref: 'exomem://review/u', categories: ['unprocessed_source']}},
        ],
        total: 8,
        truncated: 3,
        upstream_truncated: 2,
      }};
      console.log(JSON.stringify({{
        all: visibleItems(report).map((item) => item.ref),
        filtered: visibleItems(report, 'relation_debt').map((item) => item.ref),
        categories: categoriesFor(report),
        status: reportStatus(report, visibleItems(report).length),
      }}));
    """

    result = _node(source)

    assert result["all"] == [
        "exomem://review/c",
        "exomem://review/s",
        "exomem://review/u",
    ]
    assert result["filtered"] == ["exomem://review/s"]
    assert result["categories"] == [
        "contradiction",
        "relation_debt",
        "stale_review",
        "unprocessed_source",
    ]
    assert result["status"] == (
        "3 shown · 8 in this server view · 3 omitted by the requested limit · "
        "2 capped upstream"
    )


def test_worklist_filters_hidden_only_for_relation_queue_mode() -> None:
    # Bug: switching to the relation-queue tab left the Inbox/Activation
    # state+category filters live; changing one re-rendered the STALE
    # attention/activation report into the sidebar while the relation panel
    # stayed visible. The fix routes both "should this UI be interactive" and
    # "should the change handlers act" through this one predicate.
    source = f"""
      import {{worklistFiltersVisible}} from {MODEL.as_uri()!r};
      console.log(JSON.stringify({{
        attention: worklistFiltersVisible('attention'),
        activation: worklistFiltersVisible('activation'),
        relationQueue: worklistFiltersVisible('relation-queue'),
      }}));
    """

    result = _node(source)

    assert result == {"attention": True, "activation": True, "relationQueue": False}


def test_section_states_distinguish_empty_unavailable_and_truncated() -> None:
    source = f"""
      import {{sectionState}} from {MODEL.as_uri()!r};
      console.log(JSON.stringify({{
        empty: sectionState({{available: false, items: []}}),
        unavailable: sectionState({{available: false, reason: 'graph offline', nodes: []}}),
        truncated: sectionState({{available: true, items: [1], omitted: 2}}),
        available: sectionState({{available: true, items: [1]}}),
      }}));
    """

    assert _node(source) == {
        "empty": "empty",
        "unavailable": "unavailable",
        "truncated": "truncated",
        "available": "available",
    }


def test_router_restores_mode_filter_panel_and_stable_review_reference() -> None:
    source = f"""
      global.window = {{
        location: {{pathname: '/studio/', search: '?mode=activation&state=all&category=relation_debt&ref=exomem%3A%2F%2Freview%2Fstable&panel=evolution'}},
        history: {{pushState: (_state, _title, target) => global.target = target}},
      }};
      const {{readRoute, writeRoute}} = await import({STATE.as_uri()!r});
      const route = readRoute();
      writeRoute(route);
      console.log(JSON.stringify({{route, target: global.target}}));
    """

    result = _node(source)

    # state.v2 gains view/run/astep; a legacy review URL must still round-trip
    # to the same query string (view=review default emits nothing).
    assert result["route"] == {
        "mode": "activation",
        "state": "all",
        "category": "relation_debt",
        "ref": "exomem://review/stable",
        "panel": "evolution",
        "view": "review",
        "run": "",
        "astep": "start",
    }
    assert result["target"].startswith("/studio/?mode=activation&state=all")
    assert "ref=exomem%3A%2F%2Freview%2Fstable" in result["target"]
    assert "view=" not in result["target"]
