"""Lossless collection adoption: links, folder rules, re-import, artifacts, bounded calls, coverage."""

from __future__ import annotations

import datetime as dt
import hashlib
import itertools
import os
import types
from pathlib import Path

import pytest

from exomem import (
    adopt,
    adoption_plan,
    adoption_run,
    capabilities,
    epistemic_graph,
    find,
    held_fs,
    indexes,
    media_jobs,
    request_budget,
    reserved_paths,
    source_taxonomy,
    writer_lease,
)
from exomem import (
    vault as vault_module,
)
from exomem.vault import parse_frontmatter

TODAY = dt.date(2026, 7, 14)
SOURCES = "Knowledge Base/Sources"


def _vault(tmp_path: Path, files: dict[str, str | bytes]) -> Path:
    vault = tmp_path / "vault"
    kb = vault / "Knowledge Base"
    (kb / "Notes").mkdir(parents=True)
    (kb / "Sources").mkdir()
    (kb / "Sources" / "index.md").write_text(
        "# Sources - Index\n\n## By type\n\n## Recent captures\n\n", encoding="utf-8"
    )
    (kb / "index.md").write_text(
        "# Knowledge Base\n\n## Counts\n\n- Sources: 0\n\n## Recent activity\n\n", encoding="utf-8"
    )
    (kb / "log.md").write_text("# Log\n\n---\n", encoding="utf-8")
    for rel, data in files.items():
        path = vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    return vault


def _import(vault: Path, *, path: str, include: list) -> tuple[str, dict]:
    """start + select + plan (following every continuation); returns the run id and final plan call."""
    run = adoption_run.start(vault, path=path, today=TODAY)
    run_id = run["run_id"]
    adoption_run.select(vault, run_id=run_id, include=include)
    planned = adoption_run.plan(vault, run_id=run_id, today=TODAY)
    while planned.get("continuation"):
        planned = adoption_run.plan(
            vault, run_id=run_id, continuation=planned["continuation"], today=TODAY
        )
    return run_id, planned


def _apply_from(vault: Path, run_id: str, plan_id: str, last: dict) -> tuple[dict, int]:
    """Carry on from `last` until no continuation is left; returns the final call and the calls made."""
    calls = 0
    while last.get("continuation"):
        calls += 1
        last = adoption_run.apply(
            vault, run_id=run_id, plan_id=plan_id, continuation=last["continuation"], today=TODAY
        )
    return last, calls


def _apply_all(vault: Path, run_id: str, plan_id: str) -> tuple[dict, int]:
    first = adoption_run.apply(vault, run_id=run_id, plan_id=plan_id, today=TODAY)
    last, calls = _apply_from(vault, run_id, plan_id, first)
    return last, calls + 1


def _page(vault: Path, rel: str) -> tuple[dict, str]:
    frontmatter, body, _raw = parse_frontmatter((vault / rel).read_text(encoding="utf-8"))
    return frontmatter, body


@pytest.fixture
def tiny_budget(monkeypatch: pytest.MonkeyPatch):
    """A request budget below the delivery reserve: every call does one unit of work."""
    monkeypatch.setenv(request_budget.BUDGET_ENV_VAR, "1")
    request_budget.reset_resolution_cache()
    yield
    monkeypatch.delenv(request_budget.BUDGET_ENV_VAR)
    request_budget.reset_resolution_cache()


# --- 2.1: links ---------------------------------------------------------------
def test_links_resolve_by_path_basename_alias_and_embed_and_leave_a_shared_basename_unresolved(
    tmp_path: Path,
) -> None:
    vault = _vault(
        tmp_path,
        {
            "Coll/index.md": (
                "# Index\n\n[[projects/alpha]] [[beta]] [[The Gamma]] ![[img.png]]\n"
                "[[shared]] [[nowhere]] [relative](projects/alpha.md)\n"
            ),
            "Coll/projects/alpha.md": "# Alpha\n",
            "Coll/deep/beta.md": "# Beta\n",
            "Coll/gamma.md": "---\naliases: [The Gamma]\n---\n# Gamma\n",
            "Coll/assets/img.png": b"\x89PNG\r\n\x1a\n" + b"pixels",
            "Coll/one/shared.md": "# Shared one\n",
            "Coll/two/shared.md": "# Shared two\n",
        },
    )
    run_id, planned = _import(vault, path="Coll", include=["Coll"])
    items = {item["collection_path"]: item for item in planned["plan"]["items"]}
    links = items["index.md"]["links"]
    resolved = {link["raw"]: (link["via"], link["target_path"]) for link in links["resolved"]}

    assert resolved["projects/alpha"] == ("path", items["projects/alpha.md"]["target_path"])
    assert resolved["beta"] == ("basename", items["deep/beta.md"]["target_path"])
    assert resolved["The Gamma"] == ("alias", items["gamma.md"]["target_path"])
    assert resolved["img.png"] == ("basename", items["assets/img.png"]["target_path"])
    assert resolved["projects/alpha.md"][0] == "path"
    unresolved = {link["raw"]: link["reason"] for link in links["unresolved"]}
    assert unresolved == {"shared": "ambiguous", "nowhere": "not-found"}


# --- 2.2: folder rules --------------------------------------------------------
def test_a_ruled_folder_lands_in_its_kind_directory_and_an_unregistered_kind_changes_nothing(
    tmp_path: Path,
) -> None:
    vault = _vault(
        tmp_path,
        {"Coll/books/dune.md": "# Dune\n", "Coll/loose.md": "# Loose\n"},
    )
    run = adoption_run.start(vault, path="Coll", today=TODAY)
    run_id = run["run_id"]
    before = adoption_run.select(vault, run_id=run_id, include=["Coll"])

    with pytest.raises(adoption_run.AdoptionRunError) as refused:
        adoption_run.select(
            vault,
            run_id=run_id,
            include=[{"path": "Coll/books", "source_kind": "no-such-kind"}, "Coll"],
        )
    assert refused.value.code.startswith("SOURCE_KIND_")
    # The refusal offers the safe route first and gates registering on the person.
    assert "without source_kind" in str(refused.value) and "after they confirm" in str(refused.value)
    assert adoption_run.status(vault, run_id=run_id)["selection"] == before["selection"]

    ruled = adoption_run.select(
        vault,
        run_id=run_id,
        include=[{"path": "Coll/books", "source_kind": "book", "domain": "media"}, "Coll"],
    )
    assert ruled["selection"]["selection_hash"] != before["selection"]["selection_hash"]
    planned = adoption_run.plan(vault, run_id=run_id, today=TODAY)
    applied = adoption_run.apply(
        vault, run_id=run_id, plan_id=planned["plan"]["plan_id"], today=TODAY
    )

    taxonomy = source_taxonomy.load_taxonomy(vault)
    home = source_taxonomy.source_directory(
        vault, taxonomy.resolve_kind("book"), taxonomy.resolve_domain("media")
    ).relative_to(vault)
    dune = applied["outcomes"]["Coll/books/dune.md"]["target_path"]
    loose = applied["outcomes"]["Coll/loose.md"]["target_path"]
    assert Path(dune).parent == home
    assert Path(loose).parent == Path(SOURCES) / "Imported"
    frontmatter, _body = _page(vault, dune)
    assert (frontmatter["source_type"], frontmatter["domain"]) == ("book", "media")


# --- 2.3: re-import -------------------------------------------------------------
def test_a_re_staged_collection_plans_existing_refs_and_a_changed_note_names_its_predecessor(
    tmp_path: Path,
) -> None:
    vault = _vault(
        tmp_path,
        {
            "_Imports/first/Coll/a.md": "# A\n\nstays\n",
            "_Imports/first/Coll/b.md": "# B\n\nold text\n",
        },
    )
    run_id, planned = _import(vault, path="_Imports/first", include=["_Imports/first"])
    assert planned["collection"]["label"] == "Coll"
    first, _calls = _apply_all(vault, run_id, planned["plan"]["plan_id"])
    first_b = first["outcomes"]["_Imports/first/Coll/b.md"]["source_ref"]
    pages_before = sorted(p.name for p in (vault / SOURCES / "Imported").glob("*.md"))

    (vault / "_Imports/second/Coll").mkdir(parents=True)
    (vault / "_Imports/second/Coll/a.md").write_text("# A\n\nstays\n", encoding="utf-8")
    (vault / "_Imports/second/Coll/b.md").write_text("# B\n\nnew text\n", encoding="utf-8")
    run_id, planned = _import(vault, path="_Imports/second", include=["_Imports/second"])
    items = {item["collection_path"]: item for item in planned["plan"]["items"]}

    assert items["a.md"]["action"] == "already-imported"
    assert items["a.md"]["existing_ref"].startswith("exomem://")
    assert items["b.md"]["action"] == "copy-as-source"
    assert items["b.md"]["previous_import"] == first_b
    second, _calls = _apply_all(vault, run_id, planned["plan"]["plan_id"])
    assert second["outcomes"]["_Imports/second/Coll/a.md"]["status"] == "already-applied"
    pages_after = sorted(p.name for p in (vault / SOURCES / "Imported").glob("*.md"))
    assert len(pages_after) == len(pages_before) + 1


# --- 3.1: artifacts -------------------------------------------------------------
def test_pdf_image_and_non_utf8_note_import_as_artifacts_with_original_bytes_and_extraction_requests(
    tmp_path: Path,
) -> None:
    pdf = b"%PDF-1.4\n1 0 obj\n<< >>\nendobj\n%%EOF\n"
    image = b"\x89PNG\r\n\x1a\n" + b"pixels" * 20
    latin1 = "# Café\n\nnaïve\n".encode("latin-1")
    vault = _vault(
        tmp_path, {"Coll/paper.pdf": pdf, "Coll/pic.png": image, "Coll/old.md": latin1}
    )
    run_id, planned = _import(vault, path="Coll", include=["Coll"])
    applied, _calls = _apply_all(vault, run_id, planned["plan"]["plan_id"])

    jobs = media_jobs.MediaJobStore(vault)
    for original, data in (
        ("Coll/paper.pdf", pdf),
        ("Coll/pic.png", image),
        ("Coll/old.md", latin1),
    ):
        outcome = applied["outcomes"][original]
        assert outcome["status"] == "applied"
        assert (vault / outcome["artifact_path"]).read_bytes() == data
        assert (vault / original).read_bytes() == data
        frontmatter, _body = _page(vault, outcome["target_path"])
        assert frontmatter["imported_from"] == original
        assert frontmatter["original_sha256"] == hashlib.sha256(data).hexdigest()
    assert jobs.get_by_binary(vault / applied["outcomes"]["Coll/paper.pdf"]["artifact_path"])
    assert jobs.get_by_binary(vault / applied["outcomes"]["Coll/pic.png"]["artifact_path"])


# --- 3.2: fence and graph -------------------------------------------------------
def test_the_fence_is_verbatim_and_a_link_becomes_a_graph_edge_to_the_imported_target(
    tmp_path: Path,
) -> None:
    text = "# Source note\n\nSee [[target]].  \n\n\n"  # trailing spaces and blank lines
    vault = _vault(tmp_path, {"Coll/source.md": text, "Coll/target.md": "# Target\n"})
    run_id, planned = _import(vault, path="Coll", include=["Coll"])
    applied, _calls = _apply_all(vault, run_id, planned["plan"]["plan_id"])
    source = applied["outcomes"]["Coll/source.md"]
    target = applied["outcomes"]["Coll/target.md"]

    page = (vault / source["target_path"]).read_text(encoding="utf-8")
    fenced = page.split("## Capture\n\n", 1)[1]
    fence = fenced.split("\n", 1)[0]
    captured = fenced[len(fence) + 1 : fenced.rindex(f"\n{fence}")]
    assert hashlib.sha256(captured.encode("utf-8")).hexdigest() == source["sha256"]
    assert captured == text

    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()
    assert any(
        edge["relation_type"] == "links_to"
        and edge["dst_key"] == epistemic_graph._file_key(target["target_path"])
        for edge in index.edges(source_path=source["target_path"])
    )


# --- 3.3: bounded calls ---------------------------------------------------------
def test_a_tiny_budget_needs_several_calls_writes_each_item_once_and_replays_a_repeated_cursor(
    tmp_path: Path, tiny_budget: None
) -> None:
    files = {f"Coll/note-{n}.md": f"# Note {n}\n\ntext {n}\n" for n in range(4)}
    vault = _vault(tmp_path, files)
    originals = {rel: (vault / rel).read_bytes() for rel in files}
    run_id, planned = _import(vault, path="Coll", include=["Coll"])
    plan_id = planned["plan"]["plan_id"]

    first = adoption_run.apply(vault, run_id=run_id, plan_id=plan_id, today=TODAY)
    assert first["phase"] == "applying"
    assert first["progress"] == {"applied": 1, "failed": 0, "remaining": 3, "total": 4}
    (resume,) = first["next_actions"]  # an agent that stops here must report the counts, not "underway"
    assert "background worker" in resume["description"] and "unfinished" in resume["description"]
    mid = adoption_run.status(vault, run_id=run_id)
    assert mid["interrupted"] is False
    assert mid["remaining"] == 3
    assert mid["continuation"] == first["continuation"]

    second = adoption_run.apply(
        vault, run_id=run_id, plan_id=plan_id, continuation=first["continuation"], today=TODAY
    )
    before_replay = sorted(p for p in (vault / "Knowledge Base").rglob("*") if p.is_file())
    snapshot = {p: p.read_bytes() for p in before_replay}
    replay = adoption_run.apply(
        vault, run_id=run_id, plan_id=plan_id, continuation=first["continuation"], today=TODAY
    )
    assert replay["progress"] == second["progress"]
    assert replay["continuation"] == second["continuation"] != first["continuation"]
    assert {p: p.read_bytes() for p in before_replay} == snapshot
    assert sorted(p for p in (vault / "Knowledge Base").rglob("*") if p.is_file()) == before_replay

    final, calls = _apply_from(vault, run_id, plan_id, second)
    assert calls == 2
    assert final["phase"] == "applied"
    pages = sorted((vault / SOURCES / "Imported").glob("*.md"))
    assert len(pages) == len(files)
    assert {o["status"] for o in final["outcomes"].values()} == {"applied"}
    assert {rel: (vault / rel).read_bytes() for rel in files} == originals


def test_no_apply_call_runs_past_its_budget_minus_the_delivery_reserve_when_chunk_cost_swings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cheap chunk must not license a big one: every call ends inside budget minus the reserve."""
    clock = {"now": 1000.0}
    fake_time = types.SimpleNamespace(monotonic=lambda: clock["now"])
    monkeypatch.setattr(adoption_run, "time", fake_time)
    monkeypatch.setattr(request_budget, "time", fake_time)
    monkeypatch.setattr(request_budget, "DELIVERY_RESERVE_SECONDS", 2.0)
    monkeypatch.setenv(request_budget.BUDGET_ENV_VAR, "10")
    request_budget.reset_resolution_cache()
    monkeypatch.setattr(adoption_run, "APPLY_CHUNK_ITEMS", 10)
    seconds_per_item = itertools.cycle([0.05, 0.4])  # cheap chunk, then one eight times dearer
    real_apply_chunk = adoption_run._apply_chunk

    def costly_chunk(*args, **kwargs):
        result = real_apply_chunk(*args, **kwargs)
        clock["now"] += next(seconds_per_item) * len(args[3])
        return result

    monkeypatch.setattr(adoption_run, "_apply_chunk", costly_chunk)
    files = {f"Coll/note-{n:03d}.md": f"# Note {n}\n\ntext {n}\n" for n in range(60)}
    vault = _vault(tmp_path, files)
    run_id, planned = _import(vault, path="Coll", include=["Coll"])
    plan_id = planned["plan"]["plan_id"]

    elapsed: list[float] = []
    last: dict = {}
    while not elapsed or last.get("continuation"):
        began = clock["now"]
        last = adoption_run.apply(
            vault, run_id=run_id, plan_id=plan_id, continuation=last.get("continuation"),
            today=TODAY,
        )
        elapsed.append(clock["now"] - began)

    assert max(elapsed) <= 10.0 - 2.0
    assert len(elapsed) >= 2
    assert last["progress"] == {"applied": 60, "failed": 0, "remaining": 0, "total": 60}
    assert len(list((vault / SOURCES / "Imported").glob("*.md"))) == 60


def test_a_frozen_destination_taken_by_another_writer_fails_without_moving(tmp_path: Path) -> None:
    vault = _vault(tmp_path, {"Coll/a.md": "# A\n", "Coll/b.md": "# B\n"})
    run_id, planned = _import(vault, path="Coll", include=["Coll"])
    items = {item["collection_path"]: item for item in planned["plan"]["items"]}
    squatter = vault / items["a.md"]["target_path"]
    squatter.parent.mkdir(parents=True, exist_ok=True)
    squatter.write_text("someone else's page\n", encoding="utf-8")

    applied = adoption_run.apply(
        vault, run_id=run_id, plan_id=planned["plan"]["plan_id"], today=TODAY
    )

    assert applied["outcomes"]["Coll/a.md"]["code"] == "TARGET_OCCUPIED"
    assert applied["outcomes"]["Coll/b.md"]["status"] == "applied"
    assert squatter.read_text(encoding="utf-8") == "someone else's page\n"
    assert applied["phase"] == "partial"


def test_a_folder_include_gives_every_file_a_page_or_a_code(tmp_path: Path) -> None:
    vault = _vault(
        tmp_path,
        {"Coll/note.md": "# Note\n", "Coll/archive.zip": b"PK\x03\x04 bytes", "Coll/empty.md": ""},
    )
    (vault / "Coll/link.md").symlink_to(vault / "Coll/note.md")
    run = adoption_run.start(vault, path="Coll", today=TODAY)
    selected = adoption_run.select(vault, run_id=run["run_id"], include=["Coll"])

    assert set(selected["selection"]["paths"]) == {"Coll/note.md", "Coll/archive.zip"}
    refused = {row["path"]: row["code"] for row in selected["rejected"]}
    assert refused == {"Coll/link.md": "NOT_REGULAR_FILE", "Coll/empty.md": "JUNK"}


# --- 3.4: coverage --------------------------------------------------------------
def test_finish_counts_a_pending_image_an_unresolved_link_and_a_hidden_folder(tmp_path: Path) -> None:
    vault = _vault(
        tmp_path,
        {
            "Coll/note.md": "# Note\n\nSee [[missing page]].\n",
            "Coll/pic.png": b"\x89PNG\r\n\x1a\n" + b"pixels",
            "Coll/.obsidian/app.json": "{}",
            "Coll/.obsidian/theme.css": "body {}",
        },
    )
    run_id, planned = _import(vault, path="Coll", include=["Coll"])
    applied, _calls = _apply_all(vault, run_id, planned["plan"]["plan_id"])
    assert applied["phase"] == "applied"

    finished = adoption_run.finish(vault, run_id=run_id, today=TODAY)
    coverage = finished["coverage"]

    assert coverage["not_imported"]["hidden_folders"] == {"folders": 1, "files": 2}
    assert coverage["links"] == {"resolved": 0, "unresolved": 1}
    assert coverage["extraction"] == {"pending": 1}
    assert coverage["outcomes"]["by_status"] == {"applied": 2}
    assert finished["finish"]["coverage"] == coverage


def test_recall_check_accepts_only_the_exact_target_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _vault(tmp_path, {"Coll/one/plan.md": "# Plan\n\ntext\n"})
    run_id, planned = _import(vault, path="Coll", include=["Coll"])
    applied, _calls = _apply_all(vault, run_id, planned["plan"]["plan_id"])
    target = applied["outcomes"]["Coll/one/plan.md"]["target_path"]

    class Hit:
        def __init__(self, path: str) -> None:
            self.path = path

    # A page elsewhere that shares the target's file name is not the imported note.
    elsewhere = f"Knowledge Base/Notes/{Path(target).name}"
    monkeypatch.setattr(find, "find", lambda *a, **k: [Hit(elsewhere)])
    wrong = adoption_run.finish(vault, run_id=run_id, today=TODAY)
    assert wrong["finish"]["recall_check"]["ok"] is False
    assert wrong["coverage"]["recall"] == {"sampled": 1, "found": 0, "missed": [target]}
    # The search answered without the page, so a question about it is not ready.
    assert "first_question" not in wrong["finish"]
    assert wrong["finish"]["indexing"]["state"] == "missed"


def test_a_finished_run_names_the_reconcile_tool_of_the_surface_that_reads_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _vault(tmp_path, {"Coll/one/plan.md": "# Plan\n\ntext\n"})
    run_id, planned = _import(vault, path="Coll", include=["Coll"])
    applied, _calls = _apply_all(vault, run_id, planned["plan"]["plan_id"])
    target = applied["outcomes"]["Coll/one/plan.md"]["target_path"]

    class Hit:
        def __init__(self, path: str) -> None:
            self.path = path

    # The search answers without the imported page, so finish names a reconcile.
    monkeypatch.setattr(find, "find", lambda *a, **k: [Hit(f"Knowledge Base/Notes/{Path(target).name}")])
    nested = capabilities.ActiveSurfaceDescriptor(
        surface="mcp", profile="product", tier2_enabled=True, product_commands=(), tool_shape="nested"
    )
    with capabilities.active_surface(nested):
        finished = adoption_run.finish(vault, run_id=run_id, today=TODAY)
    read_later = adoption_run.status(vault, run_id=run_id)

    assert finished["finish"]["recall_check"]["next_action"] == {
        "tool": "repair_memory", "args": {"operation": {"kind": "reconcile"}},
    }
    assert read_later["finish"]["recall_check"]["next_action"] == {
        "tool": "maintain_memory", "args": {"mode": "reconcile"},
    }


def test_a_stored_route_that_no_longer_spells_still_lets_the_run_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _vault(tmp_path, {"Coll/one/plan.md": "# Plan\n\ntext\n"})
    run_id, planned = _import(vault, path="Coll", include=["Coll"])
    applied, _calls = _apply_all(vault, run_id, planned["plan"]["plan_id"])
    target = applied["outcomes"]["Coll/one/plan.md"]["target_path"]

    class Hit:
        def __init__(self, path: str) -> None:
            self.path = path

    monkeypatch.setattr(find, "find", lambda *a, **k: [Hit(f"Knowledge Base/Notes/{Path(target).name}")])
    adoption_run.finish(vault, run_id=run_id, today=TODAY)
    # A later release retires the operation that the stored route names.
    store = adoption_run.AdoptionRunStore(vault)
    run = store.load(run_id)
    retired = {"tool": "maintain_memory", "args": {"mode": "retired-mode"}}
    run["finish"]["recall_check"]["next_action"] = retired
    store.save(run)
    nested = capabilities.ActiveSurfaceDescriptor(
        surface="mcp", profile="product", tier2_enabled=True, product_commands=(), tool_shape="nested"
    )

    with capabilities.active_surface(nested):
        read = adoption_run.status(vault, run_id=run_id)

    assert read["phase"] == "done"
    assert read["finish"]["recall_check"]["next_action"] == retired


@pytest.mark.parametrize("read_state", ["pending", "failed"])
def test_finish_does_not_report_incomplete_recall_as_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, read_state: str
) -> None:
    vault = _vault(tmp_path, {"Coll/plan.md": "# Plan\n\nRetain this plan.\n"})
    run_id, planned = _import(vault, path="Coll", include=["Coll"])
    _apply_all(vault, run_id, planned["plan"]["plan_id"])

    def incomplete_find(*_args, degraded_out=None, **_kwargs):  # noqa: ANN001, ANN002, ANN003
        if read_state == "failed":
            raise OSError("the recall store is unavailable")
        if degraded_out is not None:
            degraded_out.append("pending_visibility")
        return []

    monkeypatch.setattr(find, "find", incomplete_find)
    finished = adoption_run.finish(vault, run_id=run_id, today=TODAY)
    recall = finished["finish"]["recall_check"]
    sample = finished["coverage"]["recall"]

    assert recall["ok"] is None
    assert recall["status"] == read_state
    assert "next_action" not in recall
    assert sample["status"] == read_state
    assert sample["sampled"] == 1 and sample["evaluated"] == 0
    assert sample["found"] is None and sample["missed"] == []
    assert sample[read_state] == 1
    manifest = (vault / finished["finish"]["manifest_path"]).read_text(encoding="utf-8")
    assert f"- Recall check: {read_state}" in manifest
    assert "- Recall check: needs attention" not in manifest
    if read_state == "pending":
        assert recall["warming"]["components"] == ["pending_visibility"]


def test_finish_offers_no_first_question_while_the_imported_pages_are_not_yet_searchable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _vault(tmp_path, {
        "Coll/alpha.md": "# Alpha\n\nAn alpha observation.\n",
        "Coll/beta.md": "# Beta\n\nA beta observation.\n",
    })
    run_id, planned = _import(vault, path="Coll", include=["Coll"])
    _apply_all(vault, run_id, planned["plan"]["plan_id"])

    def index_still_catching_up(*_args, degraded_out=None, **_kwargs):  # noqa: ANN001, ANN002, ANN003
        if degraded_out is not None:
            degraded_out.append("pending_visibility")
        return []

    monkeypatch.setattr(find, "find", index_still_catching_up)
    finished = adoption_run.finish(vault, run_id=run_id, today=TODAY)

    assert finished["phase"] == "done"
    assert "first_question" not in finished["finish"]
    assert "route" not in finished["finish"]
    assert finished["finish"]["indexing"] == {
        "state": "pending", "sampled": 2, "found": None, "missed": 0, "pending": 2, "failed": 0,
    }


def test_finish_retains_confirmed_hits_when_other_recall_is_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _vault(tmp_path, {
        "Coll/alpha.md": "# Alpha\n\nAn alpha observation.\n",
        "Coll/beta.md": "# Beta\n\nA beta observation.\n",
    })
    run_id, planned = _import(vault, path="Coll", include=["Coll"])
    applied, _calls = _apply_all(vault, run_id, planned["plan"]["plan_id"])
    target = applied["outcomes"]["Coll/alpha.md"]["target_path"]

    def partial_find(*_args, query, degraded_out=None, **_kwargs):  # noqa: ANN001, ANN002, ANN003
        if query == "Alpha":
            return [type("Hit", (), {"path": target})()]
        if degraded_out is not None:
            degraded_out.append("pending_visibility")
        return []

    monkeypatch.setattr(find, "find", partial_find)
    sample = adoption_run.finish(vault, run_id=run_id, today=TODAY)["coverage"]["recall"]

    assert sample["status"] == "pending"
    assert sample["sampled"] == 2 and sample["evaluated"] == 1
    assert sample["found"] == 1 and sample["pending"] == 1
    assert sample["missed"] == []


def test_default_detail_result_carries_what_a_caller_needs_to_continue(
    vault: Path, monkeypatch: pytest.MonkeyPatch, tiny_budget: None
) -> None:
    from starlette.testclient import TestClient

    from exomem import server

    old = vault / "Old Notes"
    old.mkdir()
    for n in range(2):
        (old / f"note-{n}.md").write_text(f"# Note {n}\n", encoding="utf-8")
    monkeypatch.setattr(server, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "studio-key")
    monkeypatch.delenv("EXOMEM_CF_ACCESS_TEAM_DOMAIN", raising=False)
    monkeypatch.delenv("EXOMEM_CF_ACCESS_AUD", raising=False)
    client = TestClient(server.build_server(require_auth=False).http_app())

    def call(body: dict) -> dict:
        response = client.post(
            "/api/adoption_studio", json=body, headers={"Authorization": "Bearer studio-key"}
        )
        assert response.status_code == 200, response.text
        return response.json()["data"]

    run_id = call({"action": "start", "path": "Old Notes"})["run_id"]
    call({"action": "select", "run_id": run_id, "include": ["Old Notes"]})
    planned = call({"action": "plan", "run_id": run_id})
    assert planned["continuation"]
    while planned.get("continuation"):
        planned = call({"action": "plan", "run_id": run_id, "continuation": planned["continuation"]})
    applied = call({"action": "apply", "run_id": run_id, "plan_id": planned["plan_id"]})
    assert applied["phase"] == "applying"
    assert applied["continuation"]


# --- originals stay inside the vault ------------------------------------------------
def test_a_file_swapped_for_a_link_out_of_the_vault_is_refused_at_plan_time(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("text that lives outside the vault", encoding="utf-8")
    vault = _vault(
        tmp_path, {"Coll/victim.txt": "# Victim\n\nin the vault\n", "Coll/ok.md": "# Ok\n\nfine\n"}
    )
    run_id = adoption_run.start(vault, path="Coll", today=TODAY)["run_id"]
    adoption_run.select(vault, run_id=run_id, include=["Coll"])
    (vault / "Coll/victim.txt").unlink()
    (vault / "Coll/victim.txt").symlink_to(outside)

    planned = adoption_run.plan(vault, run_id=run_id, today=TODAY)
    final, _calls = _apply_all(vault, run_id, planned["plan"]["plan_id"])

    assert {s["path"]: s["code"] for s in planned["plan"]["skipped"]} == {
        "Coll/victim.txt": "INVALID_PATH"
    }
    assert final["phase"] == "applied"
    assert not any(
        "outside the vault" in page.read_text(encoding="utf-8")
        for page in (vault / SOURCES).rglob("*.md")
    )


def test_a_file_swapped_for_a_link_after_the_plan_fails_at_apply_without_importing_the_target(
    tmp_path: Path,
) -> None:
    content = "# Victim\n\nthe same bytes on both sides\n"
    outside = tmp_path / "outside.txt"
    outside.write_text(content, encoding="utf-8")
    vault = _vault(tmp_path, {"Coll/victim.txt": content})
    run_id, planned = _import(vault, path="Coll", include=["Coll"])
    (vault / "Coll/victim.txt").unlink()
    (vault / "Coll/victim.txt").symlink_to(outside)

    final, _calls = _apply_all(vault, run_id, planned["plan"]["plan_id"])

    outcome = final["outcomes"]["Coll/victim.txt"]
    assert outcome["status"] == "failed"
    assert outcome["code"] == "INVALID_PATH"
    assert list((vault / SOURCES).rglob("*victim*")) == []


def test_planning_refuses_an_ancestor_exchanged_after_source_acquisition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _vault(tmp_path, {"Coll/parent/child/note.md": "# Original\n"})
    outside = tmp_path / "outside"
    (outside / "child").mkdir(parents=True)
    (outside / "child/note.md").write_text("# Outside\n", encoding="utf-8")
    run_id = adoption_run.start(vault, path="Coll", today=TODAY)["run_id"]
    adoption_run.select(vault, run_id=run_id, include=["Coll"])
    with held_fs.acquire(vault).require() as filesystem:
        filesystem_type = type(filesystem)
    real_file = filesystem_type.file
    exchanged = False

    def exchange_after_acquisition(self, parent, leaf, **kwargs):
        nonlocal exchanged
        result = real_file(self, parent, leaf, **kwargs)
        if leaf == "note.md" and result.ok and not exchanged:
            exchanged = True
            (vault / "Coll/parent").rename(vault / "Coll/retired")
            (vault / "Coll/parent").symlink_to(outside, target_is_directory=True)
        return result

    monkeypatch.setattr(filesystem_type, "file", exchange_after_acquisition)

    planned = adoption_run.plan(vault, run_id=run_id, today=TODAY)

    assert planned["plan"]["items"] == []
    assert planned["plan"]["skipped"][0]["code"] in {"INVALID_PATH", "SOURCE_CHANGED"}
    assert exchanged


@pytest.mark.parametrize("form", ["text", "artifact"])
@pytest.mark.parametrize("exchange_at", ["before_publication", "after_publication"])
def test_import_rolls_back_when_original_custody_changes_during_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, form: str, exchange_at: str
) -> None:
    relative = "Coll/note.md" if form == "text" else "Coll/archive.bin"
    data = "# Original\n" if form == "text" else b"\0original binary\xff"
    vault = _vault(tmp_path, {relative: data})
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside bytes must not be imported")
    run_id, planned = _import(vault, path="Coll", include=["Coll"])
    target = planned["plan"]["items"][0]["target_path"]
    exchanged = False

    def exchange():
        nonlocal exchanged
        exchanged = True
        (vault / relative).unlink()
        (vault / relative).symlink_to(outside)

    real_batch = adopt.batch_atomic_write
    if exchange_at == "before_publication":
        def exchange_before_batch(*args, **kwargs):
            exchange()
            return real_batch(*args, **kwargs)

        monkeypatch.setattr(adopt, "batch_atomic_write", exchange_before_batch)
    else:
        def exchange_after_page(path):
            if path == vault / target:
                exchange()

        monkeypatch.setattr(vault_module, "_after_batch_destination_published", exchange_after_page)

    with pytest.raises(adoption_run.AdoptionRunError) as caught:
        _apply_all(vault, run_id, planned["plan"]["plan_id"])

    assert caught.value.code == "ADOPTION_SOURCE_CHANGED"
    assert exchanged
    assert not (vault / target).exists()
    artifact = planned["plan"]["items"][0].get("artifact_path")
    assert not artifact or not (vault / artifact).exists()


def test_import_preserves_text_and_artifact_with_physical_nfd_names(tmp_path: Path) -> None:
    text_path = "Coll/cafe\u0301/note.md"
    artifact_path = "Coll/media\u0301.bin"
    binary = b"\0original\xff"
    vault = _vault(tmp_path, {text_path: "# Original\n", artifact_path: binary})
    run_id, planned = _import(vault, path="Coll", include=["Coll"])

    applied, _calls = _apply_all(vault, run_id, planned["plan"]["plan_id"])

    assert applied["phase"] == "applied"
    assert applied["outcomes"][text_path]["original_unchanged"] is True
    assert (vault / applied["outcomes"][artifact_path]["artifact_path"]).read_bytes() == binary


@pytest.mark.skipif(os.name == "nt", reason="Windows refuses literal colon file names")
def test_import_acquires_literal_colon_parents_and_leaf(tmp_path: Path) -> None:
    original = "Coll/Parent: Stage/Child: Notes/entry: Index.md"
    vault = _vault(tmp_path, {original: "# Literal entry\n"})

    applied = adopt.adopt(vault, mode="copy-as-sources", selected_paths=[original], today=TODAY)

    [copied] = applied["copy"]["copied_sources"]
    page = (vault / copied["source_path"]).read_text(encoding="utf-8")
    assert parse_frontmatter(page)[0]["imported_from"] == original
    assert (vault / original).read_text(encoding="utf-8") == "# Literal entry\n"


def test_a_colon_selector_without_a_literal_entry_cannot_authorize_source_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _vault(tmp_path, {"Coll/note.md": "# Ordinary file\n"})
    with held_fs.acquire(vault).require() as filesystem:
        filesystem_type = type(filesystem)
    real_file = filesystem_type.file

    def selector_file(self, parent, leaf, **kwargs):
        return real_file(self, parent, "note.md" if leaf == "note:stream.md" else leaf, **kwargs)

    monkeypatch.setattr(filesystem_type, "file", selector_file)

    result = adoption_plan.read_facts(
        vault, "Coll/note:stream.md", ceiling=4096, text_hint=True
    )

    assert result["code"] == "INVALID_PATH"
    assert reserved_paths.classify_logical("Coll/note:stream.md").disposition is (
        reserved_paths.PathDisposition.INVALID
    )
    with pytest.raises(reserved_paths.ReservedPathLeafError):
        reserved_paths.read_generic_bytes(vault, "Coll/note:stream.md", physical=True)


def test_physical_colon_source_names_do_not_bypass_reserved_classification(tmp_path: Path) -> None:
    vault = _vault(tmp_path, {})

    result = adoption_plan.read_facts(
        vault, "Knowledge Base/_governance/policy: draft.md", ceiling=4096, text_hint=True
    )

    assert result["code"] == "RESERVED_PATH"


@pytest.mark.skipif(os.name == "nt", reason="Windows refuses literal colon file names")
def test_retained_colon_source_rechecks_its_literal_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _vault(tmp_path, {"Coll/note: draft.md": "# Original\n"})
    with held_fs.acquire(vault).require() as filesystem:
        filesystem_type = type(filesystem)
    real_children = filesystem_type.children

    def missing_entry(self, parent, **kwargs):
        result = real_children(self, parent, **kwargs)
        if result.ok:
            return held_fs.HeldResult(value=tuple(
                row for row in result.require() if row.relative_path != "note: draft.md"
            ))
        return result

    with pytest.raises(reserved_paths.ReservedPathLeafError) as caught:
        with reserved_paths.open_generic_binary(vault, "Coll/note: draft.md", physical=True) as source:
            monkeypatch.setattr(filesystem_type, "children", missing_entry)
            source.require_current()

    assert caught.value.code == "UNSAFE_PATH"


# --- plan-layer rules -----------------------------------------------------------
def test_a_non_utf8_note_with_an_upper_case_suffix_is_kept_as_a_bin_artifact(tmp_path: Path) -> None:
    data = "# Café\n\nnaïve\n".encode("latin-1")
    vault = _vault(tmp_path, {"Coll/OLD.MD": data})
    run_id, planned = _import(vault, path="Coll", include=["Coll"])

    applied, _calls = _apply_all(vault, run_id, planned["plan"]["plan_id"])

    outcome = applied["outcomes"]["Coll/OLD.MD"]
    assert outcome["status"] == "applied"
    assert outcome["artifact_path"].endswith(".bin")
    assert (vault / outcome["artifact_path"]).read_bytes() == data


def test_a_link_fragment_is_dropped_for_resolution_and_kept_in_the_rendered_link(
    tmp_path: Path,
) -> None:
    vault = _vault(
        tmp_path,
        {
            "Coll/source.md": "# Source\n\nSee [[target#Setup]] and [guide](target.md#install).\n",
            "Coll/target.md": "# Target\n",
        },
    )
    run_id, planned = _import(vault, path="Coll", include=["Coll"])
    applied, _calls = _apply_all(vault, run_id, planned["plan"]["plan_id"])
    target = applied["outcomes"]["Coll/target.md"]["target_path"]

    page = (vault / applied["outcomes"]["Coll/source.md"]["target_path"]).read_text(encoding="utf-8")
    links = page.split("## Links\n\n", 1)[1].split("\n\n", 1)[0].splitlines()

    stem = target[: -len(".md")]
    assert links == [f"- [[{stem}#Setup]]", f"- [[{stem}#install]]"]


@pytest.mark.parametrize(
    ("aliases", "kept_aliases", "tags", "kept_tags", "rejected_tags"),
    [
        ("[Fine, 42]", ["Fine"], "[good, 'dir/', 'a//b']", ["good"], ["dir/", "a//b"]),
        ("42", [], "[good, 'dir/', 'a//b']", ["good"], ["dir/", "a//b"]),
        ("[Fine, 42]", ["Fine"], "42", [], ["42"]),
    ],
)
def test_aliases_and_tags_the_page_cannot_carry_are_reported_on_the_plan_item(
    tmp_path: Path, aliases: str, kept_aliases: list[str], tags: str,
    kept_tags: list[str], rejected_tags: list[str],
) -> None:
    vault = _vault(
        tmp_path,
        {
            "Coll/note.md": (
                f"---\naliases: {aliases}\ntags: {tags}\n---\n# Note\n"
            )
        },
    )
    _run_id, planned = _import(vault, path="Coll", include=["Coll"])

    item = planned["plan"]["items"][0]

    assert (item["aliases"], item["rejected_aliases"]) == (kept_aliases, ["42"])
    assert (item["tags"], item["rejected_tags"]) == (kept_tags, rejected_tags)


def test_importing_a_subfolder_then_its_parent_does_not_duplicate_the_subfolder_notes(
    tmp_path: Path,
) -> None:
    vault = _vault(
        tmp_path, {"Coll/sub/x.md": "# X\n\nin the subfolder\n", "Coll/y.md": "# Y\n\nat the top\n"}
    )
    run_id, planned = _import(vault, path="Coll/sub", include=["Coll/sub"])
    _apply_all(vault, run_id, planned["plan"]["plan_id"])

    run_id, planned = _import(vault, path="Coll", include=["Coll"])
    items = {item["original_path"]: item["action"] for item in planned["plan"]["items"]}
    applied, _calls = _apply_all(vault, run_id, planned["plan"]["plan_id"])

    assert items == {"Coll/sub/x.md": "already-imported", "Coll/y.md": "copy-as-source"}
    assert applied["outcomes"]["Coll/sub/x.md"]["status"] == "already-applied"
    assert len(list((vault / SOURCES / "Imported").glob("*.md"))) == 2


def test_a_page_whose_front_matter_is_longer_than_the_index_head_still_counts_as_an_import(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path, {"Coll/long.md": f"# {'A very long title ' * 3000}\n\nbody\n"})
    run_id, planned = _import(vault, path="Coll", include=["Coll"])
    _apply_all(vault, run_id, planned["plan"]["plan_id"])

    _run_id, replanned = _import(vault, path="Coll", include=["Coll"])

    assert [item["action"] for item in replanned["plan"]["items"]] == ["already-imported"]


def test_a_removed_import_cannot_be_reported_as_already_applied(tmp_path: Path) -> None:
    vault = _vault(tmp_path, {"Coll/a.md": "# A\n\noriginal\n"})
    run_id, planned = _import(vault, path="Coll", include=["Coll"])
    first, _calls = _apply_all(vault, run_id, planned["plan"]["plan_id"])
    run_id, planned = _import(vault, path="Coll", include=["Coll"])
    target = first["outcomes"]["Coll/a.md"]["target_path"]
    (vault / target).unlink()

    final, _calls = _apply_all(vault, run_id, planned["plan"]["plan_id"])

    outcome = final["outcomes"]["Coll/a.md"]
    assert outcome["status"] == "failed"
    assert outcome["code"] == "PLAN_STALE"
    assert "re-plan" in outcome["reason"]
    assert not (vault / target).exists()


def test_a_removed_artifact_cannot_be_reported_as_already_applied(tmp_path: Path) -> None:
    vault = _vault(tmp_path, {"Coll/archive.bin": b"\0original binary\xff"})
    run_id, planned = _import(vault, path="Coll", include=["Coll"])
    first, _calls = _apply_all(vault, run_id, planned["plan"]["plan_id"])
    run_id, planned = _import(vault, path="Coll", include=["Coll"])
    outcome = first["outcomes"]["Coll/archive.bin"]
    (vault / outcome["artifact_path"]).unlink()

    final, _calls = _apply_all(vault, run_id, planned["plan"]["plan_id"])

    assert (vault / outcome["target_path"]).exists()
    assert final["outcomes"]["Coll/archive.bin"]["status"] == "failed"
    assert final["outcomes"]["Coll/archive.bin"]["code"] == "PLAN_STALE"


def test_a_changed_original_cannot_be_reported_as_already_applied(tmp_path: Path) -> None:
    vault = _vault(tmp_path, {"Coll/a.md": "# A\n\noriginal\n"})
    run_id, planned = _import(vault, path="Coll", include=["Coll"])
    _apply_all(vault, run_id, planned["plan"]["plan_id"])
    run_id, planned = _import(vault, path="Coll", include=["Coll"])
    (vault / "Coll/a.md").write_text("# A\n\nchanged\n", encoding="utf-8")

    with pytest.raises(adoption_run.AdoptionRunError) as caught:
        _apply_all(vault, run_id, planned["plan"]["plan_id"])

    assert caught.value.code == "ADOPTION_SOURCE_CHANGED"
    assert adoption_run.status(vault, run_id=run_id)["outcomes"] == {}


def test_a_plan_names_the_other_root_that_earlier_imports_of_the_collection_came_from(
    tmp_path: Path,
) -> None:
    vault = _vault(
        tmp_path,
        {"_Imports/first/Coll/a.md": "# A\n", "_Imports/second/Coll/a.md": "# A\n"},
    )
    run_id, planned = _import(vault, path="_Imports/first", include=["_Imports/first"])
    assert planned["plan"]["warnings"] == []
    _apply_all(vault, run_id, planned["plan"]["plan_id"])

    _run_id, replanned = _import(vault, path="_Imports/second", include=["_Imports/second"])

    [warning] = replanned["plan"]["warnings"]
    assert warning["code"] == "EARLIER_IMPORT_FROM_OTHER_ROOT"
    assert warning["roots"] == ["_Imports/first/Coll"]


def test_the_recall_question_asks_about_the_largest_text_note_not_a_larger_media_file(
    tmp_path: Path,
) -> None:
    vault = _vault(
        tmp_path,
        {
            "Coll/scan.pdf": b"%PDF-1.4\n" + b"x" * 5000 + b"\n%%EOF\n",
            "Coll/roadmap.md": "# Roadmap\n\ntext\n",
        },
    )
    run_id, planned = _import(vault, path="Coll", include=["Coll"])
    _apply_all(vault, run_id, planned["plan"]["plan_id"])

    finished = adoption_run.finish(vault, run_id=run_id, today=TODAY)

    assert finished["finish"]["first_question"] == 'What do my notes say about "Roadmap"?'


def test_an_apply_call_reads_the_sources_tree_once_however_many_chunks_it_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each chunk re-walking every Source made a re-run cost grow with the whole vault."""
    monkeypatch.setattr(adoption_run, "APPLY_CHUNK_ITEMS", 2)
    files = {f"Coll/note-{n}.md": f"# Note {n}\n\ntext {n}\n" for n in range(5)}
    vault = _vault(tmp_path, files)
    run_id, planned = _import(vault, path="Coll", include=["Coll"])
    walks = {"index": 0, "counts": 0}
    real_index, real_counts = adoption_plan.read_import_index, indexes._count_sources

    def counting_index(*args, **kwargs):
        walks["index"] += 1
        return real_index(*args, **kwargs)

    def counting_counts(*args, **kwargs):
        walks["counts"] += 1
        return real_counts(*args, **kwargs)

    monkeypatch.setattr(adoption_plan, "read_import_index", counting_index)
    monkeypatch.setattr(indexes, "_count_sources", counting_counts)

    applied = adoption_run.apply(
        vault, run_id=run_id, plan_id=planned["plan"]["plan_id"], today=TODAY
    )

    assert applied["phase"] == "applied"
    assert walks == {"index": 0, "counts": 1}
    sources_index = (vault / SOURCES / "index.md").read_text(encoding="utf-8")
    assert "copied legacy-vault material with provenance (5)" in sources_index


def test_a_continuing_apply_call_does_not_open_the_sources_an_earlier_run_filed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tiny_budget: None
) -> None:
    """Opening every Source page on each call made a late call of a long import file one chunk."""
    monkeypatch.setattr(adoption_run, "APPLY_CHUNK_ITEMS", 1)
    files = {f"Old/old-{n}.md": f"# Old {n}\n\ntext {n}\n" for n in range(4)}
    files.update({f"New/new-{n}.md": f"# New {n}\n\ntext {n}\n" for n in range(3)})
    vault = _vault(tmp_path, files)
    run_id, planned = _import(vault, path="Old", include=["Old"])
    _apply_all(vault, run_id, planned["plan"]["plan_id"])
    run_id, planned = _import(vault, path="New", include=["New"])
    own_pages = {item["target_path"] for item in planned["plan"]["items"]}
    opened: list[str] = []
    real_entry = adoption_plan.read_import_entry

    def recording_entry(root: Path, page: Path):
        opened.append(page.relative_to(root).as_posix())
        return real_entry(root, page)

    monkeypatch.setattr(adoption_plan, "read_import_entry", recording_entry)

    applied, calls = _apply_all(vault, run_id, planned["plan"]["plan_id"])

    assert (applied["phase"], calls) == ("applied", 3)
    assert set(opened) <= own_pages
    assert len(list((vault / SOURCES / "Imported").glob("*.md"))) == 7


def test_an_apply_finds_a_source_another_run_filed_since_the_plan_and_files_no_duplicate(
    tmp_path: Path
) -> None:
    """A plan made before another run filed the same files must not file them again."""
    vault = _vault(tmp_path, {"Coll/a.md": "# A\n\none\n", "Coll/b.md": "# B\n\ntwo\n"})
    stale_run, stale = _import(vault, path="Coll", include=["Coll"])
    later = TODAY + dt.timedelta(days=1)  # another day, so its Sources are named differently
    other_run = adoption_run.start(vault, path="Coll", today=later)["run_id"]
    adoption_run.select(vault, run_id=other_run, include=["Coll"])
    other = adoption_run.plan(vault, run_id=other_run, today=later)
    adoption_run.apply(vault, run_id=other_run, plan_id=other["plan"]["plan_id"], today=later)
    filed = sorted(p.name for p in (vault / SOURCES / "Imported").glob("*.md"))

    applied = adoption_run.apply(
        vault, run_id=stale_run, plan_id=stale["plan"]["plan_id"], today=TODAY
    )

    assert {o["status"] for o in applied["outcomes"].values()} == {"already-applied"}
    assert sorted(p.name for p in (vault / SOURCES / "Imported").glob("*.md")) == filed
    assert len(filed) == 2


def test_a_fast_acknowledged_call_reserves_the_custody_handoff_it_measured_between_a_floor_and_a_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A flat 20 s reserve cost a call that hands off custody in about a second most of its budget."""
    clock = {"now": 1000.0}
    fake_time = types.SimpleNamespace(monotonic=lambda: clock["now"])
    monkeypatch.setattr(request_budget, "time", fake_time)
    monkeypatch.setattr(writer_lease, "_fast_ack_monotonic", fake_time.monotonic)
    monkeypatch.setattr(request_budget, "DELIVERY_RESERVE_SECONDS", 5.0)
    monkeypatch.setattr(adoption_run, "INITIAL_ITEM_SECONDS", 1.0)
    monkeypatch.setattr(adoption_run, "REPLY_SECONDS_PER_ITEM", 0.0)
    monkeypatch.setattr(adoption_run, "APPLY_CHUNK_BUDGET_SHARE", 1.0)
    vault = _vault(tmp_path, {})
    batch = types.SimpleNamespace(
        receipt=types.SimpleNamespace(batch_id="b", paths=()), handoff_outcome=None, handoff_error=None
    )
    session = writer_lease._FastAcknowledgementSession(
        vault_root=vault, request_id="r", mutation_attempt_digest="d",
        terminal_replay_until=0.0, advisory_required=False, batches=[batch],
    )
    pending = [{"original_bytes": 0, "form": "note"} for _ in range(10)]

    def items_admitted(budget_seconds: float) -> int:
        budget = request_budget.RequestBudget(seconds=budget_seconds, entry=clock["now"])
        return len(adoption_run._ApplyPacer(vault, budget).next_chunk(pending, first=False))

    def measure_handoff(seconds: float) -> None:
        def handoff(_session) -> int:
            clock["now"] += seconds
            batch.handoff_outcome = "ready"
            return 1

        monkeypatch.setattr(writer_lease, "_handoff_derived_batches", handoff)
        batch.handoff_outcome = None
        writer_lease._timed_handoff(session)

    token = writer_lease._ACTIVE_FAST_ACK_SESSION.set(session)
    try:
        # Nothing measured yet: the whole 20 s reserve stands.
        assert (items_admitted(12), items_admitted(27)) == (0, 2)
        # A handoff near zero still reserves the floor (2 s), not nothing.
        measure_handoff(0.0)
        assert items_admitted(12) == 5
        # A 3 s handoff per batch reserves 3 s for the chunk's batch: the session's is handed off.
        measure_handoff(3.0)
        assert items_admitted(12) == 4
        # A 100 s handoff reserves no more than the ceiling (20 s).
        measure_handoff(100.0)
        assert (items_admitted(12), items_admitted(27)) == (0, 2)
    finally:
        writer_lease._ACTIVE_FAST_ACK_SESSION.reset(token)


def test_a_call_leaves_a_byte_heavy_artifact_to_the_next_call_when_it_does_not_fit_in_the_time_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cost grows with bytes as well as items: small notes must not license a huge file."""
    clock = {"now": 1000.0}
    fake_time = types.SimpleNamespace(monotonic=lambda: clock["now"])
    monkeypatch.setattr(adoption_run, "time", fake_time)
    monkeypatch.setattr(request_budget, "time", fake_time)
    monkeypatch.setattr(request_budget, "DELIVERY_RESERVE_SECONDS", 2.0)
    monkeypatch.setenv(request_budget.BUDGET_ENV_VAR, "12")
    request_budget.reset_resolution_cache()
    # The start a call assumes for a file it has not yet timed: dear, like a slow disk.
    monkeypatch.setattr(adoption_run, "INITIAL_SECONDS_PER_MIB", 6.0)
    real_apply_chunk = adoption_run._apply_chunk

    def timed_chunk(*args, **kwargs):
        result = real_apply_chunk(*args, **kwargs)
        chunk = args[3]
        megabytes = sum(planned["original_bytes"] for planned in chunk) / 2**20
        clock["now"] += 0.1 * len(chunk) + 4.85 * megabytes  # the big file takes 9.7 s
        return result

    monkeypatch.setattr(adoption_run, "_apply_chunk", timed_chunk)
    files: dict[str, str | bytes] = {f"Coll/a-note-{n}.md": f"# Note {n}\n" for n in range(6)}
    files["Coll/z-archive.zip"] = b"\0" * (2 * 2**20)
    vault = _vault(tmp_path, files)
    run_id, planned = _import(vault, path="Coll", include=["Coll"])
    plan_id = planned["plan"]["plan_id"]

    elapsed: list[float] = []
    last: dict = {}
    while not elapsed or last.get("continuation"):
        began = clock["now"]
        last = adoption_run.apply(
            vault, run_id=run_id, plan_id=plan_id, continuation=last.get("continuation"),
            today=TODAY,
        )
        elapsed.append(clock["now"] - began)

    assert max(elapsed) <= 12.0 - 2.0
    assert last["phase"] == "applied"
    assert (vault / last["outcomes"]["Coll/z-archive.zip"]["artifact_path"]).stat().st_size == 2 * 2**20
