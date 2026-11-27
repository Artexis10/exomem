"""A capture records the class its agent judged, keeps every byte, and reports what governs the page.

Each test names the failure it catches. Every document number and name is synthetic.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

from exomem import client_artifacts, commands, sensitivity_classes, writer_lease
from exomem import server as server_module
from exomem.vault import content_hash, parse_frontmatter

CARD = "Test Person\nDocument number: X0000000\nIssued by the Republic of Testland.\n"


@pytest.fixture
def agent(vault: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Callable[[str, dict[str, Any]], dict]:
    """Call a public tool the way an agent does, through the MCP server, as the vault owner."""
    monkeypatch.setattr(server_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-lease"))
    monkeypatch.setenv("EXOMEM_CALL_LEDGER_DIR", str(tmp_path / "ledger"))
    app = server_module.build_server(require_auth=False)

    def call(tool: str, arguments: dict[str, Any]) -> dict:
        async def run() -> dict:
            async with Client(app) as client:
                result = await client.call_tool(tool, arguments, raise_on_error=False)
                return result.structured_content or {"error": " ".join(getattr(b, "text", "") for b in result.content)}

        return asyncio.run(run())

    yield call
    writer_lease.reset_managers_for_tests()


def _page(vault: Path, rel: str) -> tuple[dict, str]:
    text = (vault / rel).read_text(encoding="utf-8")
    return parse_frontmatter(text)[0], text


def test_an_agent_keeps_an_identity_card_in_full_and_the_reply_says_what_governs_it(
    vault: Path, agent: Callable[..., dict]
) -> None:
    """A capture that drops the class, or omits the card number, or claims a restriction while no
    governance policy exists."""
    reply = agent("save_source", {
        "title": "Identity card", "source_kind": "session", "content": CARD, "sensitivity": ["identity"],
    })

    assert reply["sensitivity"]["classes"] == ["identity"]
    assert reply["sensitivity"]["basis"] == "declared"
    assert reply["sensitivity"]["disclosure"]["governance"] == "off"
    assert "every client signed in as the owner reads this page" in reply["sensitivity"]["disclosure"]["statement"]
    [page] = (vault / "Knowledge Base/Sources/Sessions").glob("*identity-card.md")
    front, text = _page(vault, page.relative_to(vault).as_posix())
    assert front["sensitivity"] == ["identity"]
    assert "Document number: X0000000" in text


def test_an_unknown_class_refuses_the_capture_naming_the_registered_keys_and_writes_nothing(
    vault: Path, source_schema: Any
) -> None:
    """A free-text class on a Source, or a refusal after the page was already written."""
    before = sorted(vault.rglob("*"))

    with pytest.raises(ValueError, match=sensitivity_classes.UNKNOWN_CODE) as refused:
        commands.op_capture_source(
            vault, source_schema, title="Identity card", source_kind="session", content=CARD,
            sensitivity=["passport-ish"],
        )

    assert "identity, health, financial" in str(refused.value)
    assert sorted(vault.rglob("*")) == before


@pytest.mark.usefixtures("owner_scope")
def test_a_kind_default_applies_only_when_the_capture_declares_nothing(vault: Path, source_schema: Any) -> None:
    """A kind default that overrides an agent's own judgement, or one that never applies and
    leaves the photo unclassified."""
    commands.op_schema_memory(
        vault, subject="source-kinds", operation="save", expected_hash="none", why="id scans",
        proposal={"upsert": {"id-scan": {"label": "ID scan", "attributes": {"sensitivity": ["identity"]}}}},
    )

    defaulted = commands.op_capture_source(
        vault, source_schema, title="Card front", source_kind="id-scan", content=CARD,
    )
    declared_none = commands.op_capture_source(
        vault, source_schema, title="A newsletter", source_kind="id-scan", content="Weekly news.",
        sensitivity=[],
    )
    declared_other = commands.op_capture_source(
        vault, source_schema, title="Lab result", source_kind="id-scan", content="Test Person, lab values.",
        sensitivity=["health"],
    )

    assert defaulted["sensitivity"]["classes"] == ["identity"]
    assert defaulted["sensitivity"]["basis"] == "kind-default"
    assert declared_none["sensitivity"]["classes"] == [] and declared_none["sensitivity"]["basis"] == "none"
    assert "sensitivity" not in _page(vault, declared_none["source"]["path"])[0]
    assert declared_other["sensitivity"]["classes"] == ["health"]
    assert declared_other["sensitivity"]["basis"] == "declared"


@pytest.mark.usefixtures("owner_scope")
def test_a_kind_default_naming_no_registered_class_never_stops_the_capture(vault: Path, source_schema: Any) -> None:
    """A stale or mistyped class in a vault's kind default that refuses every capture of that kind,
    so the material is not kept because of a label."""
    commands.op_schema_memory(
        vault, subject="source-kinds", operation="save", expected_hash="none", why="id scans",
        proposal={"upsert": {"id-scan": {"label": "ID scan", "attributes": {"sensitivity": ["identity", "idenity"]}}}},
    )

    kept = commands.op_capture_source(vault, source_schema, title="Card front", source_kind="id-scan", content=CARD)

    assert kept["sensitivity"]["classes"] == ["identity"] and kept["sensitivity"]["basis"] == "kind-default"
    assert kept["sensitivity"]["skipped_defaults"]["keys"] == ["idenity"]
    assert _page(vault, kept["source"]["path"])[0]["sensitivity"] == ["identity"]


def test_a_photo_is_kept_byte_for_byte_and_its_page_carries_the_class(
    vault: Path, source_schema: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A capture of an image that classifies by rewriting, covering or re-encoding the bytes."""
    photo = b"\x89PNG\r\n\x1a\n" + b"synthetic card pixels, Test Person X0000000" * 8
    staged = tmp_path / "card.png"
    staged.write_bytes(photo)
    monkeypatch.setattr(
        client_artifacts, "stage_artifact",
        lambda *a, **kw: client_artifacts.StagedArtifact(
            "card", staged, len(photo), hashlib.sha256(photo).hexdigest(), "image/png", "card.png",
        ),
    )

    result = commands.op_capture_source(
        vault, source_schema, title="Card photo", source_kind="photograph",
        files=[{"download_url": "https://example.invalid/card", "file_id": "card"}],
        sensitivity=["identity"],
    )

    [stored] = result["files"]
    assert (vault / stored["stored_path"]).read_bytes() == photo
    assert _page(vault, stored["page"])[0]["sensitivity"] == ["identity"]
    assert result["sensitivity"]["classes"] == ["identity"] and result["sensitivity"]["basis"] == "declared"


def test_a_correction_keeps_the_class_and_a_different_declaration_is_refused(
    vault: Path, source_schema: Any
) -> None:
    """A correction that silently drops its predecessor's class, or one that changes the class
    without the classify operation's reason record."""
    first = commands.op_capture_source(
        vault, source_schema, title="Identity card", source_kind="session", content=CARD,
        sensitivity=["identity"],
    )
    path = first["source"]["path"]
    correction = {
        "supersedes": path, "expected_hash": content_hash((vault / path).read_text(encoding="utf-8")),
        "reason": "fix a typo",
    }

    with pytest.raises(ValueError, match="APPEND_ONLY"):
        commands.op_capture_source(
            vault, source_schema, title="Identity card", source_kind="session",
            content=CARD.replace("Testland", "Testlandia"), sensitivity=["health"], correction=correction,
        )
    corrected = commands.op_capture_source(
        vault, source_schema, title="Identity card", source_kind="session",
        content=CARD.replace("Testland", "Testlandia"), correction=correction,
    )

    assert corrected["sensitivity"]["classes"] == ["identity"]
    assert _page(vault, corrected["source"]["path"])[0]["sensitivity"] == ["identity"]


@pytest.mark.usefixtures("duplicate_index")
def test_a_repeated_capture_reports_the_stored_class_and_names_a_declaration_it_did_not_take(
    vault: Path, source_schema: Any
) -> None:
    """A reused capture that reports the new declaration as if it had been stored."""
    commands.op_capture_source(
        vault, source_schema, title="Identity card", source_kind="session", content=CARD,
        sensitivity=["identity"],
    )

    again = commands.op_capture_source(
        vault, source_schema, title="Identity card", source_kind="session", content=CARD,
        sensitivity=["health"],
    )

    assert again["capture_outcome"] == "reused"
    assert again["sensitivity"]["classes"] == ["identity"]
    assert {"field": "sensitivity", "provided": ["health"], "kept": ["identity"]} in again["fields_not_applied"]


def _stage_photo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, photo: bytes) -> None:
    staged = tmp_path / "card.png"
    staged.write_bytes(photo)
    monkeypatch.setattr(
        client_artifacts, "stage_artifact",
        lambda *a, **kw: client_artifacts.StagedArtifact(
            "card", staged, len(photo), hashlib.sha256(photo).hexdigest(), "image/png", "card.png",
        ),
    )


def test_an_agent_saves_a_photo_as_evidence_with_its_class_and_every_byte(
    vault: Path, agent: Callable[..., dict], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Evidence that classifies by covering or re-encoding the image, a sidecar without the class, or a
    reply that omits the disclosure in force."""
    photo = b"\x89PNG\r\n\x1a\n" + b"synthetic card pixels, Test Person X0000000" * 8
    _stage_photo(monkeypatch, tmp_path, photo)

    reply = agent("save_evidence", {
        "scope": "identity", "category": "cards", "sensitivity": ["identity"],
        "files": [{"download_url": "https://example.invalid/card", "file_id": "card"}],
    })

    [stored] = reply["files"]
    assert (vault / stored["stored_path"]).read_bytes() == photo
    sidecar = stored["stored_path"] + ".md"
    assert _page(vault, sidecar)[0]["sensitivity"] == ["identity"]
    assert reply["sensitivity"]["classes"] == ["identity"] and reply["sensitivity"]["basis"] == "declared"
    assert reply["sensitivity"]["disclosure"]["governance"] == "off"


def test_evidence_text_records_the_class_and_an_unknown_class_writes_nothing(
    vault: Path, agent: Callable[..., dict]
) -> None:
    """A text original whose sidecar drops the class, or a refusal that leaves a half-written Evidence page."""
    before = sorted(vault.rglob("*"))
    refused = agent("save_evidence", {
        "scope": "identity", "category": "cards", "name": "card.txt", "text": CARD, "sensitivity": ["passport-ish"],
    })
    assert refused["error"]["code"] == sensitivity_classes.UNKNOWN_CODE
    assert "identity, health, financial" in refused["error"]["message"]
    assert sorted(vault.rglob("*")) == before

    saved = agent("save_evidence", {
        "scope": "identity", "category": "cards", "name": "card.txt", "text": CARD, "sensitivity": ["identity", "health"],
    })

    assert saved["sensitivity"]["classes"] == ["identity", "health"]
    original = "Knowledge Base/Evidence/identity/cards/card.txt"
    assert _page(vault, original + ".md")[0]["sensitivity"] == ["identity", "health"]
    assert (vault / original).read_text(encoding="utf-8") == CARD


@pytest.mark.usefixtures("duplicate_index")
def test_saving_the_same_evidence_again_reports_the_kept_class_and_the_declaration_it_did_not_take(
    vault: Path, agent: Callable[..., dict]
) -> None:
    """A second save of the same bytes that rewrites the page's class or hides that its declaration was not used."""
    args = {"scope": "identity", "category": "cards", "name": "card.txt", "text": CARD}
    agent("save_evidence", {**args, "sensitivity": ["identity"]})

    again = agent("save_evidence", {**args, "sensitivity": ["health"]})

    assert again["sensitivity"]["classes"] == ["identity"]
    assert {"field": "sensitivity", "provided": "health", "kept": "identity"} in again["fields_not_applied"]


def test_an_agent_records_a_class_on_a_note_and_an_unknown_class_writes_nothing(
    vault: Path, agent: Callable[..., dict]
) -> None:
    """A note whose page drops the class, a reply that does not name what governs it, or a refusal
    that leaves a half-written page."""
    note = {
        "title": "Card renewal", "type": "insight", "unlinked_reason": "A standalone test note.",
        "content": "# Card renewal\n\n## Observations\n\n- [finding] Test Person renews card X0000000 #admin\n",
    }
    before = sorted(vault.rglob("*"))
    refused = agent("create_note", {**note, "sensitivity": ["passport-ish"]})
    assert refused["error"]["code"] == sensitivity_classes.UNKNOWN_CODE
    assert "identity, health, financial" in refused["error"]["message"]
    assert sorted(vault.rglob("*")) == before

    saved = agent("create_note", {**note, "sensitivity": ["identity"]})

    assert saved["sensitivity"]["classes"] == ["identity"] and saved["sensitivity"]["basis"] == "declared"
    assert saved["sensitivity"]["disclosure"]["governance"] == "off"
    [page] = vault.rglob("Card renewal.md")
    front, text = _page(vault, page.relative_to(vault).as_posix())
    assert front["sensitivity"] == ["identity"]
    assert "renews card X0000000" in text


def test_a_note_that_supersedes_a_classified_note_keeps_its_class(vault: Path, agent: Callable[..., dict]) -> None:
    """A replacement that holds the same material and drops the class, so the live page reads unclassified."""
    note = {
        "title": "Card renewal", "type": "insight", "unlinked_reason": "A standalone test note.",
        "content": "# Card renewal\n\n## Observations\n\n- [finding] Test Person renews card X0000000 #admin\n",
    }
    saved = agent("create_note", {**note, "sensitivity": ["identity"]})
    assert "error" not in saved, saved

    replaced = agent("supersede_note", {
        "note": "Card renewal", "title": "Card renewal 2027", "why": "The card was renewed",
        "unlinked_reason": "A standalone test note.",
        "content": "# Card renewal 2027\n\n## Observations\n\n- [finding] Test Person renewed card X0000001 #admin\n",
    })

    assert "error" not in replaced, replaced
    [page] = vault.rglob("Card renewal 2027.md")
    assert _page(vault, page.relative_to(vault).as_posix())[0]["sensitivity"] == ["identity"]


def test_a_class_the_registry_no_longer_holds_never_stops_a_supersession(
    vault: Path, agent: Callable[..., dict]
) -> None:
    """A replacement refused over a class that the agent never named and cannot remove."""
    page = vault / "Knowledge Base/Notes/Old card.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("---\ntype: insight\ntitle: Old card\nsensitivity: [identity, confidential]\n---\n\n"
                    "# Old card\n\n## Observations\n\n- [finding] Test Person holds card X0000000 #admin\n",
                    encoding="utf-8")

    replaced = agent("supersede_note", {
        "note": "Old card", "title": "New card", "why": "The card was renewed",
        "unlinked_reason": "A standalone test note.",
        "content": "# New card\n\n## Observations\n\n- [finding] Test Person holds card X0000001 #admin\n",
    })

    assert "error" not in replaced, replaced
    [new] = vault.rglob("New card.md")
    assert _page(vault, new.relative_to(vault).as_posix())[0]["sensitivity"] == ["identity"]


@pytest.mark.usefixtures("owner_scope")
def test_an_episode_revision_keeps_the_class_and_a_different_declaration_is_refused(
    vault: Path, source_schema: Any
) -> None:
    """An episode revision that drops the class its page holds, or one that rewrites the class
    without the classify operation's reason record."""
    episode = "ep-" + "a1" * 16

    def record(**more: Any) -> dict:
        return commands.op_episode_memory(
            vault, source_schema, action="record", episode=episode, subject="Card renewal",
            summary="Filed the card renewal", **more,
        )

    first = record(worked_on=["Test Person card X0000000"], sensitivity=["identity"])
    assert first["sensitivity"]["classes"] == ["identity"]
    assert _page(vault, first["source"]["path"])[0]["sensitivity"] == ["identity"]

    revised = record(worked_on=["Test Person card X0000000", "Posted the form"])
    with pytest.raises(ValueError, match="EPISODE_INVALID.*already records its sensitivity"):
        record(worked_on=["Test Person card X0000000", "Posted the form", "Paid"], sensitivity=["health"])

    assert revised["sensitivity"]["classes"] == ["identity"]
    assert _page(vault, revised["source"]["path"])[0]["sensitivity"] == ["identity"]


CLINIC_MANIFEST = """---
type: collection
exomem_id: 7a1c3e52-4d0b-4f6a-8e21-9b3d5c7e1f40
title: Clinic visits
semantic_profile: records
collection_version: 1
schema_version: 1
lifecycle: active
sensitivity: [health]
storage:
  strategy: markdown-items
  source: Entries
  format_version: 1
item_schema:
  natural_key: [visit]
  fields:
    visit: {type: string, required: true}
    sensitivity: {type: array, items: {type: string}}
---
"""


def test_a_file_collection_refuses_a_class_floor_and_keeps_the_classes_a_row_names(
    vault: Path, agent: Callable[..., dict]
) -> None:
    """A floor that a file collection accepts and never applies, which reads as classified rows that are not,
    or an add_record `sensitivity` that never reaches the row."""
    create = {"manifest_path": "Knowledge Base/Records/Clinic/_collection.md", "why": "Track visits",
              "scaffold": True}
    refused = agent("manage_memory", {"operation": "records.create", "arguments": {
        **create, "manifest_text": CLINIC_MANIFEST}})
    assert refused["error"]["code"] == "INVALID_COLLECTION_MANIFEST"
    assert "only a store collection can declare one" in refused["error"]["message"]
    assert not (vault / create["manifest_path"]).exists()

    made = agent("manage_memory", {"operation": "records.create", "arguments": {
        **create, "manifest_text": CLINIC_MANIFEST.replace("sensitivity: [health]\n", "")}})
    assert "error" not in made, made
    unknown = agent("add_record", {"collection": "Clinic visits", "item": {"visit": "X-ray"}, "why": "Log it",
                                   "sensitivity": ["passport-ish"]})
    added = agent("add_record", {"collection": "Clinic visits", "item": {"visit": "Scan"}, "why": "Log a scan",
                                 "sensitivity": ["Financial"]})

    assert unknown["error"]["code"] == sensitivity_classes.UNKNOWN_CODE
    assert "error" not in added, added
    assert added["sensitivity"]["classes"] == ["financial"] and added["sensitivity"]["basis"] == "declared"
    [listed] = agent("query_records", {"collection": "Clinic visits"})["rows"]
    edited = agent("edit_record", {"collection": "Clinic visits", "record": listed["id"],
                                   "changes": {"sensitivity": ["passport-ish"]}, "why": "Retag it"})
    assert edited["error"]["code"] == sensitivity_classes.UNKNOWN_CODE, edited
    [row] = (vault / "Knowledge Base/Records/Clinic/Entries").glob("*.md")
    assert _page(vault, row.relative_to(vault).as_posix())[0]["sensitivity"] == ["financial"]
