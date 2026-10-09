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


def test_final_input_checkpoint_refreshes_verified_session_status(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The final checkpoint must not accept a dispatcher context after its session closes."""
    from test_authorization_session_lifecycle import NOW, _custody, _file_connection

    from exomem.governance import authorization_session_lifecycle, store

    database = store.sidecar_path(vault)
    database.parent.mkdir(parents=True, exist_ok=True)
    connection, migration = _file_connection(database)
    custody = _custody(migration.activation_state_digest)
    opened = authorization_session_lifecycle.open_session(
        connection,
        custody=custody,
        principal_id="client-a",
        issuer_family="cli-local-owner",
        now=NOW,
        ttl_seconds=600,
    )
    monkeypatch.setattr(retained_inputs.time, "time", lambda: NOW + 1)
    monkeypatch.setattr(
        retained_inputs.authorization_custody,
        "load_authorization_custody",
        lambda root, *, now: custody,
    )
    principal = RequestPrincipal(audience_id="client-a").with_verified_authorization_session(
        opened.context, issuer_family="cli-local-owner"
    )
    try:
        with request_scope(principal):
            retained_inputs.recheck_retained_inputs(vault, ())
            authorization_session_lifecycle.close_verified_session(
                connection, custody=custody, context=opened.context, now=NOW + 1
            )
            with pytest.raises(retained_inputs.RetainedInputError) as failure:
                retained_inputs.recheck_retained_inputs(vault, ())
        assert failure.value.code == "RETAINED_INPUT_UNAVAILABLE"
    finally:
        connection.close()


def test_private_status_blocks_new_binding_but_not_retained_unit_disclosure(vault: Path) -> None:
    from test_governance_egress import _external, _reset_caches, write_rule, write_scope

    from exomem import commands
    from exomem.governance.principal import library_scope

    path = "Knowledge Base/Notes/Insights/retained-harbour.md"
    reference = "exomem://memory/12345678-1234-5678-1234-567812345678#exact"
    page = vault / path
    page.parent.mkdir(parents=True, exist_ok=True)
    original = (
        "---\ntype: insight\nexomem_id: 12345678-1234-5678-1234-567812345678\n"
        "status: closed-locally\n---\n\n## Observations\n\n- [finding] The harbour closes at dusk. ^exact\n"
    )
    page.write_text(original)
    with library_scope():
        prior = retained_inputs.resolve_retained_input(vault, reference, committed_path=path)
        assert prior.unit.unit_ref == reference
        inspected = commands.op_schema_memory(vault, subject="statuses", operation="inspect")
        commands.op_schema_memory(
            vault,
            subject="statuses",
            operation="save",
            proposal={"upsert": {"closed-locally": {"attributes": {"class": "superseded"}}}},
            expected_hash=inspected["content_hash"],
            why="the input is superseded",
        )
        with pytest.raises(retained_inputs.RetainedInputError):
            retained_inputs.resolve_retained_input(vault, reference, committed_path=path)
    write_scope(vault, paths="_Schema/statuses.yaml", name="Private statuses")
    write_rule(vault, ceiling=0)
    _reset_caches()
    with request_scope(_external()):
        with pytest.raises(retained_inputs.RetainedInputError):
            retained_inputs.resolve_retained_input(vault, reference, committed_path=path)
        disclosed = retained_inputs.resolve_retained_input(
            vault, reference, committed_path=path, disclosure=True
        )
        assert disclosed.unit.unit_ref == reference
        assert disclosed.unit.span.text == "- [finding] The harbour closes at dusk. ^exact"
        retained_inputs.recheck_retained_inputs(
            vault, ((disclosed, "The harbour closes at dusk."),), disclosure=True
        )
    assert page.read_text() == original
