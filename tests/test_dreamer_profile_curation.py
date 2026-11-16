"""Profile curation (`upkeep_profile_curation`): a profile page that may be out of date.

The workflow under test is the dreamer noticing that the owner's profile no longer matches the vault, then the
item reaching the agent with the governed edit route. Evidence is structural: a new preference or constraint
unit on a page the profile links or of its domain, and a profile line that a typed relation or a date has
outdated. Each check names a failure only a real vault shows: a profile that never hears about a note it links,
a dismissal that outlives new evidence, and a date that passes with no write to wake the dreamer.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import dreamer_fixture as fx
import pytest

from exomem import commands, dreamer, dreamer_families, dreamer_store, freshness, review_state, upkeep
from exomem.writer_lease import invoke_command

KB = fx.KB
PROFILE = f"{KB}/Notes/Profiles/games.md"
GLOBAL_PROFILE = f"{KB}/Notes/Profiles/global.md"
SESSIONS = f"{KB}/Notes/Insights/games-sessions.md"
DETAIL = f"{KB}/Notes/Insights/games-detail.md"
ELSEWHERE = f"{KB}/Notes/Insights/travel-habits.md"
KIND = dreamer_families.CURATION_KIND

TAXONOMY = (
    "schema_version: 1\n"
    "domains:\n"
    "  media: {label: Media}\n"
    "  games: {label: Games, parent: media}\n"
    "  board-games: {label: Board games, parent: games}\n"
    "  travel: {label: Travel}\n"
)
CUSTOM_CATEGORY = (
    "schema_version: 1\n"
    "categories:\n"
    "  interface_preference:\n"
    "    description: A preference about how an interface behaves.\n"
)
CUSTOM_ROLE = "schema_version: 1\nroles:\n  preferences:\n    add_categories: [interface_preference]\n"
PRESERVED = "- [rule] Prefer tactical play over luck. ^tactical\n- [rule] Keep sessions under two hours. ^short\n"


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


def _open(vault: Path, subject: str = PROFILE) -> dict | None:
    conn = dreamer_store.DreamerStore(vault).connect()
    try:
        row = dreamer_store.DreamerStore.candidate(conn, dreamer_store.candidate_id(KIND, subject, ""))
    finally:
        conn.close()
    return row if row is not None and row["state"] == "open" else None


def _profile(*, topics: str | None, links: str = "", lines: str = PRESERVED, updated: str = "2026-06-01") -> str:
    scope = f"topics: [{topics}]\n" if topics else ""
    return (
        f"---\ntitle: Profile\ntype: profile\nstatus: active\ncreated: 2026-01-01\nupdated: {updated}\n{scope}---\n\n"
        f"# Profile\n\n{links}\n\n## Observations\n\n{lines}"
    )


def _note(title: str, observations: str, *, updated: str = "2026-07-01", topics: str | None = None, extra: str = "") -> str:
    text = fx.insight(title, sources=["field-report-one"], updated=updated, extra=f"{observations}{extra}")
    return text.replace("status: active\n", f"status: active\ntopics: [{topics}]\n") if topics else text


def _vault(tmp_path: Path, *, global_profile: bool = False, links: str = "", lines: str = PRESERVED) -> Path:
    vault = fx.build(tmp_path)
    fx.write(vault, f"{KB}/_Schema/source-taxonomy.yaml", TAXONOMY)
    if global_profile:
        fx.edit(vault, GLOBAL_PROFILE, _profile(topics=None, links=links, lines=lines))
    else:
        fx.edit(vault, PROFILE, _profile(topics="games", links=links, lines=lines))
    _quiet(vault)
    return vault


def _tool(vault: Path, name: str, **kwargs):
    from exomem.command_surface import resolve_command

    command = resolve_command(name, commands.PRODUCT_COMMANDS)
    assert command is not None
    return invoke_command(command, vault, **kwargs)


def test_a_new_preference_on_a_page_of_a_descendant_domain_raises_one_item(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    assert _open(vault) is None

    fx.edit(vault, SESSIONS, _note("Games sessions", "- [preference] Prefer short sessions. ^brief\n", topics="board-games"))
    _quiet(vault)

    row = _open(vault)
    assert row is not None and row["family"] == dreamer_families.CURATION_FAMILY
    assert {item["path"] for item in row["evidence"] if item["role"] == "unit"} == {SESSIONS}
    assert row["measures"] == {"new_units": 1, "stale_lines": 0, "at_least": False}
    assert row["route"] == {
        "tool": "edit_note",
        "args": {"path": PROFILE, "operation": {"kind": "replace_string", "validate_only": True}},
    }


def test_a_category_the_vault_maps_to_preferences_raises_the_item(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    fx.write(vault, f"{KB}/_Schema/semantic-language-registry.yaml", CUSTOM_CATEGORY)
    fx.write(vault, f"{KB}/_Schema/context-roles.yaml", CUSTOM_ROLE)

    fx.edit(vault, SESSIONS, _note("Games sessions", "- [interface_preference] Show the timer. ^timer\n", topics="games"))
    _quiet(vault)

    assert _open(vault) is not None


def test_a_category_the_vault_has_not_mapped_raises_nothing(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    fx.write(vault, f"{KB}/_Schema/semantic-language-registry.yaml", CUSTOM_CATEGORY)

    fx.edit(vault, SESSIONS, _note("Games sessions", "- [interface_preference] Show the timer. ^timer\n", topics="games"))
    _quiet(vault)

    assert _open(vault) is None


def test_a_preference_older_than_the_profile_or_of_another_category_raises_nothing(tmp_path: Path) -> None:
    vault = _vault(tmp_path)

    fx.edit(vault, SESSIONS, _note("Games sessions", "- [preference] Prefer short sessions. ^brief\n", updated="2026-05-01", topics="games"))
    fx.edit(vault, DETAIL, _note("Games detail", "- [finding] Sessions run long. ^long\n", topics="games"))
    _quiet(vault)

    assert _open(vault) is None


def test_a_new_preference_on_a_linked_page_with_no_domain_raises_the_item(tmp_path: Path) -> None:
    vault = _vault(tmp_path, links="Detail in [[Notes/Insights/games-detail]].")

    fx.edit(vault, DETAIL, _note("Games detail", "- [preference] Prefer co-op. ^coop\n"))
    _quiet(vault)

    row = _open(vault)
    assert row is not None
    assert {item["path"] for item in row["evidence"] if item["role"] == "unit"} == {DETAIL}


def test_a_global_profile_hears_of_a_linked_page_and_ignores_a_unit_elsewhere(tmp_path: Path) -> None:
    vault = _vault(tmp_path, global_profile=True, links="Detail in [[Notes/Insights/games-detail]].")

    fx.edit(vault, ELSEWHERE, _note("Travel habits", "- [preference] Prefer trains. ^trains\n", topics="travel"))
    _quiet(vault)
    assert _open(vault, GLOBAL_PROFILE) is None

    fx.edit(vault, DETAIL, _note("Games detail", "- [preference] Prefer co-op. ^coop\n"))
    _quiet(vault)
    row = _open(vault, GLOBAL_PROFILE)
    assert row is not None
    assert {item["path"] for item in row["evidence"] if item["role"] == "unit"} == {DETAIL}


@pytest.mark.parametrize(
    ("relation", "role"), [("supersedes", "superseding"), ("contradicts", "contradicting")],
)
def test_a_typed_relation_to_a_profile_line_raises_the_item(tmp_path: Path, relation: str, role: str) -> None:
    vault = _vault(tmp_path)

    fx.edit(
        vault, SESSIONS,
        _note(
            "Games sessions", "- [finding] Long sessions turned out fine. ^fine\n", topics="games",
            extra=f"\n## Relations\n\n- {relation} [[Notes/Profiles/games#short]]\n",
        ),
    )
    _quiet(vault)

    row = _open(vault)
    assert row is not None
    assert {item["path"] for item in row["evidence"] if item["role"] == role} == {SESSIONS}
    assert row["measures"]["stale_lines"] == 1 and row["measures"]["new_units"] == 0


def _dated_line(field: str, day: str) -> str:
    return f"\n## Claim\n- id: summer\n- {field}: {day}\n\nPlay lighter games in summer.\n"


@pytest.mark.parametrize("date_field", ["until", "check_by"])
def test_a_profile_line_past_its_date_raises_the_item(tmp_path: Path, date_field: str) -> None:
    vault = _vault(tmp_path)
    assert _open(vault) is None

    fx.edit(vault, PROFILE, _profile(topics="games", lines=PRESERVED + _dated_line(date_field, "2026-03-01")))
    _quiet(vault, now=dt.datetime(2026, 7, 1, tzinfo=dt.UTC).timestamp())

    row = _open(vault)
    assert row is not None and row["measures"]["stale_lines"] == 1 and row["measures"]["new_units"] == 0


def test_a_date_that_passes_raises_the_item_with_no_write(tmp_path: Path) -> None:
    vault = _vault(tmp_path, lines=PRESERVED + _dated_line("until", "2026-06-15"))
    before = dt.datetime(2026, 6, 10, 12, tzinfo=dt.UTC).timestamp()
    after = dt.datetime(2026, 6, 20, 12, tzinfo=dt.UTC).timestamp()

    _quiet(vault, now=before)
    assert _open(vault) is None
    _quiet(vault, now=after)

    assert _open(vault) is not None


def test_a_dismissed_item_stays_quiet_until_new_evidence(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    fx.edit(vault, SESSIONS, _note("Games sessions", "- [preference] Prefer short sessions. ^brief\n", topics="games"))
    _quiet(vault)
    ref = upkeep.upkeep_ref(_open(vault)["id"])
    served = _tool(vault, "review_memory", mode="item", ref=ref)["item"]
    _tool(
        vault, "triage_memory", ref=ref, action="dismiss",
        why="handled: the profile already says it", expected_fingerprint=served["fingerprint"],
    )
    store = review_state.ReviewStateStore(vault)

    # The same unit under a new revision of the page is no new evidence.
    fx.edit(vault, SESSIONS, _note("Games sessions", "- [preference] Prefer short sessions. ^brief\n", updated="2026-07-05", topics="games"))
    _quiet(vault)
    held = _open(vault)
    assert store.effective_state(held["id"], held["fingerprint"])[0] == "dismissed"

    fx.edit(vault, DETAIL, _note("Games detail", "- [preference] Prefer co-op. ^coop\n", topics="games"))
    _quiet(vault)
    moved = _open(vault)
    assert moved["id"] == held["id"] and moved["fingerprint"] != held["fingerprint"]
    assert store.effective_state(moved["id"], moved["fingerprint"])[0] == "open"


def test_the_agent_is_served_the_profile_its_evidence_and_the_edit_route(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    fx.edit(vault, SESSIONS, _note("Games sessions", "- [preference] Prefer short sessions. ^brief\n", topics="games"))
    _quiet(vault)

    served = _tool(vault, "review_memory", mode="item", ref=upkeep.upkeep_ref(_open(vault)["id"]))["item"]

    assert served["kind"] == KIND and served["label"] == upkeep.LABELS[KIND]
    assert served["subject"]["title"] == "Profile"
    assert [item["title"] for item in served["evidence"]] == ["Games sessions"]
    assert served["route"]["tool"] == "edit_note" and served["route"]["args"]["path"] == PROFILE
