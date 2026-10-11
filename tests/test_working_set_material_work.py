"""The material lane's work per matched unit does not grow with its neighbourhood.

The material lane leaves out, page by page, the categories other roles own.
That exclusion once re-read the whole page-to-categories map for every unit
the query matched, so the lane's cost grew with the square of the
neighbourhood: a live long turn spent 6.6 s there. Work is SQLite
virtual-machine steps on the lexical store's connections while the lane runs,
which no clock or load can move. Invented, generic vocabulary throughout.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import context_roles, lexstore, working_set
from exomem.working_set_resolve import analyze_turn

SMALL = 40
LARGE = 4 * SMALL
UNITS_PER_PAGE = 3
TURN = "what about the harbor amber gauges drifting"


def _write_pages(vault: Path, count: int) -> list[str]:
    """`count` survey pages, each with units only the material lane selects."""
    paths = []
    for index in range(count):
        rel = f"Knowledge Base/Notes/Survey/survey-{index:04d}.md"
        path = vault / rel
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            units = "".join(
                f"- [observation] Survey {index} harbor amber gauges drift by {unit}. ^o{unit}\n"
                for unit in range(UNITS_PER_PAGE)
            )
            path.write_text(
                "---\ntype: note\nstatus: active\nupdated: 2026-03-01\n---\n\n"
                f"# Survey {index}\n\n{units}",
                encoding="utf-8",
            )
        paths.append(rel)
    lexstore.ensure_fresh(vault)
    return paths


def _material_steps(vault: Path, paths: list[str], monkeypatch: pytest.MonkeyPatch) -> tuple[int, int]:
    """VM steps of one warm material lane over `paths`, and its item count."""
    steps = [0]
    counting = [False]
    connect = lexstore.LexicalStore._connect

    def counted_connect(self, *args, **kwargs):
        conn = connect(self, *args, **kwargs)

        def tick() -> int:
            if counting[0]:
                steps[0] += 1
            return 0

        conn.set_progress_handler(tick, 1)
        return conn

    registry = context_roles.load_roles(vault)

    def run():
        return working_set._material_lane(
            vault,
            registry.roles["material"],
            anchors=(),
            neighbourhood=frozenset(paths),
            registry=registry,
            analysis=analyze_turn(TURN),
        )

    run()  # warm the per-snapshot statistics the ranking reads
    with monkeypatch.context() as patch:
        patch.setattr(lexstore.LexicalStore, "_connect", counted_connect)
        counting[0] = True
        result = run()
        counting[0] = False
    return steps[0], len(result.items)


@pytest.mark.timeout(300)
def test_material_lane_work_per_matched_unit_is_flat_in_the_neighbourhood(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    small_steps, small_items = _material_steps(vault, _write_pages(vault, SMALL), monkeypatch)
    large_steps, large_items = _material_steps(vault, _write_pages(vault, LARGE), monkeypatch)

    assert small_items and large_items
    small_rows, large_rows = SMALL * UNITS_PER_PAGE, LARGE * UNITS_PER_PAGE
    assert large_steps / large_rows <= 1.5 * small_steps / small_rows, (
        f"the material lane took {small_steps} VM steps for {small_rows} matched units and "
        f"{large_steps} for {large_rows}: its work per matched unit grows with the neighbourhood"
    )
