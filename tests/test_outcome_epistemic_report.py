"""The epistemic report's readings: a family, its position, its false-alarm rate, its prediction, the lineage groups
and the background norms, each read from fixed score tables.

No model runs. Each case invents a small set of probes and the scores of the arms on them, and checks the one thing
that its reading's rule says (design.md, decision 6 and the change's `epistemic-utility-regression` delta). A family
needs 20 position probes before it reads more than `too few probes`, so most cases hold 20.
"""

from __future__ import annotations

import pytest

from benchmarks.membench.outcome.epistemic import readings, report
from benchmarks.membench.outcome.epistemic.readings import catalogue

ARMS = ["N", "G", "F", "P"]
PROJECTION = {
    "f38": {"mutation_behaviours": ["over_flags"]},
    "families": {
        "currency": {"predict": "fail", "models": "yes"},
        "scope": {"predict": "partial", "models": "partial"},
    },
}


def probes(
    family: str,
    count: int,
    *,
    prefix=None,
    behaviours=("uses_current",),
    reading=None,
    phenomenon=None,
) -> dict:
    """``count`` probes of one family, keyed by (project, probe id); ``prefix`` names their ids when they differ."""
    key = {"behaviours": list(behaviours), **({"reading": reading} if reading else {})}
    name = prefix or family
    return {
        ("proj", f"{name}-{i:02d}"): {
            "probe_id": f"{name}-{i:02d}",
            "dimension": "supersession",
            "change_id": f"{name}-{i:02d}",
            "family": family,
            **({"phenomenon": phenomenon} if phenomenon else {}),
            "key": key,
        }
        for i in range(count)
    }


def answered(meta: dict, **correct: list[int]) -> tuple[list[dict], dict]:
    """The outcomes and the per-probe scores when each arm gets the given 0 or 1 on the probes, in id order."""
    units = sorted(meta)
    outcomes = [
        {
            "arm": arm,
            "project": project,
            "probe_id": probe_id,
            "window": 1,
            "state": "answered",
            "verdict": {"correct": bool(score)},
        }
        for arm, scores in correct.items()
        for (project, probe_id), score in zip(units, scores, strict=True)
    ]
    scores = {arm: dict(zip(units, list(values), strict=True)) for arm, values in correct.items()}
    return outcomes, scores


def read_families(meta: dict, outcomes: list[dict], scores: dict, *, arms=ARMS, earlier=()) -> dict:
    return readings.section(
        meta=meta,
        outcomes=outcomes,
        unit_scores={arm: {u: float(s) for u, s in table.items()} for arm, table in scores.items()},
        arms=arms,
        seed=1,
        earlier=earlier,
        projection=PROJECTION,
        harm_of=lambda units: None,
    )


def first(count: int, of: int) -> list[int]:
    """``count`` right answers followed by wrong ones, ``of`` in all."""
    return [1] * count + [0] * (of - count)


# -- the readings ---------------------------------------------------------------------------------


def test_a_family_of_three_probes_reads_too_few_probes_and_prints_no_position() -> None:
    """The ceiling leads the floor on all three, and a count of three still says nothing about the family."""
    meta = probes("currency", 3)
    outcomes, scores = answered(meta, N=[0] * 3, G=[1] * 3, F=[0] * 3, P=[1] * 3)

    row = read_families(meta, outcomes, scores)["rows"]["currency"]

    assert row["reading"] == "too few probes"
    assert row["probes"] == {"played": 3, "position": 3, "harm": 0, "stable": 0, "in_case_set": 3}
    for printed in ("positions", "ceiling_lead", "saturation"):
        assert printed not in row, f"a family with three probes printed {printed}"
    # The filed prediction sits beside a result that cannot be read yet: unknown, not agreed or contradicted.
    assert row["prediction"]["predicted"] == "fail"
    assert (
        row["prediction"]["contradicted"] is None
        and "too few probes" in row["prediction"]["unknown"]
    )


def test_a_family_that_no_probe_sampled_reads_not_measured_and_not_zero() -> None:
    meta = probes("currency", 3)
    outcomes, scores = answered(meta, N=[0] * 3, G=[1] * 3)

    rows = read_families(meta, outcomes, scores)["rows"]

    assert rows["scope"]["reading"] == "not measured"
    assert rows["scope"]["probes"]["played"] == 0 and rows["scope"]["prediction"] is None


def test_a_family_where_the_ceiling_leads_the_floor_reads_room_and_prints_each_arms_position() -> (
    None
):
    meta = probes("currency", 20)
    outcomes, scores = answered(meta, N=[0] * 20, G=[1] * 20, F=first(5, 20), P=first(10, 20))

    row = read_families(meta, outcomes, scores)["rows"]["currency"]

    assert row["reading"] == "room"
    assert row["ceiling_lead"]["ci95"] == [1.0, 1.0]
    # (arm - N) / (G - N): half of the way for P, a quarter for F, and none printed for the two reference arms.
    assert {arm: p["position"] for arm, p in row["positions"].items()} == {"F": 0.25, "P": 0.5}


def test_a_family_that_the_floor_already_solves_reads_no_room_and_prints_no_position() -> None:
    meta = probes("currency", 20)
    outcomes, scores = answered(meta, N=first(19, 20), G=[1] * 20, F=first(19, 20), P=[1] * 20)

    row = read_families(meta, outcomes, scores)["rows"]["currency"]

    assert row["ceiling_lead"]["ci95"][1] < 0.2
    assert row["reading"] == "no room" and "positions" not in row


def test_a_family_whose_ceiling_lead_straddles_the_threshold_reads_room_unclear() -> None:
    meta = probes("currency", 20)
    outcomes, scores = answered(meta, N=[0] * 20, G=first(6, 20), F=[0] * 20, P=first(3, 20))

    row = read_families(meta, outcomes, scores)["rows"]["currency"]

    low, high = row["ceiling_lead"]["ci95"]
    assert low < 0.2 <= high
    assert row["reading"] == "room unclear" and "positions" not in row


def test_the_primary_arm_at_the_ceiling_in_one_report_alone_does_not_read_saturated() -> None:
    meta = probes("currency", 20)
    outcomes, scores = answered(meta, N=[0] * 20, G=[1] * 20, F=[0] * 20, P=[1] * 20)

    row = read_families(meta, outcomes, scores)["rows"]["currency"]

    assert row["saturation"]["above_margin"] is True
    assert row["saturation"]["earlier_reports_above_margin"] == []
    assert row["reading"] == "room"


def test_the_primary_arm_at_the_ceiling_in_two_reports_in_a_row_reads_saturated() -> None:
    meta = probes("currency", 20)
    outcomes, scores = answered(meta, N=[0] * 20, G=[1] * 20, F=[0] * 20, P=[1] * 20)
    before = {"description": {"families": read_families(meta, outcomes, scores)}}

    row = read_families(meta, outcomes, scores, earlier=[before])["rows"]["currency"]

    assert row["reading"] == "saturated"
    assert row["saturation"]["earlier_reports_above_margin"] == [True]


def test_a_family_that_the_floor_already_solves_reads_no_room_even_when_the_primary_arm_has_stayed_at_the_ceiling() -> (
    None
):
    """With no room, the ceiling is no more than the floor: `no room` says that, and `saturated` would hide it."""
    meta = probes("currency", 20)
    outcomes, scores = answered(meta, N=first(19, 20), G=[1] * 20, F=first(19, 20), P=[1] * 20)
    before = {"description": {"families": read_families(meta, outcomes, scores)}}

    row = read_families(meta, outcomes, scores, earlier=[before])["rows"]["currency"]

    assert (
        row["saturation"]["earlier_reports_above_margin"] == [True]
        and row["saturation"]["above_margin"]
    )
    assert row["reading"] == "no room"


def test_an_earlier_report_that_left_the_margin_keeps_a_family_from_reading_saturated() -> None:
    meta = probes("currency", 20)
    outcomes, scores = answered(meta, N=[0] * 20, G=[1] * 20, F=[0] * 20, P=[1] * 20)
    behind = answered(meta, N=[0] * 20, G=[1] * 20, F=[0] * 20, P=first(10, 20))
    before = {"description": {"families": read_families(meta, *behind)}}

    row = read_families(meta, outcomes, scores, earlier=[before])["rows"]["currency"]

    assert row["saturation"]["earlier_reports_above_margin"] == [False]
    assert row["reading"] == "room"


def test_the_readings_file_describes_every_reading_that_the_code_decides(
    tmp_path, monkeypatch
) -> None:
    """A renamed reading in the data would otherwise print under a name that no rule explains."""
    broken = tmp_path / "readings.yaml"
    broken.write_text(readings.READINGS_FILE.read_text().replace("room unclear:", "unclear room:"))
    monkeypatch.setattr(readings, "READINGS_FILE", broken)
    readings.rules.cache_clear()
    try:
        with pytest.raises(SystemExit, match="must describe exactly"):
            readings.rules()
    finally:
        monkeypatch.undo()
        readings.rules.cache_clear()


# -- position, harm and false alarms --------------------------------------------------------------


def test_a_probe_read_as_harm_takes_no_part_in_a_position_and_is_read_as_harm_against_n() -> None:
    """N answers the four harm probes correctly, so a ceiling lead that counted them would read 0.83, not 1."""
    harm_probes = probes(
        "currency", 4, prefix="harm", reading="harm", behaviours=("invents_detail",)
    )
    meta = {**probes("currency", 20), **harm_probes}
    units = sorted(meta)
    n_scores = [1 if u in harm_probes else 0 for u in units]
    p_scores = [0 if u in sorted(harm_probes)[:2] else 1 for u in units]  # P loses two of the four
    outcomes, scores = answered(meta, N=n_scores, G=[1] * 24, F=n_scores, P=p_scores)

    families = readings.section(
        meta=meta,
        outcomes=outcomes,
        unit_scores=scores,
        arms=ARMS,
        seed=1,
        earlier=[],
        projection=PROJECTION,
        harm_of=lambda units: report.harm_read_probes(outcomes, meta, ARMS, set(units)),
    )
    row = families["rows"]["currency"]

    assert row["probes"]["position"] == 20 and row["probes"]["harm"] == 4
    assert row["ceiling_lead"]["mean_difference"] == 1.0
    harm = row["harm"]["P"]
    assert (harm["harmed"], harm["solved_by_n"], harm["rate"]) == (2, 4, 0.5)


def test_the_paired_differences_that_a_decision_reads_leave_out_a_probe_read_as_harm() -> None:
    meta = {**probes("currency", 3), **probes("currency", 1, prefix="harm", reading="harm")}
    outcomes, _ = answered(meta, N=[0, 0, 0, 1], P=[1, 1, 0, 0])
    table = report.score_tables(outcomes, ["N", "P"])

    row = report.pair_row("P", "N", table, meta)

    assert row["paired_probes"] == 3 and row["mean_difference"] == 0.6667, (
        "the harm probe's -1 was left out"
    )


def test_the_false_alarm_rate_counts_the_answers_on_which_the_judge_finds_over_flags() -> None:
    """Four probes ask for the false-alarm behaviour. P flags three of them, N none, and one P answer never came."""
    meta = {
        **probes("currency", 5, behaviours=("over_flags",)),
        **probes("scope", 2, behaviours=("keeps_scope",)),
    }
    flagged = {
        "currency-00": "yes",
        "currency-01": "yes",
        "currency-02": "yes",
        "currency-03": "no",
    }

    def verdict(arm: str, probe_id: str) -> dict:
        found = flagged.get(probe_id, "no") if arm == "P" else "no"
        return {"correct": True, "over_flags": found, "keeps_scope": "yes"}

    outcomes = [
        {
            "arm": arm,
            "project": "proj",
            "probe_id": probe_id,
            "window": 1,
            "state": "unanswered" if (arm, probe_id) == ("P", "currency-04") else "answered",
            "verdict": verdict(arm, probe_id),
        }
        for arm in ("N", "P")
        for _, probe_id in sorted(meta)
    ]
    scores = {arm: {u: 1.0 for u in meta} for arm in ("N", "P")}

    rows = read_families(meta, outcomes, scores, arms=["N", "P"])["rows"]

    currency = rows["currency"]["false_alarm"]
    assert currency["probes"] == 5
    assert (
        currency["arms"]["P"]["yes"],
        currency["arms"]["P"]["no"],
        currency["arms"]["P"]["rate"],
    ) == (3, 1, 0.75)
    assert currency["arms"]["P"]["unknown"] == 1, "the missing answer is neither a flag nor a pass"
    assert currency["arms"]["N"]["rate"] == 0.0
    # A family whose probes never ask for it has no rate to print: unknown, not 0.
    assert rows["scope"]["false_alarm"]["probes"] == 0
    assert rows["scope"]["false_alarm"]["arms"]["P"]["rate"] is None


# -- predictions ----------------------------------------------------------------------------------


def test_a_family_predicted_to_fail_that_reads_at_its_ceiling_is_listed_with_the_probes_to_read_first() -> (
    None
):
    meta = probes("currency", 20)
    outcomes, scores = answered(meta, N=[0] * 20, G=[1] * 20, F=[0] * 20, P=[1] * 20)

    families = read_families(meta, outcomes, scores)

    assert families["rows"]["currency"]["prediction"] == {
        "predicted": "fail",
        "models": "yes",
        "observed": "pass",
        "contradicted": True,
    }
    (listed,) = families["contradicted_predictions"]
    assert (listed["family"], listed["predicted"], listed["observed"]) == (
        "currency",
        "fail",
        "pass",
    )
    assert listed["read_first"] == [f"proj:currency-{i:02d}" for i in range(20)]


@pytest.mark.parametrize(
    ("primary", "folder", "outcome"),
    [
        (20, 0, "pass"),  # P reaches the ceiling
        (10, 0, "partial"),  # P is above the folder and below the ceiling
        (3, 3, "fail"),  # P is no better than the folder
    ],
)
def test_the_observed_outcome_of_a_family_reads_the_primary_arm_against_the_folder_and_the_ceiling(
    primary: int, folder: int, outcome: str
) -> None:
    meta = probes("scope", 20)
    outcomes, scores = answered(
        meta, N=[0] * 20, G=[1] * 20, F=first(folder, 20), P=first(primary, 20)
    )

    row = read_families(meta, outcomes, scores)["rows"]["scope"]

    assert row["prediction"]["observed"] == outcome
    assert row["prediction"]["contradicted"] is (outcome != "partial")


# -- lineage and norms ----------------------------------------------------------------------------


def test_the_report_gives_the_share_of_probes_per_lineage_group_and_counts_an_unknown_phenomenon_apart() -> (
    None
):
    entries = catalogue().entries
    home = next(i for i, e in entries.items() if "home-design" in e.lineage)
    plain = next(i for i, e in entries.items() if not e.lineage)
    meta = {
        **probes("entities", 2, phenomenon=home),
        **probes("machine", 1, phenomenon=plain),
        **probes("currency", 1),  # a hand-written probe: it names no phenomenon
    }

    shares = readings.lineage_shares(meta)

    assert shares["probes"] == 3 and shares["unknown_phenomenon"] == 1
    assert shares["groups"]["home-design"] == {"probes": 2, "share": 0.6667}
    assert shares["no_lineage"] == {"probes": 1, "share": 0.3333}


def test_the_norm_table_gives_the_keys_that_rest_on_a_background_norm_and_the_share_the_history_arm_reaches() -> (
    None
):
    entries = catalogue().entries
    resting = [i for i, e in entries.items() if "later-replaces" in e.norms][:4]
    meta = {
        u: p
        for i, entry in enumerate(resting)
        for u, p in probes("currency", 1, prefix=f"key{i}", phenomenon=entry).items()
    }
    units = sorted(meta)
    history = {
        units[0]: 1.0,
        units[1]: 1.0,
        units[2]: 0.0,
    }  # the fourth key has no answer: unknown, not missed

    table = readings.background_norms(meta, history, declared_share=0.9, history_played=True)

    later = table["norms"]["later-replaces"]
    assert (later["keys"], later["answered_by_history"], later["reached_share"]) == (4, 3, 0.6667)
    assert (
        later["below_declared_share"] is True and "later-replaces" in table["below_declared_share"]
    )
    untouched = next(n for n, row in table["norms"].items() if row["keys"] == 0)
    assert table["norms"][untouched]["reached_share"] is None, (
        "a norm with no key reads unknown, not 0"
    )
    assert set(table["norms"]) == {
        n for n, basis in catalogue().norms.items() if basis == "background"
    }
