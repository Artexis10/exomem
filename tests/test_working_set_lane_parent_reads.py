"""A write that lands between two role lanes of one request is served, not dropped.

The role lanes of one request share one read of each parent page. A write can
land after an earlier lane read a page, so that a later lane's catalogue rows
carry a newer generation than the shared read. The later lane must read the
page again and serve its units, as it did when every lane read each parent
itself, rather than drop them as stale or fail. Invented, generic vocabulary
throughout.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import context_roles, freshness, lexstore, working_set
from exomem import find as find_module
from exomem.vault import walk_vault_md
from exomem.working_set_resolve import analyze_turn

PAGES = 6
ROLES = ("preferences", "constraints", "recent_change", "material")
TURN = "what about the harbor ledger amber gauges"


def _page(index: int, edit: str = "") -> str:
    return (
        "---\ntype: note\nstatus: active\nupdated: 2026-03-01\n---\n\n"
        f"# Harbor ledger {index}\n\n"
        f"- [preference] Ledger {index} prefers amber gauges. ^pref\n"
        f"- [constraint] Ledger {index} stays under the tide mark. ^limit\n"
        f"- [decision] Ledger {index} moved the crane to the east quay. ^moved\n"
        f"- [observation] Ledger {index} harbor amber gauges drift in winter. ^seen\n"
        + edit
    )


def _lanes(vault: Path, paths: list[str]) -> tuple[dict[str, list[str]], list[dict]]:
    """Every role's item refs from one run of the lanes, and what they missed."""
    items, missing = working_set.run_lanes(
        vault,
        anchors=(),
        roles=[{"id": role} for role in ROLES],
        registry=context_roles.load_roles(vault),
        neighbourhood=frozenset(paths),
        analysis=analyze_turn(TURN),
        # One snapshot for the whole request, taken before any lane runs.
        freshness_snapshot=find_module.FreshnessSnapshot(vault),
    )
    return {role: sorted(item.ref for item in items if item.role == role) for role in ROLES}, list(
        missing
    )


@pytest.mark.timeout(300)
def test_a_write_between_lanes_is_served_by_the_later_lanes(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = [f"Knowledge Base/Notes/Harbor/ledger-{index}.md" for index in range(PAGES)]
    for index, rel in enumerate(paths):
        (vault / rel).parent.mkdir(parents=True, exist_ok=True)
        (vault / rel).write_text(_page(index), encoding="utf-8")
    # The registry a running service keeps: the request's snapshot reads it.
    freshness.seed(vault, "vault", ((str(p), freshness.stat_signature(p)) for p in walk_vault_md(vault)))
    freshness.seed(
        vault,
        "kb",
        ((str(p), freshness.stat_signature(p)) for p in find_module._walk_md(vault / "Knowledge Base")),
    )
    lexstore.ensure_fresh(vault)
    calm, calm_missing = _lanes(vault, paths)
    assert all(calm[role] for role in ROLES), calm

    real_lane = working_set._lane

    def lane_then_write(root, role, **kwargs):
        result = real_lane(root, role, **kwargs)
        if role.id == ROLES[0]:
            # Every page changes, file and catalogue, after the first lane read it.
            for index, rel in enumerate(paths):
                (vault / rel).write_text(_page(index, "\nEdited between lanes.\n"), encoding="utf-8")
            assert lexstore.get_store(vault).upsert_paths([vault / rel for rel in paths])
        return result

    with monkeypatch.context() as patch:
        patch.setattr(working_set, "_lane", lane_then_write)
        raced, raced_missing = _lanes(vault, paths)

    assert raced == calm
    assert raced_missing == calm_missing
