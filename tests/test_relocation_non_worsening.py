"""A relocation refuses a page only for the contract errors the relocation itself adds
(OpenSpec judge-relocations-by-what-they-change).

A move changes where a page lives and how links name it, never what a page says. A vault's older
pages carry debt from before the contract: no semantic units, an identity that is still their
path. The pages that link to them carry debt of their own. Judging each moved or dependent page in
full refused every such relocation for debt the move did not create.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from exomem import (
    activation_manifest,
    commands,
    move_file,
    semantic_contract,
    semantic_writes,
    writer_lease,
)
from exomem.vault import parse_frontmatter

INSIGHTS = "Knowledge Base/Notes/Insights"
MAINTAIN = next(c for c in commands.PRODUCT_COMMANDS if c.name == "maintain_memory")


def _write(vault: Path, rel: str, text: str) -> str:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return rel


def _older_page(vault: Path, rel: str, title: str) -> str:
    """A page from before the contract: no exomem_id, so its identity is its path, and no units."""
    return _write(
        vault, rel,
        f"---\ntitle: {title}\ntype: insight\nstatus: active\ncreated: 2025-02-01\n---\n\n"
        f"# {title}\n\nNotes from the {title.lower()} dive.\n",
    )


def _referrer(vault: Path, rel: str, title: str, targets: list[str]) -> str:
    """A page written outside the governed tools after activation: it links the older pages and
    has no semantic units, so its own debt is not grandfathered."""
    links = "".join(f"- supports [[{target.removeprefix('Knowledge Base/').removesuffix('.md')}]]\n"
                    for target in targets)
    return _write(
        vault, rel,
        f"---\nexomem_id: {uuid.uuid4()}\ntitle: {title}\ntype: insight\nstatus: active\n"
        f"created: 2026-04-01\n---\n\n# {title}\n\nA summary without semantic units.\n\n"
        f"## Relations\n{links}",
    )


def _maintain(vault: Path, action: str, **arguments: object) -> dict[str, object]:
    return writer_lease.invoke_command(
        MAINTAIN, vault, mode="curation", curation_action=action, **arguments
    )


def _run(
    vault: Path, run_id: str, plan_id: str, first: dict[str, object], done: set[str]
) -> dict[str, object]:
    result = first
    while result["phase"] not in done | {"failed", "blocked", "partial", "stopped"}:
        result = _maintain(vault, "resume", run_id=run_id, plan_id=plan_id)
    return result


@pytest.mark.timeout(300)
def test_a_rename_of_older_pages_moves_them_and_refuses_only_the_page_it_gives_a_new_error(
    vault: Path, corpus_cache_on: None
) -> None:
    """The rename that refused every older page: each moved page and each page linking it was
    judged in full, so debt from before the contract blocked the move. It catches a relocation
    that refuses a page for an error key its before state already had, one that lets through a
    page the move gives a new error key, and an undo that its forward move's rule cannot pass."""
    older = [
        _older_page(vault, f"{INSIGHTS}/note-{index:02d}.md", f"Kelp survey {index}")
        for index in range(1, 6)
    ]
    # Its name ends in a breadth suffix that exempts it from the semantic minimum; the name its
    # title gives does not, so the rename adds a missing-unit error the page never had.
    breadth = _older_page(vault, f"{INSIGHTS}/reef-architecture.md", "Reef layout")
    corpus = semantic_contract.build_corpus_context(vault)
    activation_manifest.ensure_manifest(vault, census=corpus.activation_census)
    referrers = [
        _referrer(vault, f"{INSIGHTS}/dive-log-{index}.md", f"Dive log {index}", older[index::2])
        for index in range(2)
    ]
    before = {rel: (vault / rel).read_bytes() for rel in [*older, breadth, *referrers]}

    proposed = _maintain(vault, "relocate", pages=[*older, breadth])

    summary = proposed["summary"]
    assert proposed["sealed"] is True, proposed
    assert [(row["page"], row["code"]) for row in summary["refused"]["shown"]] == [
        (breadth, "SEMANTIC_CONTRACT_BLOCKED")
    ]
    assert summary["moves"]["count"] == len(older)
    applied = _run(vault, proposed["run_id"], proposed["plan_id"], _maintain(
        vault, "apply", run_id=proposed["run_id"], plan_id=proposed["plan_id"],
        expected_plan_fingerprint=proposed["plan_fingerprint"], why="Approved rename.",
    ), {"completed"})
    assert applied["phase"] == "completed", applied
    for index, rel in enumerate(older, start=1):
        moved = f"{INSIGHTS}/Kelp survey {index}.md"
        assert not (vault / rel).exists()
        front = parse_frontmatter((vault / moved).read_text(encoding="utf-8"))[0]
        assert [entry["path"] for entry in front["moved_from"]] == [rel]
    assert (vault / breadth).read_bytes() == before[breadth]

    undo = _maintain(vault, "propose-compensation", run_id=proposed["run_id"])
    undone = _run(vault, proposed["run_id"], undo["plan_id"], _maintain(
        vault, "apply-compensation", run_id=proposed["run_id"], plan_id=undo["plan_id"],
        expected_plan_fingerprint=undo["plan_fingerprint"], why="Undo the rename.",
    ), {"compensated"})
    assert undone["phase"] == "compensated", undone
    assert {rel: (vault / rel).read_bytes() for rel in before} == before


def test_an_older_page_keeps_its_baseline_through_its_moves_until_its_bytes_change(
    tmp_path: Path,
) -> None:
    """An older page is in the activation baseline by its path and bytes, and a move changes both.
    Without following its lineage, a page that a relocation renamed loses its baseline, and its
    next edit must clear every debt it carried. It catches that loss, through two moves, in the
    edit path and the audit, and a baseline that outlives a change to what the page says."""
    names = ("note-01", "kelp-survey", "Kelp survey")
    first, second, third = (f"{INSIGHTS}/{name}.md" for name in names)
    _older_page(tmp_path, first, "Kelp survey")
    activation_manifest.ensure_manifest(tmp_path)
    move_file.move_file(tmp_path, old_path=first, new_path=second, update_wikilinks=False)
    move_file.move_file(tmp_path, old_path=second, new_path=third, update_wikilinks=False)
    moved = (tmp_path / third).read_text(encoding="utf-8")

    edit = semantic_writes.preflight_existing(
        tmp_path, path=third, operation="edit",
        after_source=moved.replace("Notes from", "Field notes from"),
    )
    audit = semantic_writes.evaluate_posthoc_batch(tmp_path, paths=[third], operation="audit")

    assert edit.grandfathered is True and edit.contract_result.should_block is False
    assert [item.grandfathered for item in audit.evaluations] == [True]
    semantic_writes.commit_existing(tmp_path, preflight=edit)
    edited = (tmp_path / third).read_text(encoding="utf-8")
    again = semantic_writes.preflight_existing(
        tmp_path, path=third, operation="edit",
        after_source=edited.replace("Field notes", "Dive notes"),
    )
    assert again.grandfathered is False and again.contract_result.should_block is True
