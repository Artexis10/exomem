"""The session-close suite's own logic: the refusal to score without a key, and the snapshot diff the scores rest on."""

from __future__ import annotations

from pathlib import Path

import pytest
from import_guidance import checks, session_close, session_score
from import_guidance.session_close import Case

SEED = session_close.suite_dir() / "seeds" / "c01"
PERSON = "Entities/People/Tavi Brennock.md"
OTHER = "Entities/People/Corin Haldane.md"
ROUTINE = "Notes/Patterns/sunday-meal-prep-routine.md"
RECAP = "Sources/Episodes/2026-11-03-recap.md"
RECAP_ID = "11111111-2222-5333-8444-555555555555"

KEY = {
    "reference": "Tavi's meeting-free mornings belong on her person page.",
    "must_change": [PERSON],
    "must_not_change": [OTHER],
    "recap": "required",
    "new_pages": "none",
}


def case(key: dict | None) -> Case:
    return Case("c-test", "t", SEED, "balanced", ("hi",), key, Path("c-test.json"))


def page(text: str, mtime_ns: int) -> dict:
    return {"text": text, "mtime_ns": mtime_ns}


def test_scoring_refuses_a_case_without_a_key_and_a_key_that_names_an_unseeded_page() -> None:
    with pytest.raises(SystemExit, match="no answer_key"):
        session_score.refuse_unkeyed([case(None)])
    with pytest.raises(SystemExit, match="not in the seed"):
        session_score.refuse_unkeyed([case({**KEY, "must_change": ["Entities/People/Nobody.md"]})])
    session_score.refuse_unkeyed([case(KEY)])


def test_every_shipped_case_loads_and_the_variants_change_only_engagement() -> None:
    cases = {c.id: c for c in session_close.load_cases("all")}
    assert cases["c08-regression-maximal"].turns == cases["c07-regression"].turns
    assert cases["c08-regression-maximal"].seed == cases["c07-regression"].seed
    assert cases["c08-regression-maximal"].engagement == "maximal"
    assert cases["c06-policy-light"].turns == cases["c01-person"].turns
    assert cases["c06-policy-light"].seed == cases["c01-person"].seed
    assert cases["c06-policy-light"].engagement == "light"
    assert all(c.turns[-1].startswith("That's all for today") for c in cases.values())


def routine(sources: str) -> str:
    return f"---\ntype: pattern\nsources: {sources}\n---\n\n# Sunday meal-prep routine\n\nOrder groceries on Friday.\n"


def test_diff_reports_changed_pages_and_which_recaps_a_compiled_page_cites_and_the_write_order() -> None:
    before = {
        PERSON: page("---\ntype: entity\n---\n\n# Tavi Brennock\n\nPrefers short updates.\n", 1),
        OTHER: page("---\ntype: entity\n---\n\n# Corin Haldane\n", 1),
        ROUTINE: page(routine("[]"), 1),
    }
    recap = f"---\ntype: source\nexomem_id: {RECAP_ID}\n---\n\n## Decided\n- Order on Friday.\n"
    after = {
        PERSON: page(before[PERSON]["text"] + "No meetings before 10:00.\n", 5),
        OTHER: before[OTHER],
        ROUTINE: page(routine(f"[exomem://memory/{RECAP_ID}]"), 6),
        RECAP: page(recap, 9),
    }
    seen = session_close.diff_vault(before, after)
    assert list(seen["changed"]) == [PERSON, ROUTINE]
    assert "+No meetings before 10:00." in seen["changed"][PERSON]
    assert list(seen["recaps"]) == [RECAP]
    # An entity page is not of a compiled type, and a ref names the recap as a wikilink to its path does.
    assert seen["pages"] == {PERSON: {"compiled": False, "cites": []}, ROUTINE: {"compiled": True, "cites": [RECAP]}}
    after[ROUTINE] = page(routine("['[[Sources/Episodes/2026-11-03-recap]]']"), 6)
    assert session_close.diff_vault(before, after)["pages"][ROUTINE]["cites"] == [RECAP]
    assert seen["order"] == "before"
    # The same edit after the recap is reported as after, not as before.
    after[PERSON] = page(after[PERSON]["text"], 12)
    after[ROUTINE] = page(after[ROUTINE]["text"], 12)
    assert session_close.diff_vault(before, after)["order"] == "after"


def observed(*, pages: dict, recaps: bool) -> dict:
    return {
        "changed": {p: "diff" for p in pages},
        "removed": [],
        "created": {},
        "recaps": {RECAP: {"text": ""}} if recaps else {},
        "pages": pages,
        "order": "before",
    }


def verdicts_of(key: dict, seen: dict) -> dict[str, str]:
    return {o.check: o.verdict for o in session_score.check_row(key, {"observed": seen})}


def test_checks_fail_a_missing_update_and_a_write_the_key_forbids() -> None:
    nothing = {"changed": {}, "removed": [], "created": {}, "recaps": {}, "pages": {}, "order": "no_change"}
    verdicts = verdicts_of(KEY, nothing)
    assert verdicts["must_change"] == checks.FAIL
    assert verdicts["recap"] == checks.FAIL
    assert verdicts["must_not_change"] == checks.PASS
    wrote = observed(pages={PERSON: {"compiled": False, "cites": []}, OTHER: {"compiled": False, "cites": []}}, recaps=True)
    wrote["created"] = {"Notes/extra.md": "x"}
    assert verdicts_of(KEY, wrote) == {
        "must_change": checks.PASS,
        "must_not_change": checks.FAIL,
        "recap": checks.PASS,
        "recap_cited": checks.NOT_APPLICABLE,
        "new_pages": checks.FAIL,
    }


def test_recap_cited_passes_fails_and_is_not_applicable_by_what_the_run_changed() -> None:
    """A run that cited nothing must not read as a pass, and a run with nothing to cite must not read as one either."""
    cited = {ROUTINE: {"compiled": True, "cites": [RECAP]}}
    uncited = {ROUTINE: {"compiled": True, "cites": []}}
    entity = {PERSON: {"compiled": False, "cites": []}}
    assert verdicts_of(KEY, observed(pages=cited, recaps=True))["recap_cited"] == checks.PASS
    assert verdicts_of(KEY, observed(pages=cited | uncited | {OTHER: {"compiled": True, "cites": []}}, recaps=True))["recap_cited"] == checks.FAIL
    assert verdicts_of(KEY, observed(pages=uncited, recaps=True))["recap_cited"] == checks.FAIL
    assert verdicts_of(KEY, observed(pages=entity, recaps=True))["recap_cited"] == checks.NOT_APPLICABLE
    assert verdicts_of(KEY, observed(pages=uncited, recaps=False))["recap_cited"] == checks.NOT_APPLICABLE
    # Not applicable leaves a run's verdict to the other checks.
    outcomes = [checks.Outcome("recap_cited", checks.NOT_APPLICABLE, ""), checks.Outcome("recap", checks.PASS, "")]
    assert checks.verdict(None, outcomes, []) == checks.PASS
    assert checks.verdict(None, [checks.Outcome("recap_cited", checks.NOT_APPLICABLE, "")], []) == checks.UNKNOWN


def test_judge_view_shows_turns_reference_and_diffs_but_not_the_arm_or_the_recap() -> None:
    observed = {
        "changed": {PERSON: "+No meetings before 10:00.\n"},
        "removed": [],
        "created": {RECAP: "A recap by the candidate arm."},
        "recaps": {RECAP: {"text": "A recap by the candidate arm."}},
        "order": "before",
    }
    row = {"arm": "candidate", "model": "sonnet", "observed": observed, "compile_blocks": [{"outcome": "x"}]}
    view = session_score.judge_view(case(KEY), KEY, row)
    assert "+No meetings before 10:00." in view
    assert KEY["reference"] in view
    assert "1. hi" in view
    assert not {"candidate", "sonnet", "recap", "compile"} & set(view.lower().replace(".", " ").split())
