"""The session-close suite's own logic: the refusal to score without a key, and the snapshot diff the scores rest on."""

from __future__ import annotations

from pathlib import Path

import pytest
from import_guidance import checks, session_close, session_score
from import_guidance.session_close import Case

SEED = session_close.suite_dir() / "seeds" / "c01"
PERSON = "Entities/People/Tavi Brennock.md"
OTHER = "Entities/People/Corin Haldane.md"
RECAP = "Sources/Episodes/2026-11-03-recap.md"

KEY = {
    "reference": "Tavi's meeting-free mornings belong on her person page.",
    "must_change": [PERSON],
    "must_not_change": [OTHER],
    "recap": "required",
    "recap_must_link": [PERSON],
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


def test_diff_reports_changed_pages_recap_links_and_write_order() -> None:
    before = {
        PERSON: page("---\ntype: entity\n---\n\n# Tavi Brennock\n\nPrefers short updates.\n", 1),
        OTHER: page("---\ntype: entity\n---\n\n# Corin Haldane\n", 1),
    }
    recap = (
        "---\ntype: source\n---\n\n## Decided\n"
        "- Plan around [[Tavi Brennock]]'s mornings.\n"
        "- Ask [[Entities/People/Corin Haldane]] about the rota; see [[Nobody Here]].\n"
    )
    after = {
        PERSON: page(before[PERSON]["text"] + "No meetings before 10:00.\n", 5),
        OTHER: before[OTHER],
        RECAP: page(recap, 9),
    }
    seen = session_close.diff_vault(before, after)
    assert list(seen["changed"]) == [PERSON]
    assert "+No meetings before 10:00." in seen["changed"][PERSON]
    assert seen["recaps"][RECAP]["links"] == {
        "seeded": [OTHER, PERSON],
        "created": [],
        "dangling": ["Nobody Here"],
        "ambiguous": [],
    }
    assert seen["order"] == "before"
    # The same edit after the recap is reported as after, not as before.
    after[PERSON] = page(after[PERSON]["text"], 12)
    assert session_close.diff_vault(before, after)["order"] == "after"


def test_checks_fail_a_missing_update_and_a_write_the_key_forbids() -> None:
    nothing = {"changed": {}, "removed": [], "created": {}, "recaps": {}, "order": "no_change"}
    verdicts = {o.check: o.verdict for o in session_score.check_row(KEY, {"observed": nothing})}
    assert verdicts["must_change"] == checks.FAIL
    assert verdicts["recap"] == checks.FAIL
    assert verdicts["must_not_change"] == checks.PASS
    wrote = {
        "changed": {PERSON: "diff", OTHER: "diff"},
        "removed": [],
        "created": {"Notes/extra.md": "x"},
        "recaps": {RECAP: {"text": "", "links": {"seeded": [], "created": [], "dangling": [], "ambiguous": []}}},
        "order": "before",
    }
    verdicts = {o.check: o.verdict for o in session_score.check_row(KEY, {"observed": wrote})}
    assert verdicts == {
        "must_change": checks.PASS,
        "must_not_change": checks.FAIL,
        "recap": checks.PASS,
        "recap_must_link": checks.FAIL,
        "new_pages": checks.FAIL,
    }


def test_an_optional_recap_left_unwritten_owes_no_links() -> None:
    key = {**KEY, "recap": "optional"}
    updated = {"changed": {PERSON: "diff"}, "removed": [], "created": {}, "recaps": {}, "order": "no_recap"}
    verdicts = {o.check: o.verdict for o in session_score.check_row(key, {"observed": updated})}
    assert verdicts["recap"] == checks.PASS
    assert verdicts["recap_must_link"] == checks.PASS


def test_judge_view_shows_turns_reference_and_diffs_but_not_the_arm_or_the_recap() -> None:
    observed = {
        "changed": {PERSON: "+No meetings before 10:00.\n"},
        "removed": [],
        "created": {RECAP: "A recap by the candidate arm."},
        "recaps": {RECAP: {"text": "A recap by the candidate arm.", "links": {}}},
        "order": "before",
    }
    row = {"arm": "candidate", "model": "sonnet", "observed": observed, "compile_blocks": [{"outcome": "x"}]}
    view = session_score.judge_view(case(KEY), KEY, row)
    assert "+No meetings before 10:00." in view
    assert KEY["reference"] in view
    assert "1. hi" in view
    assert not {"candidate", "sonnet", "recap", "compile"} & set(view.lower().replace(".", " ").split())
