"""Own-note adoption: a folder rule turns Markdown files into draft notes beside their Sources."""

from __future__ import annotations

import hashlib
import types
from pathlib import Path

import pytest
from test_adoption_collection import (
    SOURCES,
    TODAY,
    _apply_all,
    _apply_from,
    _import,
    _page,
    _vault,
    tiny_budget,  # noqa: F401 - the fixture
)

from exomem import adopt, adoption_run, commands, context_refs, request_budget
from exomem import audit as audit_module

JOURNAL_RULE = {"path": "Coll/Journal", "adopt_as": "note", "note_type": "insight"}
TRIP = (
    "---\ntags: [travel]\naliases: [The Trip]\n---\n"
    "# Trip planning\n\nSee [[ideas]] and ![[pic.png]].\n"
)
FILES = {
    "Coll/Journal/trip.md": TRIP,
    "Coll/Journal/ideas.md": "# Ideas\n\nBack to [[trip]].\n",
    "Coll/Journal/pic.png": b"\x89PNG\r\n\x1a\n" + b"pixels",
    "Coll/Exports/dump.md": "# Dump\n\nraw export\n",
}


def _notes(vault: Path) -> list[Path]:
    return sorted(
        page for page in (vault / "Knowledge Base/Notes").rglob("*.md") if page.name != "index.md"
    )


def _copy(applied: dict, original: str) -> str:
    return applied["outcomes"][original]["target_path"]


def test_a_mixed_collection_becomes_draft_notes_beside_their_sources_and_keeps_the_originals(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path, FILES)
    run_id, planned = _import(vault, path="Coll", include=[JOURNAL_RULE, "Coll/Exports"])
    items = {item["collection_path"]: item for item in planned["plan"]["items"]}

    assert planned["plan"]["notes"] == {"to_create": 2, "existing": 0, "previous": 0}
    assert "note" not in items["Exports/dump.md"] and "note" not in items["Journal/pic.png"]
    assert items["Journal/trip.md"]["note"]["status"] == "draft"

    applied, _calls = _apply_all(vault, run_id, planned["plan"]["plan_id"])

    assert applied["phase"] == "applied"
    assert {path: (vault / path).read_bytes() for path in FILES} == {
        path: data if isinstance(data, bytes) else data.encode() for path, data in FILES.items()
    }
    trip_source = _copy(applied, "Coll/Journal/trip.md")
    trip_path = applied["outcomes"]["Coll/Journal/trip.md"]["note"]["target_path"]
    frontmatter, body = _page(vault, trip_path)
    assert frontmatter["status"] == "draft"
    assert frontmatter["sources"] == [f"[[{trip_source.removesuffix('.md')}]]"]
    assert frontmatter["tags"] == ["travel"]
    assert frontmatter["aliases"] == ["The Trip"]
    assert frontmatter["import_collection"] == "Coll"
    assert frontmatter["collection_path"] == "Journal/trip.md"
    # The original body is kept verbatim. The appended links point at the other note
    # and at the image's Source, not at the originals.
    ideas_path = applied["outcomes"]["Coll/Journal/ideas.md"]["note"]["target_path"]
    pic_source = _copy(applied, "Coll/Journal/pic.png")
    assert body == (
        "# Trip planning\n\nSee [[ideas]] and ![[pic.png]].\n\n## Links\n\n"
        f"- [[{ideas_path.removesuffix('.md')}]]\n- [[{pic_source.removesuffix('.md')}]]\n"
    )
    # The Source copy still holds the original bytes under the original hash.
    fenced = (vault / trip_source).read_text(encoding="utf-8").split("## Capture\n\n", 1)[1]
    fence = fenced.split("\n", 1)[0]
    captured = fenced[len(fence) + 1 : fenced.rindex(f"\n{fence}")]
    assert captured == TRIP
    assert hashlib.sha256(captured.encode()).hexdigest() == (
        applied["outcomes"]["Coll/Journal/trip.md"]["sha256"]
    )
    assert len(_notes(vault)) == 2  # the Exports file is a Source only


def test_a_body_tag_joins_the_front_matter_tags_and_a_hash_in_code_does_not(
    tmp_path: Path,
) -> None:
    original = (
        "---\ntags: [travel]\n---\n# Trip\n\nPacked for #hiking today.\n\n"
        "Run `#inline-code` first.\n\n```\n#fenced-code\n```\n"
    )
    vault = _vault(tmp_path, {"Coll/Journal/trip.md": original})
    run_id, planned = _import(vault, path="Coll", include=[JOURNAL_RULE])

    applied, _calls = _apply_all(vault, run_id, planned["plan"]["plan_id"])

    note_path = applied["outcomes"]["Coll/Journal/trip.md"]["note"]["target_path"]
    assert _page(vault, note_path)[0]["tags"] == ["travel", "hiking"]
    assert _page(vault, _copy(applied, "Coll/Journal/trip.md"))[0]["tags"] == [
        "imported", "travel", "hiking",
    ]


def test_a_note_keeps_its_original_body_when_it_links_a_note_made_after_it(
    tmp_path: Path,
) -> None:
    # Notes are made in path order: alpha's note exists before zeta's, not after.
    files = {
        "Coll/Journal/alpha.md": "---\ntags: [x]\n---\nSee [[zeta]], then [[zeta#End|the end]].\n",
        "Coll/Journal/zeta.md": "# Zeta\n\nBack to [[alpha]].",
    }
    vault = _vault(tmp_path, files)
    run_id, planned = _import(vault, path="Coll", include=[JOURNAL_RULE])
    applied, _calls = _apply_all(vault, run_id, planned["plan"]["plan_id"])
    note = {
        original: applied["outcomes"][original]["note"]["target_path"].removesuffix(".md")
        for original in files
    }

    assert _page(vault, f"{note['Coll/Journal/alpha.md']}.md")[1] == (
        "See [[zeta]], then [[zeta#End|the end]].\n\n## Links\n\n"
        f"- [[{note['Coll/Journal/zeta.md']}]]\n- [[{note['Coll/Journal/zeta.md']}#End]]\n"
    )
    assert _page(vault, f"{note['Coll/Journal/zeta.md']}.md")[1] == (
        f"# Zeta\n\nBack to [[alpha]].\n\n## Links\n\n- [[{note['Coll/Journal/alpha.md']}]]\n"
    )


def test_a_draft_note_leaves_its_source_in_the_backlog_until_the_note_is_promoted(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path, FILES)
    run_id, planned = _import(vault, path="Coll", include=[JOURNAL_RULE, "Coll/Exports"])
    applied, _calls = _apply_all(vault, run_id, planned["plan"]["plan_id"])

    report = audit_module.audit(vault, categories=["unprocessed_source"])

    outstanding = {finding.path: finding for finding in report.findings}
    trip_source = _copy(applied, "Coll/Journal/trip.md")
    assert trip_source in outstanding
    assert {row["reason"] for row in outstanding[trip_source].meta["derivatives"]} == {"pending"}


def test_changing_the_rule_changes_the_plan_identity(tmp_path: Path) -> None:
    vault = _vault(tmp_path, FILES)
    _run_id, as_notes = _import(vault, path="Coll", include=[JOURNAL_RULE, "Coll/Exports"])
    _run_id, as_sources = _import(vault, path="Coll", include=["Coll"])

    assert as_notes["plan"]["plan_id"] != as_sources["plan"]["plan_id"]
    assert as_sources["plan"]["notes"]["to_create"] == 0


@pytest.mark.parametrize(
    ("rule", "code"),
    [
        ({"adopt_as": "note", "note_type": "no-such-type"}, "NOTE_TYPE_UNREGISTERED"),
        ({"adopt_as": "note"}, "NOTE_TYPE_REQUIRED"),
        ({"adopt_as": "note", "note_type": "research-note"}, "NOTE_TYPE_CANNOT_PLACE"),
        ({"adopt_as": "source", "note_type": "insight"}, "INVALID_RULE"),
        ({"adopt_as": "document", "note_type": "insight"}, "INVALID_RULE"),
    ],
)
def test_a_rule_the_registry_or_the_note_leaf_cannot_serve_is_refused_and_changes_nothing(
    tmp_path: Path, rule: dict, code: str
) -> None:
    vault = _vault(tmp_path, FILES)
    run_id = adoption_run.start(vault, path="Coll", today=TODAY)["run_id"]
    before = adoption_run.select(vault, run_id=run_id, include=["Coll"])

    with pytest.raises(adoption_run.AdoptionRunError) as refused:
        adoption_run.select(vault, run_id=run_id, include=[{"path": "Coll/Journal", **rule}])

    assert refused.value.code == code
    assert adoption_run.status(vault, run_id=run_id)["selection"] == before["selection"]


def test_a_project_and_domain_on_the_rule_place_a_type_that_needs_them(tmp_path: Path) -> None:
    vault = _vault(tmp_path, {"Coll/Notes/atlas.md": "# Atlas\n\nResearch text.\n"})
    rule = {
        "path": "Coll/Notes",
        "adopt_as": "note",
        "note_type": "research-note",
        "project": "atlas",
    }
    run_id, planned = _import(vault, path="Coll", include=[rule])
    applied, _calls = _apply_all(vault, run_id, planned["plan"]["plan_id"])

    note_path = applied["outcomes"]["Coll/Notes/atlas.md"]["note"]["target_path"]
    frontmatter, _body = _page(vault, note_path)
    assert (frontmatter["type"], frontmatter["status"], frontmatter["project"]) == (
        "research-note",
        "draft",
        "atlas",
    )


def test_a_tiny_budget_creates_each_note_once_across_several_calls(
    tmp_path: Path,
    tiny_budget: None,  # noqa: F811 - the imported fixture
) -> None:
    files = {f"Coll/Journal/entry-{n}.md": f"# Entry {n}\n\nText {n}.\n" for n in range(4)}
    vault = _vault(tmp_path, files)
    run_id, planned = _import(vault, path="Coll", include=[JOURNAL_RULE])

    applied, calls = _apply_all(vault, run_id, planned["plan"]["plan_id"])

    assert calls > 1
    assert applied["phase"] == "applied"
    assert applied["progress"] == {"applied": 4, "failed": 0, "remaining": 0, "total": 4}
    assert len(_notes(vault)) == 4
    assert len(list((vault / SOURCES / "Imported").glob("*.md"))) == 4


def test_a_crash_after_a_note_commits_but_before_its_outcome_is_recorded_creates_no_second_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _vault(tmp_path, {"Coll/Journal/trip.md": TRIP, "Coll/Journal/ideas.md": "# Ideas\n"})
    run_id, planned = _import(vault, path="Coll", include=[JOURNAL_RULE])
    plan_id = planned["plan"]["plan_id"]

    class ProcessDied(BaseException):
        """Not an Exception: nothing in the run catches it, as with a killed process."""

    def die(*_args, **_kwargs):
        raise ProcessDied

    with monkeypatch.context() as crash:
        crash.setattr(adoption_run, "_settle_note", die)
        with pytest.raises(ProcessDied):
            adoption_run.apply(vault, run_id=run_id, plan_id=plan_id, today=TODAY)
    committed = _notes(vault)
    assert len(committed) == 1  # the note landed; the run object never heard

    applied, _calls = _apply_all(vault, run_id, plan_id)

    assert applied["phase"] == "applied"
    assert len(_notes(vault)) == 2
    assert committed[0] in _notes(vault)
    statuses = {
        path: outcome["note"]["status"] for path, outcome in applied["outcomes"].items()
    }
    assert sorted(statuses.values()) == ["already-applied", "applied"]


def test_an_unchanged_re_import_creates_nothing_and_reports_the_notes_as_already_applied(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path, FILES)
    first_id, planned = _import(vault, path="Coll", include=[JOURNAL_RULE, "Coll/Exports"])
    _apply_all(vault, first_id, planned["plan"]["plan_id"])
    pages_before = sorted(str(p) for p in vault.rglob("*.md") if "_Imports" not in str(p))

    run_id, replanned = _import(vault, path="Coll", include=[JOURNAL_RULE, "Coll/Exports"])
    second, _calls = _apply_all(vault, run_id, replanned["plan"]["plan_id"])

    assert replanned["plan"]["notes"] == {"to_create": 0, "existing": 2, "previous": 0}
    assert {o["status"] for o in second["outcomes"].values()} == {"already-applied"}
    assert sorted(str(p) for p in vault.rglob("*.md") if "_Imports" not in str(p)) == pages_before


def test_a_re_import_finds_a_note_the_user_moved_without_rewriting_links(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path, {"Coll/Journal/trip.md": TRIP})
    first_id, planned = _import(vault, path="Coll", include=[JOURNAL_RULE])
    first, _calls = _apply_all(vault, first_id, planned["plan"]["plan_id"])
    note = first["outcomes"]["Coll/Journal/trip.md"]["note"]["target_path"]
    moved = "Knowledge Base/Notes/Insights/Travel/trip-planning.md"
    # The Source's `ingested_into` still names the old path.
    commands.op_move_file(vault, note, moved, update_wikilinks=False)

    run_id, replanned = _import(vault, path="Coll", include=[JOURNAL_RULE])
    second, _calls = _apply_all(vault, run_id, replanned["plan"]["plan_id"])

    assert replanned["plan"]["notes"] == {"to_create": 0, "existing": 1, "previous": 0}
    assert second["outcomes"]["Coll/Journal/trip.md"]["status"] == "already-applied"
    assert _notes(vault) == [vault / moved]


def test_a_changed_original_plans_only_a_new_source_and_names_the_note_it_leaves_behind(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path, {"Coll/Journal/trip.md": TRIP})
    first_id, planned = _import(vault, path="Coll", include=[JOURNAL_RULE])
    first, _calls = _apply_all(vault, first_id, planned["plan"]["plan_id"])
    first_note = first["outcomes"]["Coll/Journal/trip.md"]["note"]["target_path"]
    (vault / "Coll/Journal/trip.md").write_text(TRIP + "\nA new paragraph.\n", encoding="utf-8")

    run_id, replanned = _import(vault, path="Coll", include=[JOURNAL_RULE])
    item = replanned["plan"]["items"][0]
    second, _calls = _apply_all(vault, run_id, replanned["plan"]["plan_id"])

    assert replanned["plan"]["notes"] == {"to_create": 0, "existing": 0, "previous": 1}
    assert item["action"] == "copy-as-source" and "note" not in item
    assert len(_notes(vault)) == 1
    assert "A new paragraph" not in (vault / first_note).read_text(encoding="utf-8")
    new_source = second["outcomes"]["Coll/Journal/trip.md"]["target_path"]
    assert _page(vault, new_source)[0]["previous_note"] == context_refs.vault_ref(first_note)
    assert adoption_run.finish(vault, run_id=run_id, today=TODAY)["coverage"]["notes"] == {
        "planned": 0,
        "created": 0,
        "already-applied": 0,
        "failed": 0,
        "left_with_previous_note": 1,
    }


def test_a_note_target_taken_by_another_writer_fails_that_item_and_leaves_the_page(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path, {"Coll/Journal/a.md": "# A\n", "Coll/Journal/b.md": "# B\n"})
    run_id, planned = _import(vault, path="Coll", include=[JOURNAL_RULE])
    items = {item["collection_path"]: item for item in planned["plan"]["items"]}
    squatter = vault / items["Journal/a.md"]["note"]["target_path"]
    squatter.parent.mkdir(parents=True, exist_ok=True)
    squatter.write_text("someone else's page\n", encoding="utf-8")

    applied, _calls = _apply_all(vault, run_id, planned["plan"]["plan_id"])

    a = applied["outcomes"]["Coll/Journal/a.md"]
    assert (a["status"], a["code"]) == ("failed", "TARGET_OCCUPIED")
    assert a["target_path"]  # the Source it made stays
    assert applied["outcomes"]["Coll/Journal/b.md"]["status"] == "applied"
    assert squatter.read_text(encoding="utf-8") == "someone else's page\n"
    assert applied["phase"] == "partial"


def test_finish_counts_created_found_and_failed_notes(tmp_path: Path) -> None:
    vault = _vault(tmp_path, {"Coll/Journal/a.md": "# A\n", "Coll/Journal/b.md": "# B\n"})
    run_id, planned = _import(vault, path="Coll", include=[JOURNAL_RULE])
    items = {item["collection_path"]: item for item in planned["plan"]["items"]}
    squatter = vault / items["Journal/a.md"]["note"]["target_path"]
    squatter.parent.mkdir(parents=True, exist_ok=True)
    squatter.write_text("someone else's page\n", encoding="utf-8")
    _apply_all(vault, run_id, planned["plan"]["plan_id"])

    finished = adoption_run.finish(vault, run_id=run_id, today=TODAY)

    assert finished["coverage"]["notes"] == {
        "planned": 2,
        "created": 1,
        "already-applied": 0,
        "failed": 1,
        "left_with_previous_note": 0,
    }


def test_a_call_stops_between_notes_when_the_next_would_not_fit_and_the_continuation_resumes_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = {"now": 1000.0}
    fake_time = types.SimpleNamespace(monotonic=lambda: clock["now"])
    monkeypatch.setattr(adoption_run, "time", fake_time)
    monkeypatch.setattr(request_budget, "time", fake_time)
    monkeypatch.setattr(request_budget, "DELIVERY_RESERVE_SECONDS", 2.0)
    monkeypatch.setenv(request_budget.BUDGET_ENV_VAR, "30")
    request_budget.reset_resolution_cache()
    real_create = adopt.create_draft_notes

    def slow_notes(root, notes, **kwargs):
        clock["now"] += 20.0 * len(notes)  # one note costs two thirds of a call
        return real_create(root, notes, **kwargs)

    monkeypatch.setattr(adopt, "create_draft_notes", slow_notes)
    files = {f"Coll/Journal/entry-{n}.md": f"# Entry {n}\n\nText {n}.\n" for n in range(3)}
    vault = _vault(tmp_path, files)
    run_id, planned = _import(vault, path="Coll", include=[JOURNAL_RULE])
    plan_id = planned["plan"]["plan_id"]

    first = adoption_run.apply(vault, run_id=run_id, plan_id=plan_id, today=TODAY)

    assert first["continuation"] and first["phase"] == "applying"
    assert len(_notes(vault)) == 1  # the Sources of all three are down; one note is
    assert len(list((vault / SOURCES / "Imported").glob("*.md"))) == 3
    assert first["progress"] == {"applied": 1, "failed": 0, "remaining": 2, "total": 3}

    last, _calls = _apply_from(vault, run_id, plan_id, first)

    assert last["phase"] == "applied"
    assert len(_notes(vault)) == 3
    assert len(list((vault / SOURCES / "Imported").glob("*.md"))) == 3


def test_a_quick_refusal_does_not_make_the_next_batch_of_notes_look_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = {"now": 1000.0}
    fake_time = types.SimpleNamespace(monotonic=lambda: clock["now"])
    monkeypatch.setattr(adoption_run, "time", fake_time)
    monkeypatch.setattr(request_budget, "time", fake_time)
    monkeypatch.setattr(request_budget, "DELIVERY_RESERVE_SECONDS", 2.0)
    monkeypatch.setenv(request_budget.BUDGET_ENV_VAR, "40")
    request_budget.reset_resolution_cache()
    files = {f"Coll/Journal/entry-{n}.md": f"# Entry {n}\n\nText {n}.\n" for n in range(8)}
    vault = _vault(tmp_path, files)
    run_id, planned = _import(vault, path="Coll", include=[JOURNAL_RULE])
    refused = planned["plan"]["items"][0]["original_path"]  # the first note a call makes
    real_validate = adopt._validate_draft
    real_create = adopt.create_draft_notes

    def refuse_the_first_note_at_once(root, planned_item, arguments):
        if planned_item["original_path"] == refused:
            raise adopt.AdoptError("PLAN_STALE", "refused by the test")
        return real_validate(root, planned_item, arguments)

    def eight_seconds_per_made_note(root, notes, **kwargs):
        outcomes = real_create(root, notes, **kwargs)
        clock["now"] += 8.0 * sum(outcome.created is not None for outcome in outcomes)
        return outcomes

    monkeypatch.setattr(adopt, "_validate_draft", refuse_the_first_note_at_once)
    monkeypatch.setattr(adopt, "create_draft_notes", eight_seconds_per_made_note)

    elapsed: list[float] = []
    last: dict = {}
    while not elapsed or last.get("continuation"):
        began = clock["now"]
        last = adoption_run.apply(
            vault, run_id=run_id, plan_id=planned["plan"]["plan_id"],
            continuation=last.get("continuation"), today=TODAY,
        )
        elapsed.append(clock["now"] - began)

    assert max(elapsed) <= 40.0 - 2.0
    assert last["outcomes"][refused]["note"]["code"] == "PLAN_STALE"
    assert last["progress"] == {"applied": 7, "failed": 1, "remaining": 0, "total": 8}
    assert len(_notes(vault)) == 7


def test_own_note_adoption_uses_the_project_home_in_its_frozen_plan(tmp_path: Path) -> None:
    from test_vault_homes import _save

    vault = _vault(tmp_path, {"Coll/Journal/rig.md": "# Imported rigs\n\nA retained result.\n"})
    _save(vault, {"rigs": {"label": "Rigs", "attributes": {
        "folder": "Notes/Rigs", "projects": ["atlas"],
    }}})
    rule = {**JOURNAL_RULE, "note_type": "research-note", "project": "atlas"}
    run_id, planned = _import(vault, path="Coll", include=[rule])
    target = planned["plan"]["items"][0]["note"]["target_path"]
    assert target.startswith("Knowledge Base/Notes/Rigs/")

    applied, _calls = _apply_all(vault, run_id, planned["plan"]["plan_id"])

    assert applied["phase"] == "applied"
    assert applied["outcomes"]["Coll/Journal/rig.md"]["note"]["target_path"] == target
    assert _page(vault, target)[0]["status"] == "draft"
