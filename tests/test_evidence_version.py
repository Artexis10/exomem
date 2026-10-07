from __future__ import annotations

from pathlib import Path

import pytest

from exomem import commands, memory_refs, semantic_index
from exomem.episode_recovery import EpisodeInputOwner
from exomem.governance.principal import owner_principal, request_scope
from exomem.writer_lease import invoke_command

_REL = "Knowledge Base/Sources/material-version.md"
_ID = "12345678-1234-5678-1234-567812345678"


def _write(vault: Path, metadata: str = "", body: str = "Retained evidence.\n") -> Path:
    path = vault / _REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntype: source\nexomem_id: {_ID}\n{metadata}---\n\n{body}",
        encoding="utf-8",
    )
    return path


def test_material_version_binds_parsed_metadata_types_and_body(vault: Path) -> None:
    """YAML reformatting/backlinks stay stable; typed metadata and evidence do not."""
    _write(vault, "updated: 2026-10-02\nstatus: active\ncapture: {count: 1, label: sample}\n")
    before = commands.op_get(vault, path=_REL)
    assert "evidence_version" in before
    version = before["evidence_version"]
    _write(
        vault,
        "capture:\n  label: sample\n  count: 1\nstatus: active\n"
        "updated: 2026-10-02\ningested_into: ['A compiled destination']\n",
    )
    after = commands.op_get(vault, path=_REL)
    assert after["evidence_version"] == version
    assert after["content_hash"] != before["content_hash"]
    for metadata, body in (
        (
            'updated: "2026-10-02"\nstatus: active\ncapture: {count: 1, label: sample}\n',
            "Retained evidence.\n",
        ),
        (
            "updated: 2026-10-03\nstatus: active\ncapture: {count: 1, label: sample}\n",
            "Retained evidence.\n",
        ),
        (
            "updated: 2026-10-02\nstatus: draft\ncapture: {count: 1, label: sample}\n",
            "Retained evidence.\n",
        ),
        (
            "updated: 2026-10-02\nstatus: active\ncapture: {count: '1', label: sample}\n",
            "Retained evidence.\n",
        ),
        (
            "updated: 2026-10-02\nstatus: active\ncapture: {count: 1, label: sample}\n",
            "Changed retained evidence.\n",
        ),
    ):
        _write(vault, metadata, body)
        assert commands.op_get(vault, path=_REL)["evidence_version"] != version


def test_material_version_requires_a_complete_page_or_current_exact_unit(vault: Path) -> None:
    """A cap, metadata-only read or stale unit cannot bind unseen evidence."""
    _write(vault, body="- [finding] Retained evidence ^retained\n")
    full = commands.op_get(vault, path=_REL)
    assert "evidence_version" in full
    assert (
        commands.op_get(vault, path=_REL, max_body_chars=12000)["evidence_version"]
        == full["evidence_version"]
    )
    assert "evidence_version" not in commands.op_get(vault, path=_REL, frontmatter_only=True)
    assert "evidence_version" not in commands.op_get(vault, path=_REL, max_body_chars=10)
    unit_ref = semantic_index.current_parent_index_state(vault, _REL).document.units[0].unit_ref
    assert unit_ref
    unit = commands.op_read_memory(vault, path=_REL, unit_ref=unit_ref)
    assert unit["status"] == "found"
    assert unit["evidence_version"] == full["evidence_version"]
    missing = commands.op_read_memory(
        vault, path=_REL, unit_ref=f"{memory_refs.memory_ref(_ID)}#missing"
    )
    assert "evidence_version" not in missing
    _write(vault, "status: superseded\n", "- [finding] Retained evidence ^retained\n")
    superseded = commands.op_read_memory(vault, path=_REL, unit_ref=unit_ref)
    assert superseded["status"] == "superseded"
    assert "evidence_version" not in superseded


@pytest.mark.parametrize("metadata", ["capture: .nan\n", "capture: 1\ncapture: 2\n"])
def test_invalid_material_metadata_has_no_binding(vault: Path, metadata: str) -> None:
    """Nonfinite values and duplicate YAML keys cannot masquerade as retained inputs."""
    from exomem.provenance import evidence_version

    path = _write(vault, metadata)
    with pytest.raises(ValueError):
        evidence_version(path.read_text(encoding="utf-8"))
    assert "evidence_version" not in commands.op_get(vault, path=_REL)


@pytest.mark.parametrize("redact_in", ["body", "title"])
def test_terminal_projection_cannot_claim_complete_evidence(vault: Path, redact_in: str) -> None:
    """Page and unit bindings cannot survive changed text or parent metadata."""
    command = next(item for item in commands.PRODUCT_COMMANDS if item.name == "read_memory")
    secret = "Authorization: Bearer sk-proj-9dQm2XvKpLzR4wTnBcYeF8aHgJ1sVuNiO0rEyMdA"
    clean_body = "- [finding] Retained evidence ^retained\n"
    with request_scope(owner_principal(surface="mcp")):
        _write(vault, body=clean_body)
        assert "evidence_version" in invoke_command(command, vault, path=_REL)
        body = f"- [finding] {secret} ^retained\n" if redact_in == "body" else clean_body
        metadata = f'title: "{secret}"\n' if redact_in == "title" else ""
        _write(vault, metadata=metadata, body=body)
        reference = semantic_index.current_parent_index_state(vault, _REL).document.units[0].unit_ref
        assert reference
        full = invoke_command(command, vault, path=_REL)
        unit = invoke_command(command, vault, path=_REL, unit_ref=reference)
    assert unit["status"] == "found"
    assert "evidence_version" not in full
    assert "evidence_version" not in unit
    assert secret not in str(full)
    assert secret not in str(unit)


def test_unrepresentable_timestamp_does_not_break_page_or_recap_binding(vault: Path) -> None:
    """Safe YAML outside the canonical timestamp range has no material binding."""
    _write(vault, "capture: 0001-01-01T00:00:00+14:00\n")
    with request_scope(owner_principal(surface="mcp")):
        result = commands.op_get(vault, path=_REL)
        committed = EpisodeInputOwner(vault).bind_committed_input(
            "out-of-range-time", path=_REL, reference=memory_refs.memory_ref(_ID)
        )
    assert result["body"] == "Retained evidence.\n"
    assert "evidence_version" not in result
    assert committed["ledger"] == "digest_only"
    assert committed["recovery"] == "unavailable"
