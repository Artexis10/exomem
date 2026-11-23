"""One relocation call: a whole set of pages moves to a project and to its titles under one approval.

`maintain_memory(mode="curation", curation_action="relocate")` derives the plan on the server and
seals it once. One apply runs it across as many calls as the request budget needs, and one approved
compensation undoes it. Each test names the failure it catches.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

import yaml

from exomem import commands, curation, memory_refs, page_fields, relocate, writer_lease
from exomem.vault import find_body_wikilinks, parse_frontmatter

RESEARCH = "Knowledge Base/Notes/Research"
INSIGHTS = "Knowledge Base/Notes/Insights"
ARTICLES = "Knowledge Base/Sources/Articles"
CONCEPTS = "Knowledge Base/Entities/Concepts"
REGISTRY = "Knowledge Base/_Schema/project-keys.yaml"
MAINTAIN = next(c for c in commands.PRODUCT_COMMANDS if c.name == "maintain_memory")


def _write(vault: Path, rel: str, text: str) -> str:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return rel


def _link(rel: str) -> str:
    return f"[[{rel.removeprefix('Knowledge Base/').removesuffix('.md')}]]"


def _source(vault: Path, number: int, project: str, citers: list[str]) -> str:
    backrefs = "".join(f'\n  - "{_link(rel)}"' for rel in citers)
    return _write(
        vault,
        f"{ARTICLES}/2026-03-{number:02d}-field-capture-{number:02d}.md",
        f"---\ntype: source\nsource_type: article\ncaptured: 2026-03-01\nproject: {project}\n"
        f"ingested_into:{backrefs}\n---\n\n# Source: Field capture {number}\n\n"
        f"Measurements gathered on survey day {number}.\n",
    )


def _page(vault: Path, rel: str, note_type: str, title: str, project_field: str, sources: list[str]) -> str:
    cited = "".join(f'\n  - "{_link(source)}"' for source in sources)
    return _write(
        vault,
        rel,
        f"---\nexomem_id: {uuid.uuid4()}\ntype: {note_type}\nstatus: active\n{project_field}\n"
        f"created: 2026-03-02\nsources:{cited or ' []'}\n---\n\n# {title}\n\n"
        f"What survey {title.lower()} found.\n",
    )


def _relocation_vault(vault: Path) -> dict[str, object]:
    """Forty pages: 38 research notes in two projects, one entity and one insight.

    Each research note cites its own Source and the insight cites two, so forty Sources are cited;
    an entity cites none. The first two Sources of each project are also cited by a page that
    stays in that project. A summary page outside the set links to every page in the set.
    """
    _write(
        vault,
        REGISTRY,
        "projects:\n  harbor:\n    folder: Harbor\n    category: product\n"
        "  lantern:\n    folder: Lantern\n    category: product\n",
    )
    stays = {
        "harbor": f"{RESEARCH}/Harbor/Harbor baseline.md",
        "lantern": f"{RESEARCH}/Lantern/Lantern baseline.md",
    }
    insight = f"{INSIGHTS}/insight-draft.md"
    pages: list[str] = []
    sources: list[str] = []
    shared: list[str] = []
    by_project: dict[str, list[str]] = {"harbor": [], "lantern": []}
    for number in range(1, 41):
        project = "harbor" if number <= 19 else "lantern"
        citer = insight if number > 38 else (
            f"{RESEARCH}/Harbor/tide-note-{number:02d}.md"
            if project == "harbor"
            else f"{RESEARCH}/Lantern/lamp-note-{number:02d}.md"
        )
        is_shared = number in {1, 2, 20, 21}
        source = _source(vault, number, project, [citer, stays[project]] if is_shared else [citer])
        sources.append(source)
        if is_shared:
            shared.append(source)
            by_project[project].append(source)
        if number <= 38:
            title = f"Tide gauge reading {number}" if project == "harbor" else f"Lamp wick trial {number}"
            pages.append(_page(vault, citer, "research-note", title, f"project: {project}", [source]))
    pages.append(_page(
        vault, f"{CONCEPTS}/Buoy Array.md", "entity", "Buoy Array",
        "entity_type: concept\nproject: harbor", [],
    ))
    pages.append(_page(
        vault, insight, "insight", "Moorings drift in spring tides", "projects: [lantern]",
        sources[38:],
    ))
    for project, rel in stays.items():
        _page(vault, rel, "research-note", f"{project.title()} baseline", f"project: {project}", by_project[project])
    links = "\n".join(f"- {_link(rel)}" for rel in pages)
    overview = _write(
        vault,
        f"{INSIGHTS}/Survey overview.md",
        f"---\nexomem_id: {uuid.uuid4()}\ntype: insight\nstatus: active\ncreated: 2026-03-03\n"
        f"sources: []\n---\n\n# Survey overview\n\n{links}\n",
    )
    return {"pages": pages, "sources": sources, "shared": shared, "overview": overview}


def _maintain(vault: Path, action: str, **arguments: object) -> dict[str, object]:
    return writer_lease.invoke_command(
        MAINTAIN, vault, mode="curation", curation_action=action, **arguments
    )


def _snapshot(vault: Path, rels: list[str]) -> dict[str, bytes]:
    return {rel: (vault / rel).read_bytes() for rel in rels}


def _carrying(vault: Path) -> set[str]:
    return set(page_fields.fields_carrying(vault, page_fields.CARRIES_PROJECT_KEY))


def _without(fields: dict[str, object], names: set[str]) -> dict[str, object]:
    return {key: value for key, value in fields.items() if key not in names}


def _keys(fields: dict[str, object], names: set[str]) -> set[str]:
    return {key for name in names for key in page_fields.carried_keys(fields.get(name))}


def _links_everywhere(vault: Path) -> set[str]:
    kb = vault / "Knowledge Base"
    return {
        link.group(1).strip()
        for path in kb.rglob("*.md")
        for link in find_body_wikilinks(path.read_text(encoding="utf-8"))
    }


@pytest.fixture
def short_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """A request budget that covers a few steps of a call, not the whole run."""
    monkeypatch.setenv("EXOMEM_MCP_REQUEST_BUDGET_SECONDS", "10")


# The 81-step run and its undo commit one file move or Source edit per step.
@pytest.mark.timeout(600)
def test_one_relocation_and_one_approval_move_forty_pages_their_sources_and_their_links(
    vault: Path, short_budget: None, corpus_cache_on: None
) -> None:
    """The repair that took one approval per page and one call per Source edit.

    It catches a relocation that loses a page's identity or lineage, drops a project key that a
    page outside the set still relies on, changes a Source beyond its project fields and the links
    that name moved pages, leaves a link to an old path, needs more than one approval, or fails to
    undo byte for byte.
    """
    built = _relocation_vault(vault)
    pages: list[str] = built["pages"]  # type: ignore[assignment]
    sources: list[str] = built["sources"]  # type: ignore[assignment]
    shared: list[str] = built["shared"]  # type: ignore[assignment]
    before = _snapshot(vault, [*pages, *sources, built["overview"]])  # type: ignore[list-item]
    ids = {rel: parse_frontmatter(before[rel].decode())[0]["exomem_id"] for rel in pages}

    proposed = _maintain(vault, "relocate", pages=pages, to_project="meadow")

    summary = proposed["summary"]
    assert proposed["sealed"] is True and "plan" not in proposed
    assert summary["refused"]["count"] == 0, summary["refused"]
    assert summary["moves"]["count"] == 39  # 38 research notes and the insight's rename
    assert summary["corrected_in_place"]["count"] == 1  # the entity never moves
    assert summary["sources_gaining_key"]["count"] == 40
    assert summary["sources_losing_key"]["count"] == 36
    assert {row["path"] for row in summary["sources_keeping_old_key"]["shown"]} == set(shared)
    assert summary["registers_project"]["key"] == "meadow"
    assert summary["referrers_rewritten"] >= 1
    steps = proposed["steps"]
    plan = curation.CurationStore(vault).load_plan(proposed["run_id"])
    repoints = sum(1 for step in plan["steps"] if step["kind"] == curation.REPOINT_STEP_KIND)
    assert steps == 39 + 1 + 40 + repoints
    assert [item["step_id"] for item in plan["binding_manifest"] if item["preimage"] is not None] == []
    preview = _maintain(vault, "preview", run_id=proposed["run_id"])
    assert "actions" not in preview and "binding_manifest" not in preview
    assert preview["summary"]["steps"] == steps and preview["binding_health"] == "current"
    # A later preview shows the groups the person approved, not Source edits lumped with pages.
    assert {key: preview["summary"][key] for key in summary} == summary

    result = _maintain(
        vault,
        "apply",
        run_id=proposed["run_id"],
        plan_id=proposed["plan_id"],
        expected_plan_fingerprint=proposed["plan_fingerprint"],
        why="Approved relocation.",
    )
    calls = 1
    while result["phase"] == "executing":
        assert result["progress"]["this_call"] >= 1
        assert "receipts" not in result
        result = _maintain(vault, "resume", run_id=proposed["run_id"], plan_id=proposed["plan_id"])
        calls += 1

    assert result["phase"] == "completed", result
    assert result["progress"] == {"steps": steps, "committed": steps, "this_call": result["progress"]["this_call"]}
    assert calls < steps
    carrying = _carrying(vault)
    moved = {row["from"]: row["to"] for row in _all_moves(vault, proposed["run_id"])}
    for rel in pages:
        now = moved.get(rel, rel)
        fields = parse_frontmatter((vault / now).read_text(encoding="utf-8"))[0]
        assert fields["exomem_id"] == ids[rel]
        assert _keys(fields, carrying) == {"meadow"}
        if rel in moved:
            assert not (vault / rel).exists()
            assert [entry["path"] for entry in fields.get("moved_from") or []] == [rel]
    assert moved[f"{RESEARCH}/Harbor/tide-note-01.md"] == f"{RESEARCH}/Meadow/Tide gauge reading 1.md"
    assert moved[f"{INSIGHTS}/insight-draft.md"] == f"{INSIGHTS}/Moorings drift in spring tides.md"
    renames = {_link(old).strip("[]"): _link(new).strip("[]") for old, new in moved.items()}
    for rel in sources:
        old_fields, old_body, _ = parse_frontmatter(before[rel].decode())
        new_fields, new_body, _ = parse_frontmatter((vault / rel).read_text(encoding="utf-8"))
        expected_backrefs = [
            f"[[{renames.get(value.strip('[]'), value.strip('[]'))}]]"
            for value in old_fields["ingested_into"]
        ]
        assert _without(new_fields, carrying | {"ingested_into"}) == _without(old_fields, carrying | {"ingested_into"})
        assert new_fields["ingested_into"] == expected_backrefs
        assert new_body == old_body
        old_keys = _keys(old_fields, carrying)
        assert _keys(new_fields, carrying) == ({"meadow"} | old_keys if rel in shared else {"meadow"})
    assert not _links_everywhere(vault) & set(renames), "a link still names an old path"

    undo = _maintain(vault, "propose-compensation", run_id=proposed["run_id"])
    assert "plan" not in undo
    assert undo["summary"]["moves"]["count"] == 39
    assert undo["summary"]["corrected_in_place"]["count"] == 41
    undone = _maintain(
        vault,
        "apply-compensation",
        run_id=proposed["run_id"],
        plan_id=undo["plan_id"],
        expected_plan_fingerprint=undo["plan_fingerprint"],
        why="Undo the relocation.",
    )
    while undone["phase"] not in {"compensated", "failed", "blocked", "partial"}:
        undone = _maintain(vault, "resume", run_id=proposed["run_id"], plan_id=undo["plan_id"])
    assert undone["phase"] == "compensated", undone
    assert _snapshot(vault, list(before)) == before


def _all_moves(vault: Path, run_id: str) -> list[dict[str, str]]:
    plan = curation.CurationStore(vault).load_plan(run_id)
    return [
        {"from": step["args"]["old_path"], "to": step["args"]["new_path"]}
        for step in plan["steps"]
        if step["kind"] == "move"
    ]


def test_a_foreground_write_lands_between_two_steps_of_one_call(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An apply that held the writer for its whole run refused every write behind it busy.

    A write that queues while one step holds the boundary enters before the next step starts, and
    the same call still finishes the run.
    """
    built = _relocation_vault(vault)
    pages: list[str] = built["pages"][:6]  # type: ignore[index]
    proposed = _maintain(vault, "relocate", pages=pages, to_project="meadow")
    coordinator = writer_lease.get_manager()._mutation_coordinator_for(vault)  # noqa: SLF001
    started: list[int] = []
    queued_at: list[int] = []
    entered: list[int] = []
    real_next = curation._execute_next  # noqa: SLF001
    real_boundary = curation._step_boundary  # noqa: SLF001

    def other_writer() -> None:
        with writer_lease.get_manager().mutation_guard(vault, request_id="foreground", operation="edit"):
            # No step commits while this write holds the boundary, so the count places it between two steps.
            entered.append(len(curation.CurationStore(vault).reconstruct(proposed["run_id"])["committed_steps"]))

    writer = threading.Thread(target=other_writer)

    def counted(*args: object, **kwargs: object) -> dict[str, object]:
        # A step decided again under its hold calls back in; only the call's own steps count.
        if not writer_lease.active_manager().holds_mutation_boundary(vault):
            started.append(1)
        return real_next(*args, **kwargs)

    @contextmanager
    def held(root: Path, operation: str) -> Iterator[None]:
        with real_boundary(root, operation):
            if operation == "curation_step" and not queued_at:  # a whole step holds the boundary
                queued_at.append(len(started))
                writer.start()
                deadline = time.monotonic() + 10
                while coordinator.waiter_count() == 0:
                    assert time.monotonic() < deadline, "the foreground write never queued"
                    time.sleep(0.01)
            yield

    monkeypatch.setattr(curation, "_execute_next", counted)
    monkeypatch.setattr(curation, "_step_boundary", held)
    result = _maintain(
        vault,
        "apply",
        run_id=proposed["run_id"],
        plan_id=proposed["plan_id"],
        expected_plan_fingerprint=proposed["plan_fingerprint"],
        why="Approved relocation.",
    )
    writer.join(10)

    assert result["phase"] == "completed", result
    # It queued during step N's hold and entered once step N committed, before step N + 1 committed.
    assert queued_at and entered == queued_at, "the foreground write did not land before the next step"
    assert result["progress"]["this_call"] == result["progress"]["steps"]


def test_an_interrupted_relocation_resumes_without_losing_or_repeating_a_step(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A process that dies between two steps, or a resume that replays a stored reply."""
    built = _relocation_vault(vault)
    pages: list[str] = built["pages"][:10]  # type: ignore[index]
    proposed = _maintain(vault, "relocate", pages=pages, to_project="meadow")
    real = curation._execute_next  # noqa: SLF001
    calls: list[int] = []

    def dies_at_the_fifth_step(*args: object, **kwargs: object) -> dict[str, object]:
        calls.append(1)
        if len(calls) == 5:
            raise KeyboardInterrupt("process stopped")
        return real(*args, **kwargs)

    monkeypatch.setattr(curation, "_execute_next", dies_at_the_fifth_step)
    with pytest.raises(KeyboardInterrupt):
        curation.apply(
            vault,
            run_id=proposed["run_id"],
            plan_id=proposed["plan_id"],
            expected_plan_fingerprint=proposed["plan_fingerprint"],
            why="Approved relocation.",
        )
    monkeypatch.setattr(curation, "_execute_next", real)
    store = curation.CurationStore(vault)
    assert len(store.reconstruct(proposed["run_id"])["committed_steps"]) == 4

    result = _maintain(vault, "resume", run_id=proposed["run_id"], plan_id=proposed["plan_id"])
    while result["phase"] == "executing":
        result = _maintain(vault, "resume", run_id=proposed["run_id"], plan_id=proposed["plan_id"])

    assert result["phase"] == "completed", result
    committed = store.reconstruct(proposed["run_id"])["committed_steps"]
    plan = store.load_plan(proposed["run_id"])
    assert sorted(committed) == sorted(step["step_id"] for step in plan["steps"])
    assert len(committed) == len(set(committed))
    for row in _all_moves(vault, proposed["run_id"]):
        fields = parse_frontmatter((vault / row["to"]).read_text(encoding="utf-8"))[0]
        assert [entry["path"] for entry in fields["moved_from"]] == [row["from"]]


def test_a_relocation_too_large_to_load_again_is_refused_before_it_seals(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 1.19 MB plan that sealed, then failed every apply as a corrupt artifact."""
    built = _relocation_vault(vault)
    monkeypatch.setattr(curation, "MAX_SEALED_BYTES", 16_384)
    runs = curation.CurationStore(vault).root
    existing = set(runs.iterdir()) if runs.exists() else set()

    with pytest.raises(curation.CurationError) as refused:
        _maintain(vault, "relocate", pages=built["pages"], to_project="meadow")

    assert refused.value.code == "CURATION_PLAN_TOO_LARGE"
    assert (set(runs.iterdir()) if runs.exists() else set()) == existing


def test_a_relocation_registers_the_folder_it_names_for_a_new_project(vault: Path) -> None:
    """A new project's pages landing in a folder derived from its key, not the one the person named."""
    built = _relocation_vault(vault)
    pages: list[str] = built["pages"][:2]  # type: ignore[index]

    proposed = _maintain(vault, "relocate", pages=pages, to_project="meadow", folder="Meadow Survey")
    result = _maintain(
        vault,
        "apply",
        run_id=proposed["run_id"],
        plan_id=proposed["plan_id"],
        expected_plan_fingerprint=proposed["plan_fingerprint"],
        why="Approved relocation.",
    )

    assert result["phase"] == "completed", result
    assert sorted(row["to"] for row in _all_moves(vault, proposed["run_id"])) == [
        f"{RESEARCH}/Meadow Survey/Tide gauge reading 1.md",
        f"{RESEARCH}/Meadow Survey/Tide gauge reading 2.md",
    ]
    registry = yaml.safe_load((vault / REGISTRY).read_text(encoding="utf-8"))
    assert registry["projects"]["meadow"]["folder"] == "Meadow Survey"


def test_a_rename_only_relocation_keeps_every_project_and_source(vault: Path) -> None:
    """A rename that touched a page's project or a cited Source, or moved a page out of its folder."""
    built = _relocation_vault(vault)
    pages: list[str] = built["pages"][:2]  # type: ignore[index]
    sources = built["sources"][:2]  # type: ignore[index]
    before = _snapshot(vault, sources)

    proposed = _maintain(vault, "relocate", pages=pages)
    result = _maintain(
        vault,
        "apply",
        run_id=proposed["run_id"],
        plan_id=proposed["plan_id"],
        expected_plan_fingerprint=proposed["plan_fingerprint"],
        why="Approved rename.",
    )

    assert result["phase"] == "completed", result
    moved = {row["from"]: row["to"] for row in _all_moves(vault, proposed["run_id"])}
    assert moved == {
        f"{RESEARCH}/Harbor/tide-note-01.md": f"{RESEARCH}/Harbor/Tide gauge reading 1.md",
        f"{RESEARCH}/Harbor/tide-note-02.md": f"{RESEARCH}/Harbor/Tide gauge reading 2.md",
    }
    for new in moved.values():
        assert parse_frontmatter((vault / new).read_text(encoding="utf-8"))[0]["project"] == "harbor"
    for rel, raw in before.items():
        fields = parse_frontmatter((vault / rel).read_text(encoding="utf-8"))[0]
        assert fields["project"] == parse_frontmatter(raw.decode())[0]["project"]


def _apply_to_the_end(vault: Path, proposed: dict[str, object]) -> dict[str, object]:
    result = _maintain(
        vault,
        "apply",
        run_id=proposed["run_id"],
        plan_id=proposed["plan_id"],
        expected_plan_fingerprint=proposed["plan_fingerprint"],
        why="Approved relocation.",
    )
    while result["phase"] == "executing":
        result = _maintain(vault, "resume", run_id=proposed["run_id"], plan_id=proposed["plan_id"])
    return result


def test_a_relocation_with_many_referrers_rewrites_them_in_bounded_disjoint_steps(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The repoint that held the vault for every referring page in one step, past the request budget.

    It catches a referring page that no step or two steps name, a step that names more pages than
    the bound, a step that seals every move of the plan, which multiplies the sealed plan by its
    steps, and an undo whose unrepoint is one unbounded step.
    """
    monkeypatch.setattr(relocate, "REFERRERS_PER_STEP", 2)
    built = _relocation_vault(vault)
    pages: list[str] = built["pages"][:6]  # type: ignore[index]
    old_links = {_link(rel).strip("[]") for rel in pages}
    linking = {
        path.relative_to(vault).as_posix()
        for path in (vault / "Knowledge Base").rglob("*.md")
        if any(link.group(1).strip() in old_links for link in find_body_wikilinks(path.read_text(encoding="utf-8")))
    }
    before = _snapshot(vault, sorted(linking | set(pages)))

    proposed = _maintain(vault, "relocate", pages=pages, to_project="meadow")

    plan = curation.CurationStore(vault).load_plan(proposed["run_id"])
    chunks = [step["args"]["referrers"] for step in plan["steps"] if step["kind"] == curation.REPOINT_STEP_KIND]
    assert len(chunks) > 1 and all(1 <= len(chunk) <= 2 for chunk in chunks)
    named = [rel for chunk in chunks for rel in chunk]
    assert len(named) == len(set(named)) and set(named) == linking
    links_of = {
        rel: {link.group(1).strip() for link in find_body_wikilinks(before[rel].decode("utf-8"))}
        for rel in linking
    }
    for step, binding in zip(plan["steps"], plan["binding_manifest"], strict=True):
        if step["kind"] == curation.REPOINT_STEP_KIND:
            used = set().union(*(links_of[rel] for rel in step["args"]["referrers"]))
            assert {old for old, _new in binding["prepared"]["moves"]} == {
                rel for rel in pages if _link(rel).strip("[]") in used
            }, "a step seals only the moves its own referring pages link to"
    assert _apply_to_the_end(vault, proposed)["phase"] == "completed"
    assert not _links_everywhere(vault) & old_links, "a link still names an old path"

    undo = _maintain(vault, "propose-compensation", run_id=proposed["run_id"])
    undo_plan = curation.CompensationStore(vault, proposed["run_id"], undo["plan_id"]).load_plan(proposed["run_id"])
    unrepoints = [step for step in undo_plan["steps"] if step["kind"] == curation.UNREPOINT_STEP_KIND]
    assert len(unrepoints) == len(chunks)
    assert all(len(step["args"]["repoint"]["pages"]) <= 2 for step in unrepoints)
    undone = _maintain(
        vault,
        "apply-compensation",
        run_id=proposed["run_id"],
        plan_id=undo["plan_id"],
        expected_plan_fingerprint=undo["plan_fingerprint"],
        why="Undo the relocation.",
    )
    while undone["phase"] not in {"compensated", "failed", "blocked", "partial"}:
        undone = _maintain(vault, "resume", run_id=proposed["run_id"], plan_id=undo["plan_id"])
    assert undone["phase"] == "compensated", undone
    assert _snapshot(vault, list(before)) == before


class _TwoStepBudget:
    """A request budget that covers two prepared steps of a call: the second check runs short."""

    def __init__(self) -> None:
        self.checks = 0

    def can_afford(self, _reserve: float) -> bool:
        self.checks += 1
        return self.checks < 2


def test_a_proposal_that_runs_short_goes_on_from_its_prepared_steps_to_the_same_seal(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relocation whose preparation outlived the request budget, now prepared over several calls.

    It catches a call that stops without keeping its steps, a later call that prepares them again,
    a call that never stops, and a plan sealed in parts that differs from the one sealed at once.
    """
    monkeypatch.setattr(relocate, "REFERRERS_PER_STEP", 2)
    built = _relocation_vault(vault)
    pages: list[str] = built["pages"][:6]  # type: ignore[index]
    unbounded = curation._call_budget
    monkeypatch.setattr(curation, "_call_budget", _TwoStepBudget)

    result = _maintain(vault, "relocate", pages=pages, to_project="meadow")
    calls, prepared = 1, 0
    while result.get("phase") == "preparing":
        assert result["next_action"] == "relocate" and result["sealed"] is False
        progress = result["progress"]
        assert 1 <= progress["this_call"] <= 2
        assert progress["prepared"] == prepared + progress["this_call"], "a kept step ran again"
        prepared = progress["prepared"]
        assert calls <= progress["steps"], "the proposal does not advance"
        result = _maintain(vault, "relocate", pages=pages, to_project="meadow")
        calls += 1
    assert result["sealed"] is True and result["next_action"] == "apply"
    assert calls > 2

    monkeypatch.setattr(curation, "_call_budget", unbounded)
    at_once = _maintain(vault, "relocate", pages=pages, to_project="meadow")
    assert at_once["plan_fingerprint"] == result["plan_fingerprint"]
    assert at_once["run_id"] == result["run_id"]


def test_a_page_that_changes_after_its_step_was_kept_refuses_the_seal(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A step kept from an earlier call, sealed against bytes its page no longer holds.

    It catches a kept step that reaches the seal after its page changed, and a refused proposal
    whose stale steps the next call keeps.
    """
    monkeypatch.setattr(relocate, "REFERRERS_PER_STEP", 2)
    built = _relocation_vault(vault)
    pages: list[str] = built["pages"][:6]  # type: ignore[index]
    unbounded = curation._call_budget
    monkeypatch.setattr(curation, "_call_budget", _TwoStepBudget)
    assert _maintain(vault, "relocate", pages=pages, to_project="meadow")["phase"] == "preparing"
    first = vault / pages[0]
    added = "A reading added between two calls."
    first.write_text(first.read_text(encoding="utf-8") + f"\n{added}\n", encoding="utf-8")
    monkeypatch.setattr(curation, "_call_budget", unbounded)

    with pytest.raises(curation.CurationError) as refused:
        _maintain(vault, "relocate", pages=pages, to_project="meadow")
    assert refused.value.code == "CURATION_BINDING_STALE"
    resealed = _maintain(vault, "relocate", pages=pages, to_project="meadow")

    assert resealed["sealed"] is True
    assert _apply_to_the_end(vault, resealed)["phase"] == "completed"
    moves = _all_moves(vault, resealed["run_id"])
    landed = next(move["to"] for move in moves if move["from"] == pages[0])
    assert added in (vault / landed).read_text(encoding="utf-8")


@pytest.mark.parametrize("folder", ["Survey/2026", "Survey: field"])
def test_a_relocation_refuses_a_folder_that_is_not_one_portable_name(vault: Path, folder: str) -> None:
    """A folder that a path separator splits, or that YAML reads as a mapping, written into the registry."""
    built = _relocation_vault(vault)
    registry = (vault / REGISTRY).read_bytes()

    with pytest.raises(curation.CurationError) as refused:
        _maintain(vault, "relocate", pages=built["pages"][:1], to_project="meadow", folder=folder)  # type: ignore[index]

    assert refused.value.code == "INVALID_RELOCATION"
    assert (vault / REGISTRY).read_bytes() == registry


def test_a_new_project_folder_that_yaml_would_read_as_a_number_stays_a_name(vault: Path) -> None:
    """A folder written as a bare scalar: the registry then reads `2026` back as a number, not a folder."""
    built = _relocation_vault(vault)

    proposed = _maintain(vault, "relocate", pages=built["pages"][:1], to_project="meadow", folder="2026")  # type: ignore[index]

    assert _apply_to_the_end(vault, proposed)["phase"] == "completed"
    registry = yaml.safe_load((vault / REGISTRY).read_text(encoding="utf-8"))
    assert registry["projects"]["meadow"]["folder"] == "2026"


def test_a_source_cited_by_its_earlier_path_keeps_the_old_project(vault: Path) -> None:
    """A Source that moved before: a page that still cites its old path was not counted as a citer."""
    built = _relocation_vault(vault)
    source = built["sources"][2]  # type: ignore[index]  # cited only by tide-note-03
    old = f"{ARTICLES}/2026-02-01-old-capture.md"
    text = (vault / source).read_text(encoding="utf-8")
    (vault / source).write_text(
        text.replace("---\ntype: source", f"---\nmoved_from:\n  - path: {old}\ntype: source", 1),
        encoding="utf-8",
    )
    _page(vault, f"{RESEARCH}/Harbor/Harbor archive.md", "research-note", "Harbor archive", "project: harbor", [old])

    proposed = _maintain(vault, "relocate", pages=[f"{RESEARCH}/Harbor/tide-note-03.md"], to_project="meadow")

    summary = proposed["summary"]
    assert [row["path"] for row in summary["sources_keeping_old_key"]["shown"]] == [source]
    assert summary["sources_losing_key"]["count"] == 0


def test_a_rename_never_takes_the_title_another_live_page_holds(vault: Path) -> None:
    """A page renamed to a stem that another page holds as its title takes over that page's bare links."""
    built = _relocation_vault(vault)
    _page(vault, f"{INSIGHTS}/gauge-digest.md", "insight", "Tide gauge reading 1", "project: harbor", [])

    proposed = _maintain(vault, "relocate", pages=built["pages"][:1], to_project="meadow")  # type: ignore[index]

    (row,) = proposed["summary"]["moves"]["shown"]
    assert row["to"].startswith(f"{RESEARCH}/Meadow/")
    assert row["to"] != f"{RESEARCH}/Meadow/Tide gauge reading 1.md"


def test_pages_named_by_memory_ref_resolve_over_one_identity_census(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each memory ref resolved by its own whole-vault scan: a relocation of many refs scanned per ref."""
    built = _relocation_vault(vault)
    pages: list[str] = built["pages"][:6]  # type: ignore[index]
    refs = [
        f"exomem://memory/{parse_frontmatter((vault / rel).read_text(encoding='utf-8'))[0]['exomem_id']}"
        for rel in pages
    ]
    real_scan = memory_refs._scan_pages  # noqa: SLF001
    scans: list[int] = []

    def counted(*args: object, **kwargs: object) -> object:
        scans.append(1)
        return real_scan(*args, **kwargs)

    monkeypatch.setattr(memory_refs, "_scan_pages", counted)
    by_path = _maintain(vault, "relocate", pages=pages, to_project="meadow")
    path_scans = len(scans)
    scans.clear()
    by_ref = _maintain(vault, "relocate", pages=refs, to_project="meadow")

    assert by_ref["summary"]["moves"] == by_path["summary"]["moves"]
    assert len(scans) <= path_scans + 1, (len(scans), path_scans)


def test_a_list_field_that_gains_a_project_stays_a_list(vault: Path) -> None:
    """A page with an empty `projects` field gained the key as a bare string in a list field."""
    _write(vault, REGISTRY, "projects:\n  harbor:\n    folder: Harbor\n    category: product\n")
    rel = _page(vault, f"{CONCEPTS}/Mooring Line.md", "entity", "Mooring Line", "entity_type: concept\nprojects:", [])

    proposed = _maintain(vault, "relocate", pages=[rel], to_project="harbor")

    assert _apply_to_the_end(vault, proposed)["phase"] == "completed"
    assert parse_frontmatter((vault / rel).read_text(encoding="utf-8"))[0]["projects"] == ["harbor"]


def test_a_source_its_leaf_refuses_is_listed_and_the_relocation_goes_on(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One Source whose correction its leaf refused failed the whole relocation after three retries."""
    built = _relocation_vault(vault)
    pages: list[str] = built["pages"][:3]  # type: ignore[index]
    refused_source = built["sources"][1]  # type: ignore[index]
    real_prepare = curation._prepare_step  # noqa: SLF001

    def refuse_one_source(vault_root: Path, step: dict[str, object], *args: object, **kwargs: object) -> object:
        if step["args"].get("path") == refused_source:  # type: ignore[union-attr]
            raise curation.CurationError("SOURCE_WRITE_REFUSED", "the Source cannot be corrected now")
        return real_prepare(vault_root, step, *args, **kwargs)

    monkeypatch.setattr(curation, "_prepare_step", refuse_one_source)
    proposed = _maintain(vault, "relocate", pages=pages, to_project="meadow")

    summary = proposed["summary"]
    assert proposed["sealed"] is True
    assert [(row["page"], row["code"]) for row in summary["refused"]["shown"]] == [
        (refused_source, "SOURCE_WRITE_REFUSED")
    ]
    assert summary["moves"]["count"] == 3
    gaining = summary["sources_gaining_key"]["shown"]
    assert refused_source not in gaining and len(gaining) == 2


def test_a_plan_that_names_one_referrer_in_two_link_rewrites_is_refused(vault: Path) -> None:
    """Two repoint steps that both rewrite one page: the second binds bytes the first already changed."""
    built = _relocation_vault(vault)
    page = built["pages"][0]  # type: ignore[index]
    overview = built["overview"]
    plan = {
        "version": 1,
        "title": "Move one page",
        "steps": [
            {"step_id": "move-1", "kind": "move", "args": {"old_path": page, "new_path": f"{RESEARCH}/Harbor/moved.md"}},
            {"step_id": "repoint-1", "kind": curation.REPOINT_STEP_KIND, "args": {"referrers": [overview]}},
            {"step_id": "repoint-2", "kind": curation.REPOINT_STEP_KIND, "args": {"referrers": [overview.upper()]}},
        ],
    }

    with pytest.raises(curation.CurationError) as refused:
        _maintain(vault, "propose", plan=plan)

    assert refused.value.code == "CURATION_STEP_INVALID"
