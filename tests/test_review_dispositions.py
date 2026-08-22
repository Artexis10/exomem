"""Per-family dispositions: "stop suggesting this kind of thing", durably.

Triage state is per item. A family a user has decided is noise for their corpus
comes back with every new page that trips it, and the only escape today is
prominence `off`, which silences everything. This module is the contract for the
family-level answer: `quiet` leaves the default union, every due-state carrier
and write-path advisory emission while staying reachable on explicit request;
`off` additionally leaves explicit category review except the all-states view;
audit measurement is unaffected by either.

The first test is the GAP PROOF and is written to fail on today's runtime for
the right reason: the decision cannot be expressed through the documented
surface, and a decision written straight into the store changes nothing on any
surface that would carry it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _nag_governance_helpers import (
    advisory_candidate,
    doubly_flagged,
    overdue_prediction,
    scratch_page,
    seed_page,
)

from exomem import attention as attention_module
from exomem import find as find_module
from exomem import commands, corpus_aware, review_state

FAMILY = "prediction_window"
FAMILY_REF = f"exomem://review/family/{FAMILY}"
WHY = "too_frequent: predictions fire more than they help in this vault"


def _hand_written_disposition(vault: Path, family: str, disposition: str) -> None:
    """Record the decision by editing the store, bypassing the surface entirely.

    The gap proof has to separate two failures that would otherwise hide each
    other: the surface cannot express the decision, AND nothing reads it. This
    writes the decision the implementation will write, so the second half is
    measured even though the first half refuses.
    """
    path = review_state.state_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.loads(path.read_text(encoding="utf-8"))
        if path.is_file()
        else {"version": review_state.SCHEMA_VERSION, "records": {}}
    )
    payload.setdefault("dispositions", {})[family] = {
        "family": family,
        "disposition": disposition,
        "reason": "too_frequent",
        "why": WHY,
        "updated_at": "2026-08-20T00:00:00Z",
        "origin": "manual",
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _default_union_paths(vault: Path) -> list[str]:
    return [item["path"] for item in commands.op_attention(vault, limit=0)["items"]]


def _carrier_categories(vault: Path) -> dict:
    from exomem import due_state as due_state_module

    block = due_state_module.served(vault)
    return (block or {}).get("categories", {})


def _write_advisories(vault: Path) -> list[str]:
    target = seed_page(vault, "nag-editable", "Repeated body.")
    return corpus_aware.emit_write_advisories(
        vault,
        self_path=target,
        kind="near-duplicate",
        candidates=[advisory_candidate(vault)],
    )


# ==========================================================================
# THE GAP PROOF — red before any implementation exists
# ==========================================================================


def test_a_family_cannot_be_quieted_today_and_a_recorded_one_is_ignored(
    vault: Path,
) -> None:
    """RED-FIRST. One decision, four surfaces that should honour it, zero do.

    Collected rather than asserted one at a time: the gap is that family-level
    intent has no representation anywhere, so the failure has to name the whole
    surface set instead of stopping at whichever one is checked first.
    """
    overdue_prediction(vault)
    scratch_page(vault)

    unheard: list[str] = []

    try:
        commands.op_triage_memory(vault, ref=FAMILY_REF, action="quiet", why=WHY)
    except ValueError as error:
        unheard.append(f"the triage surface refused the family reference ({error})")

    _hand_written_disposition(vault, FAMILY, "quiet")
    _hand_written_disposition(vault, "near-duplicate", "quiet")

    if any("nag-backlog" in path for path in _default_union_paths(vault)):
        unheard.append("the default attention union still lists the quiet family's page")
    if FAMILY in _carrier_categories(vault):
        unheard.append("the due-state carrier still counts the quiet family")
    if _write_advisories(vault):
        unheard.append("the write path still emits an advisory of a quiet kind")

    assert unheard == [], (
        "a family set to `quiet` was not honoured: " + "; ".join(unheard)
    )


# ==========================================================================
# setting and clearing a disposition
# ==========================================================================


def test_quieting_a_family_reports_the_decision(vault: Path) -> None:
    overdue_prediction(vault)
    result = commands.op_triage_memory(vault, ref=FAMILY_REF, action="quiet", why=WHY)

    assert result["family"] == FAMILY
    assert result["disposition"] == "quiet"
    assert result["reason"] == "too_frequent"
    assert result["why"] == WHY
    assert result["origin"] == "manual"
    assert result["ref"] == FAMILY_REF


def test_normal_clears_the_record(vault: Path) -> None:
    overdue_prediction(vault)
    commands.op_triage_memory(vault, ref=FAMILY_REF, action="quiet", why=WHY)
    result = commands.op_triage_memory(vault, ref=FAMILY_REF, action="normal")

    assert result["disposition"] == "normal"
    store = review_state.ReviewStateStore(vault)
    assert review_state.disposition_for(FAMILY, payload=store.load()) == "normal"


def test_an_unregistered_family_is_refused_and_changes_nothing(vault: Path) -> None:
    store = review_state.ReviewStateStore(vault)
    before = json.dumps(store.load(), sort_keys=True)
    with pytest.raises(ValueError, match="INVALID_REVIEW_FAMILY"):
        commands.op_triage_memory(
            vault,
            ref="exomem://review/family/not_a_real_family",
            action="quiet",
            why=WHY,
        )
    assert json.dumps(store.load(), sort_keys=True) == before


def test_a_write_advisory_kind_is_a_registered_family(vault: Path) -> None:
    result = commands.op_triage_memory(
        vault,
        ref="exomem://review/family/near-duplicate",
        action="quiet",
        why="false_positive: this vault keeps deliberate near-duplicates",
    )
    assert result["disposition"] == "quiet"


def test_item_actions_are_refused_on_a_family_reference(vault: Path) -> None:
    overdue_prediction(vault)
    for action in ("dismiss", "snooze", "reopen", "competing"):
        with pytest.raises(ValueError, match="INVALID_REVIEW_ACTION"):
            commands.op_triage_memory(
                vault, ref=FAMILY_REF, action=action, until="2030-01-01", why=WHY
            )
    store = review_state.ReviewStateStore(vault)
    assert review_state.disposition_for(FAMILY, payload=store.load()) == "normal"


def test_disposition_actions_are_refused_on_an_item_reference(vault: Path) -> None:
    overdue_prediction(vault)
    scratch_page(vault)
    item = commands.op_attention(vault, limit=0)["items"][0]
    for action in ("quiet", "off", "normal"):
        with pytest.raises(ValueError, match="INVALID_REVIEW_ACTION"):
            commands.op_triage_memory(vault, ref=item["ref"], action=action, why=WHY)


# ==========================================================================
# the D2 effects table, one row at a time
# ==========================================================================


def test_a_quiet_family_leaves_the_default_union(vault: Path) -> None:
    overdue_prediction(vault)
    assert any("nag-backlog" in p for p in _default_union_paths(vault))

    commands.op_triage_memory(vault, ref=FAMILY_REF, action="quiet", why=WHY)

    assert not any("nag-backlog" in p for p in _default_union_paths(vault))


def test_a_quiet_family_is_still_reachable_on_explicit_review_and_is_annotated(
    vault: Path,
) -> None:
    overdue_prediction(vault)
    commands.op_triage_memory(vault, ref=FAMILY_REF, action="quiet", why=WHY)

    report = commands.op_attention(vault, categories=[FAMILY], limit=0)
    items = [item for item in report["items"] if "nag-backlog" in item["path"]]
    assert items, report
    assert items[0]["disposition"] == "quiet"


def test_an_off_family_is_reachable_only_through_the_all_states_view(vault: Path) -> None:
    overdue_prediction(vault)
    commands.op_triage_memory(
        vault, ref=FAMILY_REF, action="off", why="intentional: never useful here"
    )

    open_view = commands.op_attention(vault, categories=[FAMILY], limit=0, state="open")
    assert not [i for i in open_view["items"] if "nag-backlog" in i["path"]]

    all_view = commands.op_attention(vault, categories=[FAMILY], limit=0, state="all")
    items = [i for i in all_view["items"] if "nag-backlog" in i["path"]]
    assert items, all_view
    assert items[0]["disposition"] == "off"


def test_exclusion_happens_before_fusion(vault: Path) -> None:
    """An item flagged only by a quiet family disappears; a doubly-flagged one keeps
    its other reasons rather than vanishing with them."""
    overdue_prediction(vault, "nag-solo")
    # A page with a due prediction AND no outbound relations earns relation_debt too.
    from _nag_governance_helpers import write

    write(
        vault,
        "Knowledge Base/Notes/Insights/nag-both.md",
        "---\ntitle: nag-both\ntype: insight\nstatus: active\n"
        "created: 2026-01-01\nupdated: 2026-01-01\n---\n\n"
        "## Prediction\n\n- id: p2\n- check_by: 2020-01-01\n\nA claim.\n",
    )

    before = commands.op_attention(vault, limit=0)["items"]
    both = [i for i in before if "nag-both" in i["path"]]
    assert both and len(both[0]["categories"]) > 1, before

    commands.op_triage_memory(vault, ref=FAMILY_REF, action="quiet", why=WHY)

    after = commands.op_attention(vault, limit=0)["items"]
    assert not [i for i in after if "nag-solo" in i["path"]]
    surviving = [i for i in after if "nag-both" in i["path"]]
    assert surviving, after
    assert FAMILY not in surviving[0]["categories"]


def test_a_quiet_family_contributes_nothing_to_any_carrier(vault: Path) -> None:
    from exomem import due_state as due_state_module

    overdue_prediction(vault)
    scratch_page(vault)
    assert due_state_module.served(vault)["categories"] == {FAMILY: 1}

    commands.op_triage_memory(vault, ref=FAMILY_REF, action="quiet", why=WHY)

    assert due_state_module.served_entries(vault) == []
    assert due_state_module.served(vault) is None


@pytest.mark.parametrize(
    ("audience", "surface"), [("owner", "cli"), ("principal:external", "mcp")]
)
def test_a_quiet_family_counts_zero_for_every_audience(
    vault: Path, audience: str, surface: str
) -> None:
    """The exclusion is a property of the vault's decision, not of who asked.

    Emission governance IS per audience, so a carrier that went quiet for the
    second audience only because the first had already spent its emission would
    look identical here. The counts are read directly, before any governor.
    """
    from exomem import due_state as due_state_module
    from exomem.governance.principal import RequestPrincipal, request_scope

    overdue_prediction(vault)
    scratch_page(vault)
    principal = RequestPrincipal(audience_id=audience, surface=surface)
    with request_scope(principal):
        assert due_state_module.served(vault)["categories"] == {FAMILY: 1}

    commands.op_triage_memory(vault, ref=FAMILY_REF, action="quiet", why=WHY)

    with request_scope(principal):
        assert due_state_module.served_entries(vault) == []
        assert due_state_module.served(vault) is None


def test_a_disposition_governs_the_activation_surface_too(vault: Path) -> None:
    """`activation` is a served review surface, so a disposition binds it.

    It ranks its own measurements and shares `_apply_review_state` with
    `attention`, which means it was already STAMPING the first-surfaced ledger
    for signals a disposition had removed everywhere else. A surface that
    records a first surfacing for a family the user silenced is both leaking the
    family back onto a review surface and writing a false measurement about it.
    """
    seed_page(vault, "nag-orphan", "A page with no relations at all.")
    default = commands.op_review_memory(vault, mode="activation", limit=0)
    assert any("relation_debt" in item["categories"] for item in default["items"])

    commands.op_triage_memory(
        vault,
        ref="exomem://review/family/relation_debt",
        action="off",
        why="intentional: this vault does not use relations",
    )

    after = commands.op_review_memory(vault, mode="activation", limit=0)
    assert not any("relation_debt" in item["categories"] for item in after["items"])
    ledger = review_state.ReviewStateStore(vault).load()["surfaced"]
    assert all(
        not key.startswith("relation_debt") for key in ledger
    )  # nothing keyed by a silenced family reached the ledger


def test_an_explicit_activation_request_annotates_rather_than_pretends(
    vault: Path,
) -> None:
    """Same asymmetry as attention: asking by name is asking anyway."""
    seed_page(vault, "nag-orphan", "A page with no relations at all.")
    commands.op_triage_memory(
        vault,
        ref="exomem://review/family/relation_debt",
        action="quiet",
        why="too_frequent: every page trips this",
    )
    named = commands.op_review_memory(
        vault, mode="activation", categories=["relation_debt"], limit=0
    )
    listed = [item for item in named["items"] if "relation_debt" in item["categories"]]
    assert listed
    assert {item.get("disposition") for item in listed} == {"quiet"}


def test_a_quiet_kind_emits_no_write_advisory(vault: Path) -> None:
    assert _write_advisories(vault)
    commands.op_triage_memory(
        vault,
        ref="exomem://review/family/near-duplicate",
        action="quiet",
        why="false_positive: deliberate near-duplicates here",
    )
    assert _write_advisories(vault) == []


def test_audit_measurement_is_unaffected_by_a_disposition(vault: Path) -> None:
    overdue_prediction(vault)
    commands.op_triage_memory(
        vault, ref=FAMILY_REF, action="off", why="intentional: never useful here"
    )
    report = commands.op_audit(vault, categories=[FAMILY], detail="full")
    findings = json.dumps(report)
    assert "nag-backlog" in findings, report


def test_triage_of_an_item_in_a_quiet_family_is_still_allowed(vault: Path) -> None:
    overdue_prediction(vault)
    scratch_page(vault)
    item = [
        i
        for i in commands.op_attention(vault, limit=0)["items"]
        if "nag-backlog" in i["path"]
    ][0]

    commands.op_triage_memory(vault, ref=FAMILY_REF, action="quiet", why=WHY)

    result = commands.op_triage_memory(
        vault, ref=item["ref"], action="dismiss", why="handled: dealt with elsewhere"
    )
    assert result["state"] == "dismissed"


# ==========================================================================
# persistence and composition
# ==========================================================================


def test_a_disposition_survives_a_restart_and_prominence_changes(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import prominence as prominence_module

    overdue_prediction(vault)
    recorded = commands.op_triage_memory(vault, ref=FAMILY_REF, action="quiet", why=WHY)

    # "Restart" for a file-backed store is a fresh store object over the same
    # bytes; nothing is cached in the process that could carry the decision.
    for level in (prominence_module.CANON[0], prominence_module.CANON[-1]):
        monkeypatch.setenv("EXOMEM_PROMINENCE", level)
        assert prominence_module.resolve() == level

    store = review_state.ReviewStateStore(vault)
    payload = store.load()
    assert review_state.disposition_for(FAMILY, payload=payload) == "quiet"
    record = payload["dispositions"][FAMILY]
    assert record["reason"] == "too_frequent"
    assert record["updated_at"] == recorded["updated_at"]


def test_dispositions_and_item_decisions_compose(vault: Path) -> None:
    overdue_prediction(vault, "nag-one")
    overdue_prediction(vault, "nag-two")
    scratch_page(vault)

    commands.op_triage_memory(vault, ref=FAMILY_REF, action="quiet", why=WHY)
    one = [
        i
        for i in commands.op_attention(vault, categories=[FAMILY], limit=0)["items"]
        if "nag-one" in i["path"]
    ][0]
    commands.op_triage_memory(
        vault, ref=one["ref"], action="dismiss", why="handled: dealt with elsewhere"
    )

    commands.op_triage_memory(vault, ref=FAMILY_REF, action="normal")

    states = {
        item["path"].rsplit("/", 1)[-1]: item["state"]
        for item in commands.op_attention(vault, limit=0, state="all")["items"]
    }
    assert states["nag-one.md"] == "dismissed"
    assert states["nag-two.md"] == "open"


def test_a_multi_flagged_item_resurfaces_when_a_family_returns(vault: Path) -> None:
    """The compose rule the singly-flagged case does not exercise.

    The user dismissed a signal composed of `relation_debt` alone, because
    `prediction_window` was quiet and its reason had been dropped before fusion.
    When the family returns the item's composed reasons become both — a signal
    the user has never seen and never decided about — so it is open. Recording
    the restored composition against the earlier decision would leak a dismissal
    across a signal the user never saw, which is the exact hazard the
    fused/component split exists to prevent.

    The decision is not lost: the record still stands under the fingerprint it
    was taken against, and so do the component records the fan-out wrote.
    """
    doubly_flagged(vault)
    scratch_page(vault)

    commands.op_triage_memory(vault, ref=FAMILY_REF, action="quiet", why=WHY)
    quieted = [
        item
        for item in commands.op_attention(vault, limit=0, state="all")["items"]
        if "nag-double" in item["path"]
    ][0]
    assert quieted["categories"] == ["relation_debt"], quieted["categories"]
    commands.op_triage_memory(
        vault, ref=quieted["ref"], action="dismiss", why="handled: dealt with elsewhere"
    )
    dismissed_fingerprint = quieted["fingerprint"]

    commands.op_triage_memory(vault, ref=FAMILY_REF, action="normal")

    restored = [
        item
        for item in commands.op_attention(vault, limit=0, state="all")["items"]
        if "nag-double" in item["path"]
    ][0]
    assert restored["categories"] == ["prediction_window", "relation_debt"]
    assert restored["fingerprint"] != dismissed_fingerprint
    assert restored["state"] == "open"

    records = review_state.ReviewStateStore(vault).load()["records"]
    key = f"{quieted['item_id']}:{dismissed_fingerprint}"
    assert key in records, sorted(records)
    assert records[key]["action"] == "dismiss"
    assert records[key]["reason"] == "handled"


def test_the_dispositions_view_lists_what_is_quiet_and_why(vault: Path) -> None:
    overdue_prediction(vault)
    scratch_page(vault)
    commands.op_triage_memory(vault, ref=FAMILY_REF, action="quiet", why=WHY)
    item = [
        i
        for i in commands.op_attention(vault, categories=[FAMILY], limit=0)["items"]
        if "nag-backlog" in i["path"]
    ][0]
    commands.op_triage_memory(
        vault, ref=item["ref"], action="dismiss", why="handled: dealt with elsewhere"
    )

    view = commands.op_review_memory(vault, mode="dispositions")
    families = {row["family"]: row for row in view["dispositions"]}
    assert families[FAMILY]["disposition"] == "quiet"
    assert families[FAMILY]["reason"] == "too_frequent"
    assert families[FAMILY]["why"] == WHY
    assert families[FAMILY]["origin"] == "manual"
    assert families[FAMILY]["updated_at"]
    assert families[FAMILY]["manual_dismissals"] >= 1


def test_manual_dismissals_are_attributed_by_component_not_by_the_fused_key(
    vault: Path,
) -> None:
    """The count follows the per-finding identity, so it survives recomposition.

    Keyed on the FUSED fingerprint the count is charged to whichever families
    the item happens to be flagged by at read time, and it disappears entirely
    the moment the item's composition moves — because the fused key the view
    looks up is then a key nothing was ever recorded against. Here the page is
    dismissed while `prediction_window` is its only flag and then loses its
    relations, which earns it `relation_debt` and changes the fused fingerprint.

    The component fingerprint is the one `apply_for_item` fanned the decision
    out to, so `prediction_window` keeps its one dismissal and `relation_debt`
    — whose signal nobody put down — correctly has none.
    """
    overdue_prediction(vault, "nag-evolving")
    scratch_page(vault)
    item = [
        entry
        for entry in commands.op_attention(vault, limit=0, state="all")["items"]
        if "nag-evolving" in entry["path"]
    ][0]
    assert item["categories"] == [FAMILY]
    commands.op_triage_memory(
        vault, ref=item["ref"], action="dismiss", why="handled: dealt with elsewhere"
    )

    page = vault / "Knowledge Base/Notes/Insights/nag-evolving.md"
    page.write_text(
        page.read_text(encoding="utf-8").split("## Relations")[0], encoding="utf-8"
    )
    find_module.clear_cache()

    keys = commands._review_keys_by_family(vault)
    payload = review_state.ReviewStateStore(vault).load()
    counts = review_state.manual_dismissals_by_family(payload, keys)
    assert counts[FAMILY] == 1
    assert counts["relation_debt"] == 0

    # The fused key the item now reports is not the one the decision was
    # recorded against — which is exactly why attributing by it loses the count.
    recomposed = [
        entry
        for entry in commands.op_attention(vault, limit=0, state="all")["items"]
        if "nag-evolving" in entry["path"]
    ][0]
    fused = {
        category: [f"{recomposed['item_id']}:{recomposed['fingerprint']}"]
        for category in recomposed["categories"]
    }
    assert review_state.manual_dismissals_by_family(payload, fused) == {
        FAMILY: 0,
        "relation_debt": 0,
    }


# ==========================================================================
# mechanism removal
# ==========================================================================


def test_removing_the_attention_filter_puts_the_quiet_family_back(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The filter is a named mechanism, so its absence is provable."""
    overdue_prediction(vault)
    commands.op_triage_memory(vault, ref=FAMILY_REF, action="quiet", why=WHY)
    assert not any("nag-backlog" in p for p in _default_union_paths(vault))

    monkeypatch.setattr(
        attention_module,
        "_excluded_families",
        lambda *args, **kwargs: (frozenset(), {}),
    )
    assert any("nag-backlog" in p for p in _default_union_paths(vault))


def test_removing_the_carrier_filter_puts_the_quiet_family_back(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import due_state as due_state_module

    overdue_prediction(vault)
    scratch_page(vault)
    commands.op_triage_memory(vault, ref=FAMILY_REF, action="quiet", why=WHY)
    assert due_state_module.served(vault) is None

    monkeypatch.setattr(
        due_state_module, "_excluded_families", lambda *args, **kwargs: frozenset()
    )
    assert due_state_module.served(vault)["categories"] == {FAMILY: 1}


def test_removing_the_advisory_filter_puts_the_quiet_kind_back(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands.op_triage_memory(
        vault,
        ref="exomem://review/family/near-duplicate",
        action="quiet",
        why="false_positive: deliberate near-duplicates here",
    )
    assert _write_advisories(vault) == []

    monkeypatch.setattr(
        corpus_aware, "_excluded_advisory_kinds", lambda *args, **kwargs: frozenset()
    )
    assert _write_advisories(vault)


# ==========================================================================
# the CLI surface
# ==========================================================================


def _cli(argv: list[str], capsys) -> tuple[int, str, str]:
    from exomem.__main__ import main

    try:
        code = main(argv)
    except SystemExit as error:  # argparse usage errors
        code = error.code if isinstance(error.code, int) else 1
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _disposition(vault: Path) -> dict | None:
    payload = review_state.ReviewStateStore(vault).load()
    return (payload.get("dispositions") or {}).get(FAMILY)


def test_the_cli_quiets_a_family_by_bare_name(vault: Path, capsys) -> None:
    code, out, err = _cli(
        [
            "review",
            "quiet",
            FAMILY,
            "--reason",
            "too_frequent",
            "--why",
            "fires more than it helps",
            "--json",
        ],
        capsys,
    )
    assert code == 0, err
    data = json.loads(out.strip().splitlines()[-1])["data"]
    assert data["family"] == FAMILY
    assert data["disposition"] == "quiet"
    assert data["reason"] == "too_frequent"
    assert data["why"] == "too_frequent: fires more than it helps"
    assert data["ref"] == FAMILY_REF
    assert _disposition(vault)["disposition"] == "quiet"


def test_the_cli_also_takes_the_full_family_reference(vault: Path, capsys) -> None:
    code, _out, err = _cli(
        ["review", "off", FAMILY_REF, "--reason", "intentional", "--json"], capsys
    )
    assert code == 0, err
    assert _disposition(vault)["disposition"] == "off"


def test_the_cli_refuses_to_quiet_a_family_with_no_reason(vault: Path, capsys) -> None:
    """Refused at the PARSER, so `--help` shows the requirement and the words.

    The store refuses it too (`INVALID_REVIEW_REASON`) and that stays the
    backstop for every other caller; what changed is that the CLI no longer
    accepts an argument it is going to reject a layer down.
    """
    code, _out, err = _cli(["review", "quiet", FAMILY, "--why", "just stop"], capsys)
    assert code != 0
    assert "--reason" in err
    assert _disposition(vault) is None


def test_the_cli_refuses_unspecified_as_a_reason_for_quiet(vault: Path, capsys) -> None:
    """`unspecified` is a valid code for an ITEM decision and never for a family.

    Offering it in `--choices` and then failing on it is the specific shape this
    fixes: argparse accepted the word, the store rejected it, and the user was
    told about a vocabulary the parser had just implied was fine.
    """
    code, _out, err = _cli(
        ["review", "quiet", FAMILY, "--reason", "unspecified"], capsys
    )
    assert code != 0
    assert "unspecified" in err
    assert _disposition(vault) is None
    # Still valid on an item decision, which is why it stays in `REASON_CODES`.
    assert "unspecified" in review_state.REASON_CODES


def test_the_cli_rejects_a_word_outside_the_vocabulary(vault: Path, capsys) -> None:
    code, _out, err = _cli(["review", "quiet", FAMILY, "--reason", "annoying"], capsys)
    assert code != 0
    assert "annoying" in err
    assert _disposition(vault) is None


def test_the_cli_clears_a_family_without_a_reason(vault: Path, capsys) -> None:
    assert _cli(["review", "quiet", FAMILY, "--reason", "handled"], capsys)[0] == 0
    code, out, err = _cli(["review", "normal", FAMILY], capsys)
    assert code == 0, err
    assert "normal" in out
    assert _disposition(vault) is None


def test_the_cli_composes_the_reason_token_onto_a_dismissal(
    vault: Path, capsys
) -> None:
    overdue_prediction(vault)
    scratch_page(vault)
    item = [
        entry
        for entry in commands.op_attention(vault, limit=0)["items"]
        if "nag-backlog" in entry["path"]
    ][0]

    code, _out, err = _cli(
        [
            "review",
            "dismiss",
            item["ref"],
            "--reason",
            "handled",
            "--why",
            "closed in the ticket",
            "--json",
        ],
        capsys,
    )
    assert code == 0, err

    payload = review_state.ReviewStateStore(vault).load()
    record = payload["records"][f"{item['item_id']}:{item['fingerprint']}"]
    assert record["reason"] == "handled"
    assert record["why"] == "handled: closed in the ticket"
    assert record["origin"] == "manual"


def test_a_bare_reason_needs_no_free_text(vault: Path, capsys) -> None:
    code, _out, err = _cli(
        ["review", "quiet", FAMILY, "--reason", "false_positive", "--json"], capsys
    )
    assert code == 0, err
    assert _disposition(vault)["reason"] == "false_positive"


# ==========================================================================
# what the bootstrap payload teaches
# ==========================================================================


def _post_write(vault: Path) -> dict:
    payload = commands.op_bootstrap(vault, profile="compact")
    return payload["authoring_contract"]["post_write"]


def test_compact_bootstrap_names_every_reason_code(vault: Path) -> None:
    """Checked against the vocabulary rather than a retyped list.

    A code added to `REASON_CODES` and not to the payload is a code no agent
    ever composes, which is the whole failure this coupling catches.
    """
    text = _post_write(vault)["review_reason"]
    missing = [
        code
        for code in review_state.REASON_CODES
        if code != review_state.DEFAULT_REASON and f"{code}:" not in text
    ]
    assert missing == [], f"bootstrap does not name {missing}"
    assert review_state.DEFAULT_REASON in text


def test_compact_bootstrap_names_the_family_route_and_its_three_actions(
    vault: Path,
) -> None:
    text = _post_write(vault)["family_disposition"]
    assert review_state.FAMILY_PREFIX in text
    for action in review_state.DISPOSITIONS:
        assert f"'{action}'" in text, action
    # The wrong answer this guidance exists to displace.
    assert "prominence" in text


def test_compact_bootstrap_says_a_quiet_family_is_not_a_clean_one(vault: Path) -> None:
    text = _post_write(vault)["family_disposition_reading"]
    assert "silent, not clean" in text
    assert "dispositions" in text


# ==========================================================================
# the count is composed by the SHARED composer, and resolves refs once
# ==========================================================================


def test_the_count_follows_the_shared_composer_rather_than_a_local_copy(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stub the one composer; the dispositions count must move with it.

    The S1 blocker was a second hand-rolled derivation of the component
    fingerprint drifting from the one `apply_for_item` records against, which
    under-counts silently. A local copy passes every test written against real
    fingerprints, because both derivations agree until one of them changes — so
    the only test that can catch it stubs the canonical implementation and
    asserts the caller returns the stub.
    """
    doubly_flagged(vault, "nag-shared")
    scratch_page(vault)

    monkeypatch.setattr(
        review_state,
        "component_fingerprints",
        lambda _vault, _item, **_kwargs: [("prediction_window", "stubbed-value")],
    )
    keys = commands._review_keys_by_family(vault)

    assert list(keys) == ["prediction_window"]
    assert all(key.endswith(":stubbed-value") for key in keys["prediction_window"]), keys


def test_the_counts_ref_lookups_do_not_scale_with_the_vault(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One `refs_for_paths` for the whole view, however many items it counts.

    `refs_for_paths` opens a database connection. Asking it per item made the
    cost of "which families are quiet" scale with the vault rather than with the
    number of families, which is the wrong axis entirely for a view that returns
    four rows.

    Asserted as INVARIANCE across vault size rather than as an absolute count,
    because the attention scan underneath does its own single resolution and
    this test is not about that one. A per-item lookup shows up here as a count
    that tracks the item count; a hoisted one does not move at all.
    """

    def lookups(count: int) -> tuple[int, int]:
        for index in range(count):
            overdue_prediction(vault, f"nag-many-{index}")
        find_module.clear_cache()
        calls: list[int] = []
        real = review_state.refs_for_paths

        def counting(vault_root, paths):
            calls.append(len(paths))
            return real(vault_root, paths)

        monkeypatch.setattr(review_state, "refs_for_paths", counting)
        keys = commands._review_keys_by_family(vault)
        monkeypatch.setattr(review_state, "refs_for_paths", real)
        return len(calls), len(keys.get(FAMILY, []))

    scratch_page(vault)
    few_calls, few_items = lookups(2)
    many_calls, many_items = lookups(12)

    assert (few_items, many_items) == (2, 12), (few_items, many_items)
    assert few_calls == many_calls, (
        f"{few_calls} ref lookups for {few_items} items but {many_calls} for "
        f"{many_items}: the view is resolving refs per item"
    )
