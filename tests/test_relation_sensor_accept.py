"""Regressions for relation proposals and exact governed acceptance."""

from pathlib import Path

import dreamer_fixture as fx
import pytest
import sensing_fixture as sf
import test_relation_sensor_integration as sensor

from exomem import commands, epistemic_graph, freshness, markdown_relations, relation_queue


@pytest.fixture(autouse=True)
def clear_freshness():
    freshness.clear()
    yield
    freshness.clear()


def test_none_readings_leave_the_queue_unchanged(tmp_path: Path, monkeypatch) -> None:
    sf.enable(monkeypatch)
    sensor._active(monkeypatch)
    vault = sf.build(tmp_path)
    sf.settle(vault)
    before = epistemic_graph.EpistemicGraphIndex(vault).relation_review_batch()
    sensor._worker_and_ingest(vault, sensor.TypeStub(label_index=-1))
    assert {row[7] for row in sensor._rows(vault)} == {"none"}
    after = epistemic_graph.EpistemicGraphIndex(vault).relation_review_batch()
    assert after == before


def test_queue_with_readings_uses_no_page_file_reads(tmp_path: Path, monkeypatch) -> None:
    sf.enable(monkeypatch)
    sensor._active(monkeypatch)
    vault = sf.build(tmp_path)
    sf.settle(vault)
    sensor._worker_and_ingest(vault, sensor.TypeStub())

    def unexpected_page_read(*_args, **_kwargs):
        pytest.fail("a queue menu must use the graph snapshot")

    monkeypatch.setattr(relation_queue, "_page_for", unexpected_page_read)
    batch = epistemic_graph.EpistemicGraphIndex(vault).relation_review_batch()
    assert any(item.get("sensed") for group in batch["groups"] for item in group["items"])


@pytest.mark.parametrize("target", [
    "Notes/Insights/seal-wear-winter#^winter",
    "Notes/Insights/seal-wear-winter|Winter wear",
])
def test_retype_preserves_the_authored_link(tmp_path: Path, monkeypatch, target: str) -> None:
    sf.enable(monkeypatch)
    sensor._active(monkeypatch)
    vault = sf.build(tmp_path)
    path = f"{sf.KB}/Notes/Insights/retype-target.md"
    old_bullet = f"- links_to [[{target}]]"
    fx.write(vault, path, sf.note(
        "Retype target", "2026-05-04", "A plain link awaits a type.",
        extra=f"\n## Relations\n\n{old_bullet}\n",
    ))
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    sf.settle(vault)
    sensor._worker_and_ingest(vault, sensor.TypeStub())
    sensor._warm(vault, monkeypatch)
    sensor._log(vault)
    group, item = next(
        (group, item) for group, item in sensor._sensed_items(vault)
        if item["method"] == "retype" and item["from"] == path
    )
    relation = item["sensed"]["relation"]
    result = sensor._accept(vault, group, item, requested_relation=relation)
    assert result["accepted"] is True
    assert result["bullet"] == f"- {relation} [[{target}]]"
    document = markdown_relations.parse_markdown_relations(
        (vault / path).read_text(), relation_types={relation},
    )
    assert document.errors == []
    [authored] = document.relations
    assert authored.kind == relation
    assert authored.target == f"{sf.KB}/{target.split('|', 1)[0]}"
    if "|" in target:
        assert "|Winter wear]]" in authored.raw


def test_dismissed_retype_cannot_be_accepted_by_path(tmp_path: Path, monkeypatch) -> None:
    sf.enable(monkeypatch)
    sensor._active(monkeypatch)
    vault = sensor._build_retype_vault(tmp_path)
    sf.settle(vault)
    sensor._worker_and_ingest(vault, sensor.TypeStub())
    sensor._warm(vault, monkeypatch)
    sensor._log(vault)
    group, item = next(
        (group, item) for group, item in sensor._sensed_items(vault)
        if item["method"] == "retype" and item["from"] == sensor.RTYPE
    )
    commands.op_triage_memory(vault, ref=item["ref"], action="dismiss")
    before = (vault / sensor.RTYPE).read_text()
    with pytest.raises(ValueError, match="REVIEW_ITEM_CHANGED.*decided"):
        sensor._accept(
            vault, group, item, requested_relation=item["sensed"]["relation"],
            path=item["from"],
        )
    assert (vault / sensor.RTYPE).read_text() == before
