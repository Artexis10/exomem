from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from exomem import retained_inputs
from exomem.episode_recovery import EpisodeInputOwner
from exomem.governance import egress
from exomem.governance.principal import RequestPrincipal, request_scope


def test_retained_read_preserves_logical_path_and_physical_snapshot_guard(vault: Path) -> None:
    """A logical receipt path must retain an exact guard on its different NFD filename."""
    physical = "Knowledge Base/Sources/cafe\u0301.md"
    logical = "Knowledge Base/Sources/café.md"
    reference = "exomem://memory/12345678-1234-5678-1234-567812345678"
    source = (
        "---\r\ntype: source\r\nexomem_id: 12345678-1234-5678-1234-567812345678\r\n"
        "status: active\r\n---\r\n\r\n- [finding] Exact α text ^exact\r\n"
    ).encode()
    path = vault / physical
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(source)
    with request_scope(RequestPrincipal(audience_id="client-a")):
        with egress.disclosure_boundary(vault, "retained-read") as collector:
            result = retained_inputs.resolve_retained_input(
                vault, reference + "#exact", committed_path=logical
            )

    assert result.page.path == result.released["path"] == logical
    assert result.page.content.encode() == source
    assert result.guard.target == physical
    assert result.guard.expected_content_hash == hashlib.sha256(source).hexdigest()
    assert result.guard.expected_content_size == len(source)
    result.guard.recheck(vault)
    assert result.unit.unit_ref == reference + "#exact"
    assert result.unit.span.text == "- [finding] Exact α text ^exact"
    assert result.authorization is None
    assert collector.outcomes == []


def test_snapshot_preparation_refuses_a_disappearing_physical_leaf(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deletion between the prepared read and guard capture stays a content-free refusal."""
    relative = "Knowledge Base/Sources/disappearing.md"
    path = vault / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    source = "---\ntype: source\nexomem_id: 12345678-1234-5678-1234-567812345678\n---\nRetained.\n"
    path.write_text(source, encoding="utf-8")
    original = retained_inputs.get_page

    def disappear_after_read(*args, **kwargs):
        page = original(*args, **kwargs)
        path.unlink()
        return page

    monkeypatch.setattr(retained_inputs, "get_page", disappear_after_read)
    with request_scope(RequestPrincipal(audience_id="client-a")):
        with pytest.raises(retained_inputs.RetainedInputError) as failure:
            retained_inputs.resolve_retained_input(
                vault,
                "exomem://memory/12345678-1234-5678-1234-567812345678",
                committed_path=relative,
            )
    assert failure.value.code == "RETAINED_INPUT_UNAVAILABLE"
    assert failure.value.reason == "input is unavailable"
    path.write_text(source, encoding="utf-8")
    with request_scope(RequestPrincipal(audience_id="client-a")):
        owner = EpisodeInputOwner(vault)
        bound = owner.bind_committed_input(
            "preparation-swap",
            path=relative,
            reference="exomem://memory/12345678-1234-5678-1234-567812345678",
        )
        assert bound["ledger"] == "digest_only"
        assert owner.recover_input(bound["episode_id"]) == {
            "status": "unavailable",
            "input_revision": 1,
        }
