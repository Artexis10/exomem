"""link tool tests — typed entity creation under Entities/<Type>/."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
import yaml

from kb_mcp import link as link_module


TODAY = dt.date(2026, 5, 25)


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _fm(p: Path) -> dict:
    fm = _read(p).split("\n---\n")[0].removeprefix("---\n")
    return yaml.safe_load(fm)


def test_link_creates_person_entity(vault: Path) -> None:
    result = link_module.link(
        vault,
        entity_type="person",
        name="Jane Goodall",
        summary="Primatologist; long-running chimpanzee field studies.",
        affiliation="Jane Goodall Institute",
        relationship="referenced",
        today=TODAY,
    )
    assert result.path == "Knowledge Base/Entities/People/Jane Goodall.md"
    written = vault / result.path
    assert written.exists()
    fm = _fm(written)
    assert fm["type"] == "entity"
    assert fm["entity_type"] == "person"
    assert fm["affiliation"] == "Jane Goodall Institute"
    assert fm["relationship"] == "referenced"
    assert "# Jane Goodall" in _read(written)


def test_link_creates_concept_entity_with_domain(vault: Path) -> None:
    result = link_module.link(
        vault,
        entity_type="concept",
        name="Backpressure",
        summary="Reactive-system flow control mechanism.",
        domain="infrastructure",
        today=TODAY,
    )
    written = vault / result.path
    fm = _fm(written)
    assert fm["entity_type"] == "concept"
    assert fm["domain"] == "infrastructure"


def test_link_creates_library_entity_with_metadata(vault: Path) -> None:
    result = link_module.link(
        vault,
        entity_type="library",
        name="pgvector",
        summary="Postgres extension for vector similarity search.",
        language="C",
        repo="https://github.com/pgvector/pgvector",
        license="PostgreSQL",
        used_in=["q", "endstate"],
        today=TODAY,
    )
    written = vault / result.path
    fm = _fm(written)
    assert fm["entity_type"] == "library"
    assert fm["language"] == "C"
    assert fm["repo"] == "https://github.com/pgvector/pgvector"
    assert fm["license"] == "PostgreSQL"
    assert fm["used_in"] == ["q", "endstate"]


def test_link_creates_decision_entity_with_status(vault: Path) -> None:
    result = link_module.link(
        vault,
        entity_type="decision",
        name="Use Tailscale Funnel Over Cloudflare Tunnel",
        summary="Self-hosted MCP exposure decision.",
        decided="2026-05-18",
        project="substrate",
        decision_status="accepted",
        today=TODAY,
    )
    written = vault / result.path
    fm = _fm(written)
    assert fm["entity_type"] == "decision"
    assert fm["decided"] == dt.date(2026, 5, 18)  # YAML parses ISO date
    assert fm["project"] == "substrate"
    assert fm["decision_status"] == "accepted"


def test_link_rejects_invalid_entity_type(vault: Path) -> None:
    with pytest.raises(link_module.LinkError) as exc:
        link_module.link(
            vault,
            entity_type="bogus",
            name="X",
            summary="y",
            today=TODAY,
        )
    assert exc.value.code == "INVALID_LINK"
    assert "entity_type" in exc.value.missing


def test_link_rejects_missing_name(vault: Path) -> None:
    with pytest.raises(link_module.LinkError) as exc:
        link_module.link(
            vault,
            entity_type="person",
            name="",
            summary="y",
            today=TODAY,
        )
    assert exc.value.code == "INVALID_LINK"
    assert "name" in exc.value.missing


def test_link_rejects_missing_summary(vault: Path) -> None:
    with pytest.raises(link_module.LinkError) as exc:
        link_module.link(
            vault,
            entity_type="concept",
            name="X",
            summary="",
            today=TODAY,
        )
    assert exc.value.code == "INVALID_LINK"
    assert "summary" in exc.value.missing


def test_link_rejects_invalid_decision_status(vault: Path) -> None:
    with pytest.raises(link_module.LinkError) as exc:
        link_module.link(
            vault,
            entity_type="decision",
            name="X",
            summary="y",
            decision_status="maybe",
            today=TODAY,
        )
    assert exc.value.code == "INVALID_LINK"
    assert "decision_status" in exc.value.missing


def test_link_refuses_when_entity_exists(vault: Path) -> None:
    link_module.link(
        vault,
        entity_type="concept",
        name="Throughput",
        summary="x",
        today=TODAY,
    )
    with pytest.raises(link_module.LinkError) as exc:
        link_module.link(
            vault,
            entity_type="concept",
            name="Throughput",
            summary="y",
            today=TODAY,
        )
    assert exc.value.code == "ENTITY_EXISTS"


def test_link_normalizes_tags(vault: Path) -> None:
    result = link_module.link(
        vault,
        entity_type="person",
        name="Tag Test Person",
        summary="x",
        tags=["UPPER", "with space", "with_under", "dupe", "DUPE"],
        today=TODAY,
    )
    fm = _fm(vault / result.path)
    assert fm["tags"] == ["upper", "with-space", "with-under", "dupe"]


def test_link_appends_log_entry(vault: Path) -> None:
    log_file = vault / "Knowledge Base" / "log.md"
    link_module.link(
        vault,
        entity_type="library",
        name="Logged Lib",
        summary="x",
        today=TODAY,
    )
    text = _read(log_file)
    assert "## [2026-05-25] link | Entities/Libraries/Logged Lib" in text


def test_link_connections_normalize_and_render(vault: Path) -> None:
    result = link_module.link(
        vault,
        entity_type="concept",
        name="Connected Concept",
        summary="x",
        connections=[
            "Knowledge Base/Notes/Insights/foo",
            "[[Notes/Patterns/bar]]",
            "  Cognitive Core/Strategy  ",
        ],
        today=TODAY,
    )
    text = _read(vault / result.path)
    # Each connection rendered as a bullet
    assert "- [[Knowledge Base/Notes/Insights/foo]]" in text
    assert "- [[Knowledge Base/Notes/Patterns/bar]]" in text
    assert "- [[Knowledge Base/Cognitive Core/Strategy]]" in text
