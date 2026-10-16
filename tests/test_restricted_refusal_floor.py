"""A restricted caller cannot time a direct read to tell withheld from missing.

Every refusal of a direct read that a restricted caller receives completes at
the fixed disclosure floor. Successes, notices included, and every owner
refusal return without it.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

import pytest

from exomem import commands, memory_refs, writer_lease
from exomem.governance import egress, membership, policy, projection_timing, receipts
from exomem.governance.principal import RequestPrincipal, library_scope, request_scope

READ_MEMORY = next(item for item in commands.PRODUCT_COMMANDS if item.name == "read_memory")
EXTERNAL = RequestPrincipal(audience_id="external", surface="mcp")


def _restrict_external(vault: Path, *, folder: str, ceiling: int) -> None:
    """Release `Notes/<folder>/**` to `external` at `ceiling`."""
    governance = vault / "Knowledge Base" / "_Governance"
    (governance / "scopes").mkdir(parents=True, exist_ok=True)
    (governance / "rules").mkdir(parents=True, exist_ok=True)
    (governance / "scopes" / "example.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FC1\n"
        f'name: example\npaths: ["Notes/{folder}/**"]\n',
        encoding="utf-8",
    )
    (governance / "rules" / "example.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FC2\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FC1"]\n'
        f"audience: external\nceiling: {ceiling}\n",
        encoding="utf-8",
    )
    policy._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()


def _write_page(vault: Path, relative: str, identity: str) -> None:
    target = vault / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        f"---\ntype: insight\nexomem_id: {identity}\n---\n\n# Example\n\nExample body.\n",
        encoding="utf-8",
    )


def _timed_refusal(vault: Path, target: str, **arguments: str) -> tuple[float, str]:
    started = time.perf_counter()
    with pytest.raises(ValueError) as caught:
        writer_lease.invoke_command(READ_MEMORY, vault, path=target, **arguments)
    return time.perf_counter() - started, f"{type(caught.value).__name__}: {caught.value}"


def test_restricted_refusals_complete_at_the_floor_and_read_alike(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    floor_s = projection_timing.RESTRICTED_REFUSAL_FLOOR_MS / 1000.0
    identity = memory_refs.new_id()
    page = "Knowledge Base/Notes/Hidden/withheld-example.md"
    ref = memory_refs.memory_ref(identity)
    unit = f"{ref}#example-unit"
    _write_page(vault, page, identity)
    _restrict_external(vault, folder="Hidden", ceiling=egress.LEVEL_NONE)

    with request_scope(EXTERNAL):
        withheld_path = _timed_refusal(vault, page)
        hidden_only_ref = _timed_refusal(vault, ref)
        withheld_unit = _timed_refusal(vault, page, unit_ref=unit)
    (vault / page).unlink()
    with request_scope(EXTERNAL):
        missing_path = _timed_refusal(vault, page)
        absent_ref = _timed_refusal(vault, ref)
        missing_unit = _timed_refusal(vault, page, unit_ref=unit)

    for elapsed, _body in (
        withheld_path, hidden_only_ref, withheld_unit, missing_path, absent_ref, missing_unit
    ):
        assert elapsed >= floor_s
    assert withheld_path[1] == missing_path[1]
    assert hidden_only_ref[1] == absent_ref[1]
    assert withheld_unit[1] == missing_unit[1]

    # Nothing can be withheld from the owner, so its refusal is never padded.
    # A floor raised far above a refusal's cost makes any padding unmistakable.
    monkeypatch.setattr(projection_timing, "RESTRICTED_REFUSAL_FLOOR_MS", 2_000)
    with library_scope():
        owner_elapsed, _body = _timed_refusal(vault, page)
    assert owner_elapsed < 1.0


def test_an_early_refusal_leaves_the_full_withheld_receipt(vault: Path) -> None:
    """The owner's audit record of a refused read keeps its hash, size and ref."""
    identity = memory_refs.new_id()
    page = "Knowledge Base/Notes/Hidden/withheld-example.md"
    _write_page(vault, page, identity)
    _restrict_external(vault, folder="Hidden", ceiling=egress.LEVEL_NONE)
    held = (vault / page).read_bytes()
    recorded = len(receipts.event_records(vault))

    with request_scope(EXTERNAL):
        _timed_refusal(vault, page)

    (record,) = receipts.event_records(vault)[recorded:]
    (outcome,) = record["outcomes"]
    assert outcome["decision"] == "withheld"
    assert outcome["level"] == egress.LEVEL_NONE
    assert outcome["content_hash"] == hashlib.sha256(held).hexdigest()
    assert outcome["size"] == len(held)
    assert outcome["ref"] == memory_refs.memory_ref(identity)


def test_a_restricted_notice_returns_without_the_floor(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = "Knowledge Base/Notes/Noticed/noticed-example.md"
    _write_page(vault, page, memory_refs.new_id())
    _restrict_external(vault, folder="Noticed", ceiling=egress.LEVEL_NOTICE)
    monkeypatch.setattr(projection_timing, "RESTRICTED_REFUSAL_FLOOR_MS", 2_000)

    started = time.perf_counter()
    with request_scope(EXTERNAL):
        result = writer_lease.invoke_command(READ_MEMORY, vault, path=page)
    elapsed = time.perf_counter() - started

    assert result["withheld"] is True
    assert result["level"] == egress.LEVEL_NOTICE
    assert "path" not in result
    assert elapsed < 1.0
