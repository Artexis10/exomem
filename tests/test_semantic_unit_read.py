from __future__ import annotations

from pathlib import Path

import pytest

from exomem import commands, semantic_index, semantic_unit_read
from exomem import vault as vault_module
from exomem.get_page import GetResult

_PARENT_ID = "12345678-1234-5678-1234-567812345678"
_REL = "Knowledge Base/Notes/Insights/exact-unit-read.md"


def _write_page(
    vault: Path,
    body: str,
    *,
    status: str = "active",
    superseded_by: list[str] | None = None,
) -> Path:
    path = vault / _REL
    path.parent.mkdir(parents=True, exist_ok=True)
    successor = (
        f"superseded_by: {superseded_by!r}\n" if superseded_by is not None else ""
    )
    path.write_text(
        "---\n"
        "type: insight\n"
        f"exomem_id: {_PARENT_ID}\n"
        "title: Exact unit read\n"
        f"status: {status}\n"
        "created: 2026-07-16\n"
        "updated: 2026-07-16\n"
        f"{successor}"
        "sources: []\n"
        "tags: [semantic-units]\n"
        "---\n\n"
        f"{body}",
        encoding="utf-8",
    )
    return path


def _unit_ref(vault: Path, *, index: int = 0) -> str:
    state = semantic_index.current_parent_index_state(vault, _REL)
    ref = state.document.units[index].unit_ref
    assert ref is not None
    return ref


def test_read_memory_resolves_exact_unit_with_parent_citation_and_bounded_context(
    vault: Path,
) -> None:
    padding = "Before context.\n" * 200
    path = _write_page(
        vault,
        f"{padding}- [config] Session TTL is 30 minutes ^session-ttl\n"
        + ("After context.\n" * 200),
    )
    unit_ref = _unit_ref(vault)

    out = commands.op_read_memory(
        vault,
        path=f"exomem://memory/{_PARENT_ID}",
        unit_ref=unit_ref,
    )

    assert out["status"] == "found"
    assert out["unit"]["unit_ref"] == unit_ref
    assert out["unit"]["content"] == "Session TTL is 30 minutes"
    assert out["unit"]["source_hash"]
    assert out["parent"] == {
        "path": _REL,
        "ref": f"exomem://memory/{_PARENT_ID}",
        "title": "Exact unit read",
        "type": "insight",
        "status": "active",
        "updated": "2026-07-16",
        "superseded_by": [],
        "content_hash": vault_module.content_hash(path.read_text(encoding="utf-8")),
    }
    context = out["parent_context"]
    assert "- [config] Session TTL is 30 minutes ^session-ttl" in context["markdown"]
    assert len(context["markdown"]) <= 2400
    assert context["truncated_before"] is True
    assert context["truncated_after"] is True


def test_read_memory_reports_stale_anonymous_unit_without_nearest_match(
    vault: Path,
) -> None:
    _write_page(vault, "- [config] Session TTL is 30 minutes\n")
    old_ref = _unit_ref(vault)
    _write_page(
        vault,
        "- [config] Session TTL is 45 minutes\n"
        "- [config] Session TTL is 30 minutes nearby\n",
    )

    out = commands.op_read_memory(vault, path=_REL, unit_ref=old_ref)

    assert out["status"] == "stale"
    assert out["unit_ref"] == old_ref
    assert "unit" not in out
    assert "parent_context" not in out


def test_read_memory_reports_ambiguous_duplicate_anchor(vault: Path) -> None:
    _write_page(
        vault,
        "- [config] First ^same\n"
        "- [rule] Second ^same\n",
    )
    duplicate_ref = f"exomem://memory/{_PARENT_ID}#same"

    out = commands.op_read_memory(vault, path=_REL, unit_ref=duplicate_ref)

    assert out["status"] == "ambiguous"
    assert out["unit_ref"] == duplicate_ref
    assert "unit" not in out


def test_read_memory_reports_missing_anchor_without_substitution(vault: Path) -> None:
    _write_page(vault, "- [config] Current ^current\n")
    missing_ref = f"exomem://memory/{_PARENT_ID}#missing"

    out = commands.op_read_memory(vault, path=_REL, unit_ref=missing_ref)

    assert out["status"] == "missing"
    assert out["unit_ref"] == missing_ref
    assert "unit" not in out


def test_read_memory_marks_exact_unit_on_superseded_parent(vault: Path) -> None:
    _write_page(
        vault,
        "- [decision] Retired decision ^retired\n",
        status="superseded",
        superseded_by=["Knowledge Base/Notes/Insights/current.md"],
    )
    unit_ref = _unit_ref(vault)

    out = commands.op_read_memory(vault, path=_REL, unit_ref=unit_ref)

    assert out["status"] == "superseded"
    assert out["unit"]["unit_ref"] == unit_ref
    assert out["parent"]["status"] == "superseded"
    assert out["parent"]["superseded_by"] == [
        "Knowledge Base/Notes/Insights/current.md"
    ]


def test_read_memory_default_page_contract_is_unchanged(vault: Path) -> None:
    _write_page(vault, "- [config] Session TTL is 30 minutes ^session-ttl\n")

    assert commands.op_read_memory(vault, path=_REL) == commands.op_get(
        vault,
        path=_REL,
    )


def test_exact_unit_read_rejects_unbounded_page_expansion(vault: Path) -> None:
    _write_page(vault, "- [config] Current ^current\n")

    with pytest.raises(ValueError, match="INVALID_UNIT_READ_OPTIONS"):
        commands.op_read_memory(
            vault,
            path=_REL,
            unit_ref=_unit_ref(vault),
            include_raw=True,
        )


def test_exact_unit_read_uses_one_raw_snapshot_for_unit_parent_and_context(
    vault: Path,
) -> None:
    path = _write_page(vault, "- [config] New value ^current\n")
    content = path.read_text(encoding="utf-8")
    unit_ref = _unit_ref(vault)
    deliberately_incoherent_page = GetResult(
        path=_REL,
        frontmatter={
            "type": "pattern",
            "title": "Old title",
            "status": "superseded",
            "updated": "2020-01-01",
        },
        body="- [config] Old value ^current\n",
        content=content,
        content_hash=vault_module.content_hash(content),
        mtime=path.stat().st_mtime,
    )

    out = semantic_unit_read.read_semantic_unit(
        vault,
        page=deliberately_incoherent_page,
        unit_ref=unit_ref,
    ).as_dict()

    assert out["status"] == "found"
    assert out["unit"]["content"] == "New value"
    assert "New value" in out["parent_context"]["markdown"]
    assert "Old value" not in out["parent_context"]["markdown"]
    assert out["parent"]["title"] == "Exact unit read"
    assert out["parent"]["type"] == "insight"
    assert out["parent"]["status"] == "active"
