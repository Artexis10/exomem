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

from exomem import client_artifacts, commands, sensitivity_classes
from exomem import server as server_module
from exomem import writer_lease
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
