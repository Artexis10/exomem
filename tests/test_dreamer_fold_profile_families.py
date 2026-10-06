"""The episode-recap fold (`upkeep_fold`) and profile (`upkeep_profile`) families.

Fold: two or more episodes recorded recaps that link a governed page after it
was last updated, and the page neither links nor cites them. The route is a
curation work item over the page and those recaps. Profile: pages of two or
more independent origins link a governed page that carries no summary of
itself. The route is `edit_memory` setting its `summary`. Both are counts over
the published graph; neither writes anything but the sidecar.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import dreamer_fixture as fx
import pytest

from exomem import (
    commands,
    dreamer,
    dreamer_families,
    dreamer_store,
    freshness,
    review_state,
    upkeep,
)
from exomem import schema as schema_module
from exomem.governance.principal import owner_principal, request_scope
from exomem.writer_lease import invoke_command

EPISODE_A = "ep-" + "a1" * 16
EPISODE_B = "ep-" + "b2" * 16
EPISODE_C = "ep-" + "c3" * 16


@pytest.fixture(autouse=True)
def _clean():
    freshness.clear()
    dreamer.reset_for_tests()
    dreamer_store.clear_reader_memo()
    yield
    dreamer.reset_for_tests()
    freshness.clear()
    dreamer_store.clear_reader_memo()


def _quiet(vault: Path, now: float | None = None) -> None:
    results = fx.run_to_quiet(vault, now=now)
    assert all(result.stop_reason != "error" for result in results), results


def _row(vault: Path, kind: str, subject: str = fx.ENTITY) -> dict | None:
    conn = dreamer_store.DreamerStore(vault).connect()
    try:
        return dreamer_store.DreamerStore.candidate(
            conn, dreamer_store.candidate_id(kind, subject, "")
        )
    finally:
        conn.close()


def _open(vault: Path, kind: str, subject: str = fx.ENTITY) -> dict | None:
    row = _row(vault, kind, subject)
    return row if row is not None and row["state"] == "open" else None


def _source_schema(vault: Path):
    """What the Source writer needs and the generated vault lacks: the shipped
    Source contract, and the indexes and log a capture updates."""
    references = vault / fx.KB / "_Schema" / "references"
    if not references.exists():
        shipped = Path(commands.__file__).parent / "_scaffold" / "_Schema" / "references"
        references.mkdir(parents=True)
        for name in ("frontmatter.md", "page-types.md"):
            shutil.copyfile(shipped / name, references / name)
        fixture = Path(__file__).parent / "fixtures" / fx.KB
        for rel in ("Sources/index.md", "index.md", "log.md"):
            shutil.copyfile(fixture / rel, vault / fx.KB / rel)
    return schema_module.load_source_schema(vault)


def _record(vault: Path, episode: str, decided: str) -> str:
    """One recap through the real recorder, as the owner; returns the page it wrote."""
    with request_scope(owner_principal(surface="mcp")):
        result = commands.op_episode_memory(
            vault,
            _source_schema(vault),
            action="record",
            episode=episode,
            subject=f"Pump session {episode[3:7]}",
            summary="Worked through the pump findings.",
            decided=[decided],
        )
    path = result["source"]["path"]
    _observed(vault, path)
    return path


def _observed(vault: Path, *paths: str) -> None:
    """What the watcher and the graph drain do after a write lands."""
    freshness.on_files_changed(vault, changed=[vault / path for path in paths])
    fx.publish_graph(vault)


def _tool(vault: Path, name: str, **kwargs):
    command = next(command for command in commands.PRODUCT_COMMANDS if command.name == name)
    return invoke_command(command, vault, **kwargs)


# ----------------------------------------------------------------------
# fold
# ----------------------------------------------------------------------


def test_two_episodes_recorded_about_a_page_since_it_changed_make_one_fold_item(
    tmp_path: Path,
) -> None:
    vault = fx.build(tmp_path)
    first = _record(vault, EPISODE_A, "Run the [[Orbit Pump]] below 40 litres a minute")
    _quiet(vault)
    # One episode is one origin: nothing yet.
    assert _open(vault, dreamer_families.FOLD_KIND) is None
    second = _record(vault, EPISODE_B, "Replace the [[Orbit Pump]] seals yearly")
    _quiet(vault)
    row = _open(vault, dreamer_families.FOLD_KIND)
    assert row is not None and row["family"] == dreamer_families.FOLD_FAMILY
    assert {item["path"] for item in row["evidence"] if item["role"] == "recap"} == {
        first,
        second,
    }
    assert row["route"] == {
        "tool": "maintain_memory",
        "args": {
            "mode": "curation",
            "curation_action": "work-item",
            "paths": [fx.ENTITY, *sorted([first, second])],
        },
    }
    # Deduplicated: a rerun over unchanged evidence keeps one row and its fingerprint.
    fingerprint = row["fingerprint"]
    fx.edit(vault, first, (vault / first).read_text("utf-8"))
    _quiet(vault)
    assert _open(vault, dreamer_families.FOLD_KIND)["fingerprint"] == fingerprint
    folds = [
        r
        for r in dreamer_store.read_view(vault).candidates
        if r["family"] == dreamer_families.FOLD_FAMILY and r["state"] == "open"
    ]
    assert len(folds) == 1

    # A new revision of episode A that no longer links the page supersedes the
    # one that did: the superseded revision is not live, so one origin remains.
    _record(vault, EPISODE_A, "Nothing further on the pump")
    _observed(vault, first)
    _quiet(vault)
    assert _open(vault, dreamer_families.FOLD_KIND) is None


def test_live_revisions_of_one_episode_count_once(tmp_path: Path) -> None:
    """An interrupted record can leave two live revisions of one episode."""
    vault = fx.build(tmp_path)
    for day in ("01", "02"):
        fx.edit(vault, *fx.entity_recap(EPISODE_A, day))
    _quiet(vault)
    assert _open(vault, dreamer_families.FOLD_KIND) is None


def test_the_page_updated_after_the_recaps_resolves_the_fold_item(tmp_path: Path) -> None:
    vault = fx.build(tmp_path)
    for episode, day in ((EPISODE_A, "02"), (EPISODE_B, "03")):
        fx.edit(vault, *fx.entity_recap(episode, day))
    _quiet(vault)
    assert _open(vault, dreamer_families.FOLD_KIND) is not None
    fx.edit(vault, fx.ENTITY, fx.entity(updated="2026-06-01"))
    _quiet(vault)
    assert _open(vault, dreamer_families.FOLD_KIND) is None
    assert _row(vault, dreamer_families.FOLD_KIND)["state"] == "resolved"


def test_a_dismissed_fold_item_returns_only_when_another_episode_links_the_page(
    tmp_path: Path,
) -> None:
    vault = fx.build(tmp_path)
    for episode, day in ((EPISODE_A, "02"), (EPISODE_B, "03")):
        fx.edit(vault, *fx.entity_recap(episode, day))
    _quiet(vault)
    ref = upkeep.upkeep_ref(_open(vault, dreamer_families.FOLD_KIND)["id"])
    served = _tool(vault, "review_memory", mode="item", ref=ref)["item"]
    _tool(
        vault,
        "triage_memory",
        ref=ref,
        action="dismiss",
        why="handled: the decisions are already on the page",
        expected_fingerprint=served["fingerprint"],
    )
    _quiet(vault)
    store = review_state.ReviewStateStore(vault)
    held = _open(vault, dreamer_families.FOLD_KIND)
    assert store.effective_state(held["id"], held["fingerprint"])[0] == "dismissed"
    fx.edit(vault, *fx.entity_recap(EPISODE_C, "04"))
    _quiet(vault)
    moved = _open(vault, dreamer_families.FOLD_KIND)
    assert moved["id"] == held["id"] and moved["fingerprint"] != held["fingerprint"]
    assert store.effective_state(moved["id"], moved["fingerprint"])[0] == "open"


# ----------------------------------------------------------------------
# profile
# ----------------------------------------------------------------------


def test_pages_of_two_origins_linking_a_page_without_a_summary_make_one_profile_item(
    tmp_path: Path,
) -> None:
    vault = fx.build(tmp_path)
    _quiet(vault)
    row = _open(vault, dreamer_families.PROFILE_KIND)
    assert row is not None and row["family"] == dreamer_families.PROFILE_FAMILY
    assert {item["path"] for item in row["evidence"] if item["role"] == "referrer"} == {
        fx.CAVITATION,
        fx.SEAL_WEAR,
    }
    assert row["route"] == {
        "tool": "edit_memory",
        "args": {
            "path": fx.ENTITY,
            "operation": {"kind": "patch_frontmatter", "field": "summary"},
        },
    }


def test_one_source_fanned_out_is_one_origin_for_profile(tmp_path: Path) -> None:
    vault = fx.build(tmp_path)
    fx.edit(
        vault,
        fx.SEAL_WEAR,
        fx.insight(
            "Pump seal wear",
            sources=["field-report-one"],
            updated="2026-05-02",
            links="Seal wear on the [[Notes/Entities/orbit-pump]] doubles after a dry start.",
        ),
    )
    _quiet(vault)
    assert _open(vault, dreamer_families.PROFILE_KIND) is None


@pytest.mark.parametrize(
    "described",
    [
        'summary: "A circulation pump on the test rig."\n',
        "## Summary\n\nA circulation pump on the test rig.\n",
    ],
    ids=["summary_field", "summary_section"],
)
def test_a_page_that_describes_itself_gets_no_profile_item(tmp_path: Path, described: str) -> None:
    vault = fx.build(tmp_path)
    page = fx.entity()
    if described.startswith("summary:"):
        page = page.replace("status: active\n", f"status: active\n{described}")
    else:
        page = page + "\n" + described
    fx.edit(vault, fx.ENTITY, page)
    _quiet(vault)
    assert _open(vault, dreamer_families.PROFILE_KIND) is None


def test_the_agent_applying_the_profile_route_resolves_it(tmp_path: Path) -> None:
    vault = fx.build(tmp_path)
    _quiet(vault)
    ref = upkeep.upkeep_ref(_open(vault, dreamer_families.PROFILE_KIND)["id"])
    context = _tool(vault, "review_item_context", ref=ref)
    route = context["route"]
    operation = {
        **route["args"]["operation"],
        "value": "The test rig's circulation pump.",
        "expected_hash": context["subject"]["content_hash"],
    }
    _tool(
        vault,
        route["tool"],
        path=route["args"]["path"],
        operation=operation,
        why="the upkeep item asked for the page's summary",
    )
    assert "summary: " in (vault / fx.ENTITY).read_text("utf-8")
    _observed(vault, fx.ENTITY)
    _quiet(vault)
    assert _open(vault, dreamer_families.PROFILE_KIND) is None
    assert _row(vault, dreamer_families.PROFILE_KIND)["state"] == "resolved"
