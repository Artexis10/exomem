"""Closed reason codes and origin tags on every review-state record.

Two of the three capture primitives the paired metrics need. The reason code
rides the existing free-text `why` as a leading colon-terminated token, so no
tool input schema moves for it; `origin` says whether a person decided or the
runtime did, which is the whole of the manual-maintenance metric.

The first test is the GAP PROOF: today a dismissal stores neither.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from _nag_governance_helpers import overdue_prediction, scratch_page

from exomem import commands, review_state

FAMILY_REF = "exomem://review/family/prediction_window"


def _dismiss(vault: Path, why: str) -> dict:
    overdue_prediction(vault)
    scratch_page(vault)
    item = [
        i
        for i in commands.op_attention(vault, limit=0)["items"]
        if "nag-backlog" in i["path"]
    ][0]
    return commands.op_triage_memory(vault, ref=item["ref"], action="dismiss", why=why)


def _record(vault: Path, result: dict) -> dict:
    payload = review_state.ReviewStateStore(vault).load()
    key = f"{result['item_id']}:{result['fingerprint']}"
    return payload["records"][key]


# ==========================================================================
# THE GAP PROOF
# ==========================================================================


def test_a_dismissal_records_neither_a_reason_nor_an_origin_today(vault: Path) -> None:
    """RED-FIRST. The two fields the metrics are computed from are simply absent."""
    result = _dismiss(vault, "intentional: this page is a deliberate hub")
    record = _record(vault, result)

    missing = [field for field in ("reason", "origin") if field not in record]
    assert missing == [], f"the review record carries no {', '.join(missing)}: {record}"


# ==========================================================================
# reason-token parsing
# ==========================================================================


def test_a_coded_dismissal_stores_both_the_code_and_the_text(vault: Path) -> None:
    why = "intentional: this page is a deliberate hub"
    record = _record(vault, _dismiss(vault, why))
    assert record["reason"] == "intentional"
    assert record["why"] == why


def test_free_text_without_a_code_records_unspecified(vault: Path) -> None:
    record = _record(vault, _dismiss(vault, "looks fine to me"))
    assert record["reason"] == "unspecified"
    assert record["why"] == "looks fine to me"


def test_an_unknown_leading_token_records_unspecified_verbatim(vault: Path) -> None:
    why = "whatever: not a vocabulary word"
    record = _record(vault, _dismiss(vault, why))
    assert record["reason"] == "unspecified"
    assert record["why"] == why


@pytest.mark.parametrize(
    ("why", "reason"),
    [
        ("intentional: deliberate", "intentional"),
        ("false_positive: the detector is wrong", "false_positive"),
        ("handled: dealt with in the ticket", "handled"),
        ("deferred: not now", "deferred"),
        ("too_frequent: fires more than it helps", "too_frequent"),
        ("unspecified: no view", "unspecified"),
        ("  handled  :  padded  ", "handled"),
        ("handled", "unspecified"),
        ("a note: with a colon: and another", "unspecified"),
        ("", "unspecified"),
        (None, "unspecified"),
    ],
)
def test_the_parser_is_the_one_place_that_knows_the_vocabulary(
    why: str | None, reason: str
) -> None:
    parsed, verbatim = review_state.parse_reason(why)
    assert parsed == reason
    assert verbatim == (str(why).strip() if why else None)


def test_a_colon_inside_the_free_text_is_not_a_code(vault: Path) -> None:
    why = "handled: see the incident review: 2026-08-01"
    record = _record(vault, _dismiss(vault, why))
    assert record["reason"] == "handled"
    assert record["why"] == why


def test_the_vocabulary_is_closed(vault: Path) -> None:
    assert review_state.REASON_CODES == (
        "intentional",
        "false_positive",
        "handled",
        "deferred",
        "too_frequent",
        "unspecified",
    )


# ==========================================================================
# quiet/off require a code
# ==========================================================================


@pytest.mark.parametrize("action", ["quiet", "off"])
def test_a_disposition_without_a_code_is_refused(vault: Path, action: str) -> None:
    store = review_state.ReviewStateStore(vault)
    with pytest.raises(ValueError, match="INVALID_REVIEW_REASON"):
        commands.op_triage_memory(vault, ref=FAMILY_REF, action=action, why="just stop")
    assert review_state.disposition_for(
        "prediction_window", payload=store.load()
    ) == "normal"


def test_normal_needs_no_code(vault: Path) -> None:
    commands.op_triage_memory(
        vault, ref=FAMILY_REF, action="quiet", why="too_frequent: enough"
    )
    result = commands.op_triage_memory(vault, ref=FAMILY_REF, action="normal")
    assert result["disposition"] == "normal"


# ==========================================================================
# origin
# ==========================================================================


def test_a_triage_decision_is_manual(vault: Path) -> None:
    record = _record(vault, _dismiss(vault, "handled: dealt with elsewhere"))
    assert record["origin"] == "manual"


def test_a_family_disposition_set_through_triage_is_manual(vault: Path) -> None:
    commands.op_triage_memory(
        vault, ref=FAMILY_REF, action="quiet", why="too_frequent: enough"
    )
    payload = review_state.ReviewStateStore(vault).load()
    assert payload["dispositions"]["prediction_window"]["origin"] == "manual"


def test_what_the_runtime_writes_itself_is_automatic(vault: Path) -> None:
    """Compaction is the runtime deciding, so what it writes says so.

    The standing dismissal it preserves keeps `manual` — it was a person's
    decision and compaction did not change it. What compaction itself authors is
    the drop report, and that carries `automatic`.

    The lapsed snooze is here so compaction has something to DROP: a walk that
    removes nothing no longer stamps `stats`, because a store whose thresholds
    are permanently tripped would otherwise rewrite itself on every write for a
    timestamp saying nothing happened.
    """
    result = _dismiss(vault, "handled: dealt with elsewhere")
    store = review_state.ReviewStateStore(vault)
    store.apply(
        "d" * 24,
        "e" * 24,
        action="snooze",
        until=(dt.date.today() - dt.timedelta(days=200)).isoformat(),
        why="deferred: later",
    )
    report = store.compact(force=True)

    payload = store.load()
    assert report["dropped"]["records"] == 1
    assert _record(vault, result)["origin"] == "manual"
    assert payload["stats"]["compaction"]["origin"] == "automatic"
    assert report["origin"] == "automatic"


def test_ledger_entries_are_automatic(vault: Path) -> None:
    """Nobody decided to surface a signal; the runtime did."""
    overdue_prediction(vault)
    scratch_page(vault)
    commands.op_attention(vault, limit=0)

    payload = review_state.ReviewStateStore(vault).load()
    assert payload["surfaced"], payload
    assert {row["origin"] for row in payload["surfaced"].values()} == {"automatic"}


def test_migrated_records_carry_manual(vault: Path) -> None:
    """Schema 1 could only be written by the triage surface, so that is the truth."""
    result = _dismiss(vault, "handled: dealt with elsewhere")
    path = review_state.state_path(vault)
    import json

    payload = json.loads(path.read_text(encoding="utf-8"))
    legacy_records = {
        key: {
            field: value
            for field, value in record.items()
            if field not in {"reason", "origin"}
        }
        for key, record in payload["records"].items()
    }
    path.write_text(
        json.dumps({"version": 1, "records": legacy_records}, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )

    migrated = review_state.ReviewStateStore(vault).load()
    key = f"{result['item_id']}:{result['fingerprint']}"
    assert migrated["version"] == review_state.SCHEMA_VERSION
    assert migrated["records"][key]["origin"] == "manual"
    # The code is parsed from the `why` that was already stored, so a coded
    # dismissal made before the vocabulary existed reads the same afterwards.
    assert migrated["records"][key]["reason"] == "handled"


def test_the_manual_count_in_a_window_is_computable_from_the_store(vault: Path) -> None:
    _dismiss(vault, "handled: dealt with elsewhere")
    payload = review_state.ReviewStateStore(vault).load()
    now = dt.datetime.now(dt.UTC)
    counted = review_state.manual_records_since(
        payload, since=now - dt.timedelta(days=1)
    )
    assert counted >= 1
    assert review_state.manual_records_since(
        payload, since=now + dt.timedelta(days=1)
    ) == 0
