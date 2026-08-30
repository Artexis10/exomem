"""Adaptation is deterministic, consent-shaped, and happens at most once.

Nothing here quiets anything. Three manual dismissals in one family earn the
family's next surfacing **one offer** to quiet it, and the offer changes nothing
by itself: the disposition moves only when a person decides.

Three properties are easy to lose and each has its own pin:

- **Records, not the live index.** The count is taken from the durable
  review-state records, so it never decreases when the dismissed items later
  vanish from the vault. Counting the surface would let a tidy-up silently
  disarm an offer the user had already earned.
- **Manual only.** An `automatic`-origin decision is the runtime deciding, and
  the runtime's own decisions must never accumulate into a nudge.
- **No usage signals at all.** Reads, queries and engagement are banned as
  inputs — the ban is on the INPUT, not merely on today's implementation, so
  there is a structural pin as well as a behavioural one.
"""

from __future__ import annotations

import ast
import inspect
import json
import textwrap
from pathlib import Path

import pytest
from _nag_governance_helpers import overdue_prediction, scratch_page

from exomem import commands, review_state

FAMILY = "prediction_window"
FAMILY_REF = f"exomem://review/family/{FAMILY}"
WHY = "handled: dealt with outside the queue"
QUIET_WHY = "too_frequent: fires more than it helps in this vault"


def _payload(vault: Path) -> dict:
    return review_state.ReviewStateStore(vault).load()


def _surface(vault: Path) -> dict:
    """One served review surfacing — the moment an offer may be carried."""
    return commands.op_attention(vault, limit=0)


def _offered_families(report: dict) -> list[str]:
    return [row["family"] for row in report.get("quiet_offers", [])]


def _open_items(vault: Path, fragment: str) -> list[dict]:
    """A LOOKUP, not a surfacing.

    `record_surfacing=False` is the seam attention already draws for callers
    that list in order to look something up. Using it here keeps the tests from
    spending an offer on a helper: an offer is made once, so a helper that
    quietly surfaced would decide the outcome of every assertion after it.
    """
    from exomem import attention as attention_module

    report = attention_module.attention(vault, limit=0, record_surfacing=False)
    return [
        item.as_dict()
        for item in report.items
        if fragment in item.path and item.state == "open"
    ]


def _dismiss(vault: Path, fragment: str) -> None:
    items = _open_items(vault, fragment)
    assert items, f"no open item matching {fragment!r} to dismiss"
    commands.op_triage_memory(vault, ref=items[0]["ref"], action="dismiss", why=WHY)


def _slate(vault: Path) -> dict | None:
    return (_payload(vault).get("dispositions") or {}).get(FAMILY)


@pytest.fixture
def three_dismissals(vault: Path) -> Path:
    """A vault where the user has put down three items of one family by hand.

    Six pages, not three: an offer is made on the family's next SURFACING, so a
    family with nothing left to show has no surfacing to carry one. `nag-six` is
    never dismissed by any test here, which is what keeps "no second offer"
    meaning "suppressed" rather than "there was nothing to suppress".
    """
    scratch_page(vault)
    for slug in ("nag-one", "nag-two", "nag-three", "nag-four", "nag-five", "nag-six"):
        overdue_prediction(vault, slug)
    for slug in ("nag-one", "nag-two", "nag-three"):
        _dismiss(vault, slug)
    return vault


# ------------------------------------------------------------------ 3.1 the offer


def test_the_third_manual_dismissal_arms_exactly_one_offer(three_dismissals: Path) -> None:
    vault = three_dismissals

    assert _offered_families(_surface(vault)) == [FAMILY]
    # And never again: the offer is made once, not once per surfacing.
    assert _offered_families(_surface(vault)) == []
    assert _offered_families(_surface(vault)) == []


def test_two_dismissals_earn_nothing(vault: Path) -> None:
    scratch_page(vault)
    for slug in ("nag-one", "nag-two", "nag-three"):
        overdue_prediction(vault, slug)
    for slug in ("nag-one", "nag-two"):
        _dismiss(vault, slug)

    assert _offered_families(_surface(vault)) == []
    assert _slate(vault) is None


def test_an_automatic_origin_decision_never_counts(vault: Path) -> None:
    """The runtime's own decisions must not accumulate into a nudge."""
    scratch_page(vault)
    for slug in ("nag-one", "nag-two", "nag-three"):
        overdue_prediction(vault, slug)
    for slug in ("nag-one", "nag-two"):
        _dismiss(vault, slug)

    third = _open_items(vault, "nag-three")[0]
    review_state.ReviewStateStore(vault).apply(
        third["item_id"],
        third["fingerprint"],
        action="dismiss",
        why=WHY,
        origin=review_state.AUTOMATIC,
        family=FAMILY,
    )

    assert review_state.manual_dismissal_events(_payload(vault), FAMILY) == 2
    assert _offered_families(_surface(vault)) == []


def test_the_count_is_taken_from_the_records_not_the_live_index(
    three_dismissals: Path,
) -> None:
    """Deleting the dismissed items must not disarm an offer already earned."""
    vault = three_dismissals
    for slug in ("nag-one", "nag-two", "nag-three"):
        (vault / "Knowledge Base" / "Notes" / "Insights" / f"{slug}.md").unlink()
    from exomem import find as find_module

    find_module.clear_cache()

    assert review_state.manual_dismissal_events(_payload(vault), FAMILY) == 3
    assert _offered_families(_surface(vault)) == [FAMILY]


def test_the_offer_is_recorded_durably_against_the_family(three_dismissals: Path) -> None:
    vault = three_dismissals
    _surface(vault)

    slate = _slate(vault)
    assert slate is not None
    assert slate["quiet_offered_at"]
    assert slate["disposition"] == "normal", (
        "the offer is not a disposition; it must not silence anything"
    )
    # A fresh store over the same bytes: nothing is cached in the process.
    assert review_state.ReviewStateStore(vault).load()["dispositions"][FAMILY][
        "quiet_offered_at"
    ] == slate["quiet_offered_at"]


def test_a_decline_without_a_reset_never_re_offers(three_dismissals: Path) -> None:
    vault = three_dismissals
    assert _offered_families(_surface(vault)) == [FAMILY]

    # The user says nothing and keeps dismissing.
    for slug in ("nag-four", "nag-five"):
        _dismiss(vault, slug)

    assert review_state.manual_dismissal_events(_payload(vault), FAMILY) == 5
    report = _surface(vault)
    assert _offered_families(report) == []
    assert any(FAMILY in item["categories"] for item in report["items"]), (
        "the family must still be surfacing, or the silence proves nothing"
    )


def test_quieting_the_family_keeps_the_offer_marker(three_dismissals: Path) -> None:
    """Only an explicit reset clears it — accepting the offer must not re-arm it."""
    vault = three_dismissals
    _surface(vault)
    offered_at = _slate(vault)["quiet_offered_at"]

    commands.op_triage_memory(vault, ref=FAMILY_REF, action="quiet", why=QUIET_WHY)

    slate = _slate(vault)
    assert slate["disposition"] == "quiet"
    assert slate["quiet_offered_at"] == offered_at


def test_an_explicit_reset_clears_the_slate_and_one_new_offer_may_appear(
    three_dismissals: Path,
) -> None:
    vault = three_dismissals
    _surface(vault)
    commands.op_triage_memory(vault, ref=FAMILY_REF, action="quiet", why=QUIET_WHY)
    commands.op_triage_memory(vault, ref=FAMILY_REF, action="normal")

    assert _slate(vault) is None, "a reset clears the family's slate"

    for slug in ("nag-four", "nag-five"):
        _dismiss(vault, slug)

    assert _offered_families(_surface(vault)) == [FAMILY]
    assert _offered_families(_surface(vault)) == []


def _without_docstrings(fn) -> str:
    """The function's CODE. Prose about a banned input is not a banned input.

    Scanning raw source made the ban unstatable: the docstring that says usage
    logs are never inputs would itself trip a scan for the word `usage`.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            ):
                node.body.pop(0)
    return ast.unparse(tree)


def test_the_derivation_reads_review_state_records_and_nothing_else() -> None:
    """The spec bans the INPUT, not merely the current implementation.

    A behavioural test cannot prove the absence of an input that nothing
    currently supplies, so this reads the derivation's own source. Adding a
    usage, query, read-count or engagement signal to it has to break this
    line first.
    """
    source = "\n".join(
        _without_docstrings(fn)
        for fn in (
            review_state.manual_dismissal_events,
            review_state.quiet_offered_at,
            review_state._quiet_offer_due,
            review_state.ReviewStateStore.arm_quiet_offer,
        )
    ).lower()

    for banned in (
        "query_log",
        "access_log",
        "read_count",
        "engagement",
        "usage",
        "find_module",
        "first_surfaced",
        "surfaced",
    ):
        assert banned not in source, f"the derivation must not read {banned}"


def test_repeated_surfacing_without_triage_adapts_nothing(vault: Path) -> None:
    """"Nothing adapts on its own" — surfaced and ignored is not a decision."""
    scratch_page(vault)
    for slug in ("nag-one", "nag-two", "nag-three"):
        overdue_prediction(vault, slug)

    for _ in range(6):
        assert _offered_families(_surface(vault)) == []

    assert _slate(vault) is None
    assert review_state.disposition_for(FAMILY, payload=_payload(vault)) == "normal"


# ---------------------------------------------- 3.2 the offer changes nothing


def test_the_offer_changes_nothing_by_itself(three_dismissals: Path) -> None:
    vault = three_dismissals
    from exomem import envelope

    before_envelope = envelope.resolved()
    before_open = {item["path"] for item in _open_items(vault, "nag-")}

    report = _surface(vault)
    assert _offered_families(report) == [FAMILY], "the offer must actually be made"

    assert review_state.disposition_for(FAMILY, payload=_payload(vault)) == "normal"
    assert {item["path"] for item in _open_items(vault, "nag-")} == before_open
    assert envelope.resolved() == before_envelope
    # The family is still on the daily surface: an offer is a question, not an
    # answer, and nothing was quieted while it stood.
    assert any(FAMILY in item["categories"] for item in report["items"])


# -------------------------------------------------- 3.3 the schema migration


def _write_store(vault: Path, payload: dict) -> Path:
    path = review_state.state_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def test_the_schema_version_moved_once(vault: Path) -> None:
    assert review_state.SCHEMA_VERSION == 3
    assert review_state._READABLE_SCHEMA_VERSIONS == frozenset({1, 2, 3})


def test_a_previous_schema_store_is_migrated_on_load_and_rewritten_on_write(
    vault: Path,
) -> None:
    overdue_prediction(vault, "nag-one")
    scratch_page(vault)
    path = _write_store(
        vault,
        {
            "version": 2,
            "records": {
                "abc123:def456": {
                    "item_id": "abc123",
                    "fingerprint": "def456",
                    "action": "dismiss",
                    "until": None,
                    "why": "handled: earlier",
                    "reason": "handled",
                    "origin": "manual",
                    "updated_at": "2026-08-01T00:00:00Z",
                }
            },
            "dispositions": {},
            "surfaced": {},
            "stats": {},
        },
    )

    store = review_state.ReviewStateStore(vault)
    loaded = store.load()

    assert loaded["version"] == 3
    assert loaded["records"]["abc123:def456"]["action"] == "dismiss"
    # A record written before family attribution existed carries none, and is
    # therefore never counted: the store cannot invent an attribution it never
    # had, and guessing one would fabricate an event the user did not create.
    assert "family" not in loaded["records"]["abc123:def456"]
    assert review_state.manual_dismissal_events(loaded, FAMILY) == 0
    assert json.loads(path.read_text("utf-8"))["version"] == 2, "load never writes"

    _dismiss(vault, "nag-one")

    rewritten = json.loads(path.read_text("utf-8"))
    assert rewritten["version"] == 3
    assert rewritten["records"]["abc123:def456"]["action"] == "dismiss"


def test_a_newer_schema_is_refused_by_this_runtime_with_a_named_error(vault: Path) -> None:
    _write_store(
        vault,
        {
            "version": review_state.SCHEMA_VERSION + 1,
            "records": {},
            "dispositions": {},
            "surfaced": {},
            "stats": {},
        },
    )

    with pytest.raises(ValueError) as error:
        review_state.ReviewStateStore(vault).load()

    assert review_state.error_code(error.value) == "REVIEW_STATE_INVALID"
    assert "unsupported review state schema" in str(error.value)


def test_a_dismissal_record_carries_the_family_that_produced_the_signal(
    vault: Path,
) -> None:
    overdue_prediction(vault, "nag-one")
    scratch_page(vault)
    _dismiss(vault, "nag-one")

    families = {
        record.get("family")
        for record in _payload(vault)["records"].values()
        if record.get("action") == "dismiss"
    }
    assert FAMILY in families


def test_the_family_slate_holds_an_offer_while_the_disposition_is_normal(
    three_dismissals: Path,
) -> None:
    """The slot the previous schema had nowhere to put.

    `normal` is represented by the ABSENCE of a disposition record, so before
    this migration there was no durable place to record that an offer had been
    made to a family the user had not quieted — and an offer that is not durable
    is an offer that repeats.
    """
    vault = three_dismissals
    _surface(vault)

    slate = _slate(vault)
    assert slate["disposition"] == "normal"
    assert slate["quiet_offered_at"]
    # Slate-only rows are not dispositions: they must not reach the filters or
    # the view that lists what a user has quieted.
    assert review_state.disposition_map(_payload(vault)) == {}
    view = commands.op_review_memory(vault, mode="dispositions")
    assert [row["family"] for row in view["dispositions"]] == []
