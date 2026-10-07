"""An agent names what a source is; nothing new lands in a catch-all.

The defect this binds: a capture with no kind, or with `other`, landed in
`Sources/Other`, and an agent refused for another reason (a video transcript
with no URL) picked `other` to get past the refusal. Now:

- an agent-facing capture with no kind, or with `other` or `unclassified`, is
  refused before anything is written, and the refusal lists the vault's known
  kinds with their use counts so the agent can choose;
- a capture with no agent in the loop (the terminal UI, the hosted capture box,
  an out-of-band upload, a legacy-vault import) is recorded as `unclassified`,
  and every later capture reports the vault's unclassified sources as debt;
- a legacy `Sources/Other` page stays readable, findable and filterable.

Every fixture here is synthetic.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
import yaml

from exomem import adopt as adopt_module
from exomem import commands, mutation_terminal, product_invoke
from exomem import schema as schema_module
from exomem.cli_ops import OpError
from exomem.tui.backend import ExomemBackend

KB = "Knowledge Base"


def _vault_files(vault: Path) -> dict[str, bytes]:
    return {
        path.relative_to(vault).as_posix(): path.read_bytes()
        for path in sorted(vault.rglob("*"))
        if path.is_file()
    }


def _frontmatter(vault: Path, rel_path: str) -> dict:
    text = (vault / rel_path).read_text(encoding="utf-8")
    front, _, _ = text.partition("\n---\n")
    return yaml.safe_load(front.removeprefix("---\n"))


def _committed_envelope(leaf: object) -> dict:
    """The compact envelope an MCP/REST/CLI caller actually receives."""
    terminal = mutation_terminal.committed_terminal(
        leaf, request_id="r", receipt_id=None, idempotency_key=None
    )
    return mutation_terminal.project_terminal(terminal, "compact")


# ---------------------------------------------------------------------------
# Agent-facing capture names a kind
# ---------------------------------------------------------------------------
def test_a_kindless_agent_capture_is_refused_with_the_known_kinds_and_writes_nothing(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    before = _vault_files(vault)

    with pytest.raises(OpError) as refused:
        commands.op_capture_source(
            vault, source_schema, content="Raw notes from a call.", title="Loose capture"
        )

    error = refused.value.as_public_dict()
    assert error["code"] == "SOURCE_KIND_REQUIRED"
    known = error["known_source_kinds"]
    # The fixture vault files two Articles, one Book and one Session: the counts
    # are read from the vault, most used first.
    assert known[0] == {"kind": "article", "sources": 2}
    assert {"kind": "book", "sources": 1} in known
    assert {"kind": "session", "sources": 1} in known
    counts = [entry["sources"] for entry in known]
    assert counts == sorted(counts, reverse=True)
    assert len(known) <= 30
    # A kind no agent may choose is never offered.
    assert not {"other", "unclassified", "episode"} & {entry["kind"] for entry in known}
    # The refusal says what each count counts and where it came from.
    assert "Sources/" in error["known_source_kinds_counted"]
    assert error["remediation"]
    assert _vault_files(vault) == before


@pytest.mark.parametrize("unchosen", ["other", "unclassified"])
def test_an_agent_cannot_choose_a_kind_that_records_no_choice(
    vault: Path, source_schema: schema_module.SourceSchema, unchosen: str
) -> None:
    before = _vault_files(vault)

    with pytest.raises(OpError) as refused:
        commands.op_capture_source(
            vault, source_schema, content="Raw notes.", title="Catch-all capture",
            source_kind=unchosen,
        )

    assert refused.value.code == "SOURCE_KIND_REQUIRED"
    assert refused.value.details["known_source_kinds"]
    assert _vault_files(vault) == before


def test_a_kindless_file_capture_is_refused_before_any_byte_is_fetched(
    vault: Path,
    source_schema: schema_module.SourceSchema,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import client_artifacts

    fetched: list[object] = []
    monkeypatch.setattr(
        client_artifacts, "stage_artifact", lambda *args, **kwargs: fetched.append(args)
    )

    with pytest.raises(OpError, match="SOURCE_KIND_REQUIRED"):
        commands.op_capture_source(
            vault, source_schema, title="Attached manual",
            files=[{"download_url": "https://files.example/1", "file_id": "f1"}],
        )

    assert fetched == []


def test_a_new_slug_registers_and_lands_in_its_projected_folder(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    out = commands.op_capture_source(
        vault, source_schema, content="Ridge counts, 06:10.", title="Ridge survey",
        source_kind="field-notebook",
    )

    assert out["source"]["path"].startswith(f"{KB}/Sources/Field Notebook/")
    registry = yaml.safe_load(
        (vault / KB / "_Schema" / "source-taxonomy.yaml").read_text(encoding="utf-8")
    )
    assert "field-notebook" in registry["source_kinds"]


def test_a_url_refusal_names_what_to_capture_instead(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    """A transcript held without its URL must not be pushed into a catch-all."""
    with pytest.raises(ValueError, match="INVALID_SOURCE") as refused:
        commands.op_capture_source(
            vault, source_schema, content="Speaker: welcome to the talk.",
            title="Conference talk", source_kind="video",
        )

    reason = str(refused.value)
    assert "url" in reason
    assert "source_kind" in reason, "the refusal must name the other way forward"
    # Following that guidance succeeds without a URL.
    out = commands.op_capture_source(
        vault, source_schema, content="Speaker: welcome to the talk.",
        title="Conference talk", source_kind="transcript",
    )
    assert out["source"]["path"]


# ---------------------------------------------------------------------------
# No agent in the loop: recorded as unclassified, reported as debt
# ---------------------------------------------------------------------------
def test_the_terminal_ui_records_a_kindless_thought_as_unclassified(vault: Path) -> None:
    backend = ExomemBackend(str(vault))
    assert backend.resolve_vault().initialized

    result = backend.capture_thought("Call the plumber about the valve.", "Plumber")

    path = result["path"]
    assert path.startswith(f"{KB}/Sources/Unclassified/")
    assert _frontmatter(vault, path)["source_type"] == "unclassified"


def test_a_kindless_out_of_band_upload_is_recorded_as_unclassified(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A person may post these bytes from the upload form, with no agent to ask."""
    from starlette.testclient import TestClient

    from exomem import server, upload_tokens

    secret = "upload-secret"
    monkeypatch.setattr(server, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "leases"))
    monkeypatch.setenv("EXOMEM_UPLOAD_TOKEN", secret)
    monkeypatch.setenv("EXOMEM_DISABLE_RELEVANCE_CHECK", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", "1")
    client = TestClient(server.build_server(require_auth=False).http_app())
    token = upload_tokens.mint(secret, scope=upload_tokens.upload_scope("source"))

    response = client.post(
        "/upload",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("scan.txt", b"Warranty card, serial 4471.", "text/plain")},
        data={"title": "Warranty card"},
    )

    assert response.status_code in (200, 201), response.text
    page = response.json()["path"]
    assert page.startswith(f"{KB}/Sources/Unclassified/")
    assert _frontmatter(vault, page)["source_type"] == "unclassified"


def test_unclassified_sources_are_reported_as_debt_on_the_next_agent_capture(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    product_invoke.invoke_product(
        "capture_source",
        {"content": "Buy filters for the furnace.", "title": "Furnace filters"},
        vault_root=vault,
    )

    leaf = commands.op_capture_source(
        vault, source_schema, content="Ridge counts, 06:10.", title="Ridge survey",
        source_kind="field-notebook",
    )

    advisory = _committed_envelope(leaf)["structure_suggestion"]
    assert advisory["kind"] == "source_classification_debt"
    assert advisory["unclassified_sources"] == 1
    assert advisory["folders"] == ["Unclassified"]


def test_a_legacy_vault_import_is_unclassified_and_counts_as_debt(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    legacy = vault / "Legacy" / "boiler-receipt.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("# Boiler receipt\n\nPaid in full.\n", encoding="utf-8")

    report = adopt_module.adopt(
        vault, mode="copy-as-sources", selected_paths=["Legacy/boiler-receipt.md"],
        today=dt.date(2026, 10, 7),
    )
    imported = report["copy"]["copied_sources"][0]["source_path"]
    assert _frontmatter(vault, imported)["source_type"] == "unclassified"

    leaf = commands.op_capture_source(
        vault, source_schema, content="Ridge counts, 06:10.", title="Ridge survey",
        source_kind="field-notebook",
    )
    advisory = leaf["source"]["structure_suggestion"]
    assert advisory["unclassified_sources"] == 1
    assert advisory["folders"] == ["Imported"]


def test_a_vault_with_no_unclassified_sources_reports_no_debt(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    leaf = commands.op_capture_source(
        vault, source_schema, content="Ridge counts, 06:10.", title="Ridge survey",
        source_kind="field-notebook",
    )
    assert "structure_suggestion" not in leaf["source"]


# ---------------------------------------------------------------------------
# Legacy Sources/Other stays readable, findable and filterable
# ---------------------------------------------------------------------------
def _legacy_other_page(vault: Path) -> str:
    folder = vault / KB / "Sources" / "Other"
    folder.mkdir(parents=True)
    page = folder / "2026-01-05-harbour-ferry-timetable.md"
    page.write_text(
        "---\ntype: source\ntitle: Harbour ferry timetable\nsource_type: other\n"
        "captured: 2026-01-05\ntags: []\ningested_into: []\n---\n\n"
        "# Harbour ferry timetable\n\n## Capture\n\nThe harbour ferry leaves at 07:40.\n",
        encoding="utf-8",
    )
    return page.relative_to(vault).as_posix()


def test_a_legacy_other_page_stays_readable_findable_and_filterable(
    vault: Path,
) -> None:
    rel = _legacy_other_page(vault)

    read = commands.op_read_memory(vault, path=rel)
    assert "07:40" in str(read)

    found = commands.op_ask_memory(vault, query="harbour ferry timetable", limit=10)
    assert rel.removesuffix(".md") in str(found)

    filtered = commands.op_ask_memory(
        vault, query="harbour ferry timetable", source_kinds=["other"], limit=10
    )
    assert rel.removesuffix(".md") in str(filtered)


def test_a_legacy_other_page_counts_as_debt(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    _legacy_other_page(vault)

    leaf = commands.op_capture_source(
        vault, source_schema, content="Ridge counts, 06:10.", title="Ridge survey",
        source_kind="field-notebook",
    )

    advisory = leaf["source"]["structure_suggestion"]
    assert advisory["unclassified_sources"] == 1
    assert advisory["folders"] == ["Other"]
