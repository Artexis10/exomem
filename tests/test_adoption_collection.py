"""Lossless collection adoption: links, folder rules, re-import, artifacts, bounded calls, coverage."""

from __future__ import annotations

import datetime as dt
import hashlib
from pathlib import Path

import pytest

from exomem import adoption_run, epistemic_graph, find, media_jobs, request_budget, source_taxonomy
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
