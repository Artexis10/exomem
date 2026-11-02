"""`upkeep_sensed_compile`: a recap claim its home page does not state, served as an upkeep item.

The family reads the sensing projection for each caller and stores nothing in the
dreamer sidecar. These tests cover what a user sees and does: the item, its rank
against structural items, a dismissal and a family setting, and the per-caller
twins where a withheld page must read exactly like an absent one.
"""

from __future__ import annotations

import json
from pathlib import Path

import dreamer_fixture as fx
import pytest
import sensing_fixture as sf
from test_governance_egress import _external
from test_sensed_coverage import (
    CLAIM,
    ELSEWHERE,
    LATER,
    LINE,
    OTHER_LINE,
    recap_page,
    seal_page,
    sense_coverage,
    start,
)
from test_upkeep_vocabulary_egress_twin import _cmd, _govern, _outcome, _reset, _reset_caches

from exomem import dreamer_families, freshness, review_state, sensed_model, sensor_worker, upkeep
from exomem.governance.principal import owner_principal, request_scope
from exomem.writer_lease import invoke_command

FAMILY = dreamer_families.SENSED_COMPILE_FAMILY
FAMILY_REF = f"exomem://review/family/{FAMILY}"


@pytest.fixture(autouse=True)
def _clean():
    _reset()
    sensor_worker.reset_for_tests()
    sensed_model._VECTORS.clear()
    yield
    _reset()
    sensor_worker.reset_for_tests()
    freshness.clear()
    sensed_model._VECTORS.clear()


def sensed(vault: Path, **kwargs) -> list[dict]:
    return [i for i in upkeep.review(vault, limit=50, **kwargs)["items"] if i["family"] == FAMILY]


def two_recaps(tmp_path: Path, monkeypatch, stub=None) -> tuple[Path, sf.CoverageStubInstrument]:
    """Two recaps of different episodes link the home: a structural fold item and two compile items."""
    stub = stub or sf.CoverageStubInstrument()
    vault = start(
        tmp_path, monkeypatch, recap_page([LINE]),
        recap_page([OTHER_LINE], key=ELSEWHERE, when=LATER),
    )
    sense_coverage(vault, stub)
    sf.settle(vault)
    return vault, stub


def stripped(listing: dict) -> str:
    """A review answer without the sensed family's items, counts and completeness flag."""
    return json.dumps(
        {
            **listing,
            "setting": None,
            "items": [i for i in listing["items"] if i["family"] != FAMILY],
            "families": {k: v for k, v in listing["families"].items() if k != FAMILY},
            "evidence_complete": {k: v for k, v in listing["evidence_complete"].items() if k != FAMILY},
        },
        sort_keys=True,
    )


def test_a_due_claim_is_served_after_every_structural_item_and_moves_none(
    tmp_path: Path, monkeypatch
) -> None:
    """R2: a reading adds an item and never suppresses, reorders or alters a structural one."""
    vault, _stub = two_recaps(tmp_path, monkeypatch)

    on = upkeep.review(vault, limit=50)
    families = [item["family"] for item in on["items"]]
    assert families[0] == dreamer_families.FOLD_FAMILY and families.count(FAMILY) == 2
    assert families[1:] == [FAMILY, FAMILY], "the sensed items come after every structural one"
    [first, _second] = sensed(vault)
    assert first["compile"]["claims"] == [
        {"claim_index": 0, "verdict": "absent", "reading_id": first["compile"]["claims"][0]["reading_id"]}
    ]
    assert first["compile"]["claims"][0]["reading_id"]
    assert first["compile"]["target"] == first["subject"]["ref"]
    assert first["evidence_count"] == 1 and first["kind"] == "episode.compile"

    one = upkeep.review(vault, limit=1)
    assert [item["family"] for item in one["items"]] == [dreamer_families.FOLD_FAMILY]
    assert one["truncated"] is True, "a sensed item never displaces a structural one under a limit"

    monkeypatch.setenv("EXOMEM_SENSING", "off")
    assert stripped(upkeep.review(vault, limit=50)) == stripped(on)
    assert FAMILY not in upkeep.review(vault, limit=50)["evidence_complete"]


def test_a_listed_item_is_stamped_once_for_the_effect_counter(tmp_path: Path, monkeypatch) -> None:
    vault, _stub = two_recaps(tmp_path, monkeypatch)
    assert review_state.ReviewStateStore(vault).load().get("surfaced") in (None, {})

    assert len(sensed(vault)) == 2
    stamps = review_state.ReviewStateStore(vault).load()["surfaced"]
    assert len(stamps) == 2
    assert {entry["family"] for entry in stamps.values()} == {FAMILY}
    assert {entry["surface"] for entry in stamps.values()} == {"review"}
    first_stamp = dict(stamps)

    assert len(sensed(vault)) == 2
    assert review_state.ReviewStateStore(vault).load()["surfaced"] == first_stamp


def test_the_item_and_its_context_resolve_by_ref(tmp_path: Path, monkeypatch) -> None:
    vault, _stub = two_recaps(tmp_path, monkeypatch)
    [first, _second] = sensed(vault)

    shown = invoke_command(_cmd("review_memory"), vault, mode="item", ref=first["ref"])["item"]
    assert shown["compile"] == first["compile"] and shown["fingerprint"] == first["fingerprint"]
    context = invoke_command(_cmd("review_item_context"), vault, ref=first["ref"])
    assert context["item"]["compile"] == first["compile"]
    assert context["subject"]["ref"] == first["compile"]["target"]
    assert [page["ref"] for page in context["evidence"]] == [first["compile"]["episode"]]


def test_a_dismissal_holds_until_the_due_claim_set_changes(tmp_path: Path, monkeypatch) -> None:
    """The fingerprint is the due claims: a re-tick keeps the dismissal, a changed set reopens it."""
    vault, stub = start_with_two_claims(tmp_path, monkeypatch)
    [item] = sensed(vault)
    assert [claim["claim_index"] for claim in item["compile"]["claims"]] == [0, 1]

    invoke_command(
        _cmd("triage_memory"), vault, ref=item["ref"], action="dismiss", why="handled: probe"
    )
    assert sensed(vault) == []
    sf.settle(vault)
    [dismissed] = sensed(vault, state="dismissed")
    assert dismissed["fingerprint"] == item["fingerprint"]

    # The home now states the first claim; only the second is still due.
    fx.edit(vault, sf.SEAL, seal_page(sf.WINTER_TEXT))
    sf.settle(vault)
    stub.table[(sf.WINTER_TEXT, CLAIM)] = "entailed"
    assert sense_coverage(vault, stub) == 2
    sf.settle(vault)

    [reopened] = sensed(vault)
    assert [claim["claim_index"] for claim in reopened["compile"]["claims"]] == [1]
    assert reopened["ref"] == item["ref"] and reopened["fingerprint"] != item["fingerprint"]


def start_with_two_claims(tmp_path: Path, monkeypatch) -> tuple[Path, sf.CoverageStubInstrument]:
    stub = sf.CoverageStubInstrument()
    vault = start(tmp_path, monkeypatch, recap_page([LINE, OTHER_LINE]))
    sense_coverage(vault, stub)
    sf.settle(vault)
    return vault, stub


def test_a_family_setting_applies_to_the_sensed_items(tmp_path: Path, monkeypatch) -> None:
    vault, _stub = two_recaps(tmp_path, monkeypatch)
    triage = _cmd("triage_memory")

    invoke_command(triage, vault, ref=FAMILY_REF, action="quiet", why="too_frequent: probe")
    [quiet, _other] = sensed(vault)
    assert quiet["disposition"]["family"] == "quiet"

    invoke_command(triage, vault, ref=FAMILY_REF, action="off", why="too_frequent: probe")
    assert sensed(vault) == []
    assert FAMILY not in upkeep.review(vault, limit=50)["evidence_complete"]
    assert sensed(vault, categories=[FAMILY]) == [], "off also drops the explicit category request"

    invoke_command(triage, vault, ref=FAMILY_REF, action="normal", why="too_frequent: probe")
    assert len(sensed(vault)) == 2


# ----------------------------------------------------------------------
# per-caller twins: a withheld page equals an absent one
# ----------------------------------------------------------------------


def _vault(root: Path, recaps: list[tuple[str, str]], *, home: bool = True, govern=()) -> Path:
    """A minimal vault: the home (optional), a bystander and these recaps, all sensed."""
    freshness.clear()
    vault = root / "vault"
    if home:
        fx.write(vault, sf.SEAL, sf.note("Seal wear", "2026-04-01", sf.SEAL_TEXT))
    fx.write(vault, sf.INLET, sf.note("Inlet pressure", "2026-05-03", sf.INLET_TEXT))
    for path, text in recaps:
        fx.write(vault, path, text)
    fx.seed(vault)
    fx.publish_graph(vault)
    sf.settle(vault)
    sense_coverage(vault, sf.CoverageStubInstrument())
    sf.settle(vault)
    if govern:
        _govern(vault, list(govern))
    _reset_caches()
    return vault


def _observe(vault: Path, principal, refs: dict[str, str]) -> dict[str, tuple[str, str]]:
    """Everything the caller can observe about the listed refs, in a fixed order of calls."""
    freshness.clear()
    fx.seed(vault)
    _reset_caches()
    seen = {}
    with request_scope(principal):
        seen["review"] = _outcome(lambda: upkeep.review(vault, limit=50, state="all"))
        for name, ref in refs.items():
            seen[f"item:{name}"] = _outcome(
                lambda ref=ref: invoke_command(_cmd("review_memory"), vault, mode="item", ref=ref)
            )
            seen[f"context:{name}"] = _outcome(
                lambda ref=ref: invoke_command(_cmd("review_item_context"), vault, ref=ref)
            )
            seen[f"triage:{name}"] = _outcome(
                lambda ref=ref: invoke_command(
                    _cmd("triage_memory"), vault, ref=ref, action="dismiss", why="handled: probe"
                )
            )
    return seen


def _owner_refs(vault: Path) -> dict[str, str]:
    freshness.clear()
    fx.seed(vault)
    _reset_caches()
    with request_scope(owner_principal()):
        return {
            item["compile"]["episode"]: item["ref"]
            for item in upkeep.review(vault, limit=50)["items"]
            if item["family"] == FAMILY
        }


def _recaps_pair(monkeypatch, root: Path, *, recaps, twin_recaps, withheld, home_twin=True):
    """The real vault with `withheld` governed, and its twin where those pages do not exist."""
    sf.enable(monkeypatch, sf.STUB, sf.COVERAGE_STUB)
    real = _vault(root / "real", recaps, govern=withheld)
    twin = _vault(root / "twin", twin_recaps, home=home_twin)
    return real, twin


def test_a_withheld_home_equals_an_absent_one(tmp_path: Path, monkeypatch) -> None:
    page = recap_page([LINE])
    real, twin = _recaps_pair(
        monkeypatch, tmp_path, recaps=[page], twin_recaps=[page], home_twin=False,
        withheld=["Notes/Insights/seal-wear.md"],
    )
    owner = _owner_refs(real)
    assert len(owner) == 1, "withheld, not absent: the owner is served the item"
    refs = {"home": next(iter(owner.values()))}

    withheld = _observe(real, _external(), refs)

    assert withheld == _observe(twin, _external(), refs)
    assert json.loads(withheld["review"][1])["items"] == []
    assert withheld["item:home"][0] != "ok", "the ref reads as an unknown one"


def test_a_withheld_recap_equals_an_absent_one(tmp_path: Path, monkeypatch) -> None:
    visible = recap_page([LINE])
    restricted = recap_page([OTHER_LINE], key=ELSEWHERE, when=LATER)
    real, twin = _recaps_pair(
        monkeypatch, tmp_path, recaps=[visible, restricted], twin_recaps=[visible],
        withheld=[restricted[0].removeprefix(f"{fx.KB}/")],
    )
    owner = _owner_refs(real)
    assert len(owner) == 2, "withheld, not absent: the owner is served both items"
    visible_ref = _owner_refs(twin)
    assert len(visible_ref) == 1
    [restricted_ref] = [ref for ref in owner.values() if ref not in visible_ref.values()]
    refs = {"restricted": restricted_ref}

    withheld = _observe(real, _external(), refs)

    assert withheld == _observe(twin, _external(), refs)
    served = json.loads(withheld["review"][1])["items"]
    assert [item["ref"] for item in served] == list(visible_ref.values())
    assert withheld["item:restricted"][0] != "ok"
