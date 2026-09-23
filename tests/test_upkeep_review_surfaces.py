"""D1-T10: the upkeep namespace and its explicit review surfaces.

`review_memory(mode="upkeep")` is the bounded, tool-parity list. Item and
context revalidate one proposal from its own pages and never run the attention
union. Every count is taken after egress and triage: a withheld page never
appears, not even as a number.
"""

from __future__ import annotations

import json
from pathlib import Path

import dreamer_fixture as fx
import pytest
from test_governance_egress import SCOPE_ID, _external, write_rule

from exomem import (
    attention,
    commands,
    dreamer,
    dreamer_families,
    dreamer_store,
    freshness,
    upkeep,
)
from exomem.governance import egress, membership, policy
from exomem.governance.principal import request_scope


@pytest.fixture(autouse=True)
def _clean():
    freshness.clear()
    dreamer.reset_for_tests()
    dreamer_store.clear_reader_memo()
    policy._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()
    yield
    dreamer.reset_for_tests()
    freshness.clear()
    dreamer_store.clear_reader_memo()
    policy._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()


def _ready(tmp_path: Path) -> Path:
    vault = fx.build(tmp_path)
    results = fx.run_to_quiet(vault)
    assert all(result.stop_reason != "error" for result in results), results
    return vault


def _hydration_ref() -> str:
    return upkeep.upkeep_ref(
        dreamer_store.candidate_id(dreamer_families.HYDRATION_KIND, fx.ENTITY, "")
    )


def _withhold(vault: Path, rel: str) -> None:
    """Withhold one page from the external audience."""
    target = vault / "Knowledge Base" / "_Governance" / "scopes" / "patterns.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    glob = rel.removeprefix("Knowledge Base/")
    target.write_text(
        f'governance_version: 1\nid: {SCOPE_ID}\nname: Withheld\npaths: ["{glob}"]\n',
        encoding="utf-8",
    )
    write_rule(vault, ceiling=0)
    policy._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()


def test_review_mode_upkeep_is_bounded_and_counts_after_egress(tmp_path: Path) -> None:
    vault = _ready(tmp_path)
    listed = commands.op_review_memory(vault, mode="upkeep")
    assert listed["status"] == "available"
    kinds = {item["kind"] for item in listed["items"]}
    assert kinds == {dreamer_families.LINK_KIND, dreamer_families.HYDRATION_KIND}
    assert listed["families"]["upkeep_hydration"] == 1
    assert listed["families"]["upkeep_link"] >= 1
    # Hydration ranks before link.
    assert listed["items"][0]["kind"] == dreamer_families.HYDRATION_KIND
    for item in listed["items"]:
        assert item["permission"] == "consideration does not authorize mutation"
        assert len(item["evidence"]) <= upkeep.SHOWN_EVIDENCE
    hydration = listed["items"][0]
    assert hydration["ref"] == _hydration_ref()
    assert hydration["subject"]["title"] == "Orbit Pump"
    assert hydration["evidence_count"] == 2

    bounded = commands.op_review_memory(vault, mode="upkeep", limit=1)
    assert len(bounded["items"]) == 1
    assert bounded["truncated"] is True
    assert bounded["families"] == listed["families"]

    # Withhold one contributor: the hydration item loses an origin, so it is
    # withheld whole, and it is not counted either.
    _withhold(vault, fx.SEAL_WEAR)
    with request_scope(_external()):
        guarded = commands.op_review_memory(vault, mode="upkeep")
    encoded = json.dumps(guarded)
    assert "pump-seal-wear" not in encoded
    assert "Pump seal wear" not in encoded
    assert "upkeep_hydration" not in guarded["families"]
    assert all(item["kind"] != dreamer_families.HYDRATION_KIND for item in guarded["items"])


def test_item_mode_revalidates_without_an_attention_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _ready(tmp_path)
    for name in ("attention", "item_by_ref", "activation"):
        monkeypatch.setattr(
            attention, name, lambda *_a, _n=name, **_k: pytest.fail(f"attention.{_n} ran")
        )
    result = commands.op_review_memory(vault, mode="item", ref=_hydration_ref())
    item = result["item"]
    assert item["kind"] == dreamer_families.HYDRATION_KIND
    assert item["ref"] == _hydration_ref()
    view = dreamer_store.read_view(vault)
    stored = next(row for row in view.candidates if row["kind"] == dreamer_families.HYDRATION_KIND)
    assert item["fingerprint"] == stored["fingerprint"]

    # A proposal that no longer holds is not found; nothing is written.
    fx.edit(vault, fx.ENTITY, fx.entity(updated="2026-09-01"))
    generation = dreamer_store.read_view(vault).generation
    with pytest.raises(ValueError, match="REVIEW_ITEM_NOT_FOUND"):
        commands.op_review_memory(vault, mode="item", ref=_hydration_ref())
    assert dreamer_store.read_view(vault).generation == generation


def test_item_context_is_bounded_and_refuses_a_stale_fingerprint(tmp_path: Path) -> None:
    vault = _ready(tmp_path)
    context = commands.op_review_item_context(vault, ref=_hydration_ref(), max_body_chars=40)
    assert context["mode"] == "upkeep"
    assert context["subject"]["path"] == fx.ENTITY
    assert len(context["subject"]["excerpt"]) <= 40
    assert context["subject"]["truncated"] is True
    assert context["subject"]["content_hash"]
    assert {entry["path"] for entry in context["evidence"]} == {fx.CAVITATION, fx.SEAL_WEAR}
    assert all(len(entry["excerpt"]) <= upkeep.CONTEXT_EVIDENCE_CHARS for entry in context["evidence"])
    assert context["route"]["tool"] == "maintain_memory"
    with pytest.raises(ValueError, match="REVIEW_ITEM_CHANGED"):
        commands.op_review_item_context(
            vault, ref=_hydration_ref(), expected_fingerprint="0" * 24
        )
    with pytest.raises(ValueError, match="INVALID_UPKEEP_CONTEXT_ARGUMENTS"):
        commands.op_review_item_context(vault, ref=_hydration_ref(), max_graph_nodes=5)


def test_off_family_is_absent_and_quiet_family_is_listed(tmp_path: Path) -> None:
    vault = _ready(tmp_path)
    family_ref = "exomem://review/family/upkeep_hydration"
    commands.op_triage_memory(vault, ref=family_ref, action="quiet", why="too_frequent: noisy")
    quiet = commands.op_review_memory(vault, mode="upkeep")
    hydration = [i for i in quiet["items"] if i["kind"] == dreamer_families.HYDRATION_KIND]
    assert len(hydration) == 1
    assert hydration[0]["disposition"]["family"] == "quiet"
    assert quiet["families"]["upkeep_hydration"] == 1

    commands.op_triage_memory(vault, ref=family_ref, action="off", why="too_frequent: noisy")
    off = commands.op_review_memory(vault, mode="upkeep")
    assert all(i["kind"] != dreamer_families.HYDRATION_KIND for i in off["items"])
    assert "upkeep_hydration" not in off["families"]

    commands.op_triage_memory(vault, ref=family_ref, action="normal")
    back = commands.op_review_memory(vault, mode="upkeep")
    assert back["families"]["upkeep_hydration"] == 1


def test_an_absent_sidecar_reports_unavailable(tmp_path: Path) -> None:
    vault = fx.build(tmp_path)
    listed = commands.op_review_memory(vault, mode="upkeep")
    assert listed["status"] == "unavailable"
    assert listed["items"] == []
    assert not dreamer_store.sidecar_path(vault).exists()

