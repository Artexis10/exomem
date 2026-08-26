"""Direct-read payload and governance projection contracts."""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from exomem import commands, mutation_lock, writer_lease
from exomem.governance import authorization_custody, egress, policy
from exomem.governance.principal import RequestPrincipal, request_scope
from exomem.vault import content_hash


def _page(vault: Path) -> str:
    p = vault / "Knowledge Base" / "Notes" / "get-payload-probe.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "---\ntype: research-note\nproject: project-alpha\n---\n\n"
        "# Get payload probe\n\nbody text here\n",
        encoding="utf-8",
    )
    return "Knowledge Base/Notes/get-payload-probe.md"


def _govern(vault: Path, *, ceiling: int, options: str = "") -> None:
    governance = vault / "Knowledge Base" / "_Governance"
    (governance / "scopes").mkdir(parents=True, exist_ok=True)
    (governance / "rules").mkdir(parents=True, exist_ok=True)
    (governance / "scopes" / "notes.yaml").write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FAV\n"
        "name: Notes\n"
        'paths: ["Notes/**"]\n',
        encoding="utf-8",
    )
    (governance / "rules" / "external.yaml").write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FB0\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FAV"]\n'
        "audience: external\n"
        f"ceiling: {ceiling}\n"
        f"{options}",
        encoding="utf-8",
    )
    from exomem.governance import membership, policy

    policy._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()


def _external() -> RequestPrincipal:
    return RequestPrincipal(audience_id="external", surface="mcp")


@pytest.fixture
def registry_proven_never_enrolled(
    vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    external = tmp_path / "authorization-custody"
    external.mkdir(mode=0o700)
    lease_state = tmp_path / "writer-lease-state"
    lease_state.mkdir(mode=0o700)
    if os.name == "nt":  # pragma: no cover - exercised by Windows CI
        sid = mutation_lock._windows_current_user_sid()
        mutation_lock._windows_apply_private_dacl(external, sid)
        mutation_lock._windows_apply_private_dacl(lease_state, sid)
    monkeypatch.setenv(
        authorization_custody.KEYRING_FILE_ENV,
        str(external / "authorization-keyring.json"),
    )
    monkeypatch.setenv(
        authorization_custody.CONTROL_FILE_ENV,
        str(external / "authorization-control.json"),
    )
    monkeypatch.setenv(
        authorization_custody.MEMBERSHIP_FILE_ENV,
        str(external / "authorization-serving-membership.json"),
    )
    monkeypatch.setenv(authorization_custody.REPLICA_ID_ENV, "standalone")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(lease_state))
    writer_lease.reset_managers_for_tests()
    authorization_custody.provision_standalone_custody(vault, now=int(time.time()))
    policy._CACHE.clear()
    egress.clear_decision_memo()
    try:
        yield
    finally:
        policy._CACHE.clear()
        egress.clear_decision_memo()
        writer_lease.reset_managers_for_tests()


def test_default_get_has_no_content_key(vault: Path) -> None:
    rel = _page(vault)
    out = commands.op_get(vault, path=rel)
    assert "content" not in out
    assert set(out) >= {"path", "frontmatter", "body", "content_hash", "mtime"}
    assert out["body"].startswith("# Get payload probe")


def test_include_raw_returns_disk_bytes(vault: Path) -> None:
    rel = _page(vault)
    out = commands.op_get(vault, path=rel, include_raw=True)
    # `read_text` normalizes newlines, so it cannot witness disk bytes.
    assert out["content"] == (vault / rel).read_bytes().decode("utf-8")
    assert out["content_hash"] == content_hash(out["content"])


def test_drift_guard_roundtrip_without_content(vault: Path) -> None:
    """edit(expected_hash=get().content_hash) still works — the hash is
    server-computed over raw bytes; callers never need `content`."""
    rel = _page(vault)
    got = commands.op_get(vault, path=rel)
    edited = commands.op_edit(
        vault, path=rel,
        new_body=got["body"] + "\nappended line\n",
        expected_hash=got["content_hash"],
        why="payload dedup roundtrip test",
    )
    assert "appended line" in (vault / rel).read_text(encoding="utf-8")
    assert edited


def test_frontmatter_only_unaffected(vault: Path) -> None:
    rel = _page(vault)
    out = commands.op_get(vault, path=rel, frontmatter_only=True)
    assert "content" not in out
    assert out["frontmatter"]["type"] == "research-note"


def test_history_and_links_compose(vault: Path) -> None:
    rel = _page(vault)
    out = commands.op_get(vault, path=rel, include_history=True, links=True)
    assert "content" not in out
    assert "history" in out and "links" in out


@pytest.mark.parametrize("ceiling", range(1, 6))
def test_include_raw_is_identical_to_default_below_l6(
    vault: Path, ceiling: int
) -> None:
    rel = _page(vault)
    _govern(vault, ceiling=ceiling)

    with request_scope(_external()):
        default = commands.op_get(
            vault,
            path=rel,
            include_history=True,
            links=True,
        )
        ordinary = commands.op_get(
            vault,
            path=rel,
            include_history=True,
            links=True,
            include_raw=False,
        )
        raw_requested = commands.op_get(
            vault,
            path=rel,
            include_history=True,
            links=True,
            include_raw=True,
        )
        frontmatter_only = commands.op_get(
            vault,
            path=rel,
            frontmatter_only=True,
            include_raw=True,
        )

    assert default == ordinary
    assert raw_requested == ordinary
    assert frontmatter_only == ordinary
    assert "content" not in ordinary
    assert "content_hash" not in ordinary
    assert "frontmatter" not in ordinary
    assert "history" not in ordinary
    assert "links" not in ordinary
    if ceiling == egress.LEVEL_EXCERPT:
        assert ordinary["body"] == "# Get payload probe body text here"
        assert ordinary["body_truncated"] is True


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"include_raw": False},
        {"include_raw": True},
        {"frontmatter_only": True},
    ],
)
def test_l0_read_is_byte_identical_to_same_input_missing(
    vault: Path, options: dict[str, bool]
) -> None:
    rel = _page(vault)
    _govern(vault, ceiling=egress.LEVEL_NONE)

    with request_scope(_external()), pytest.raises(ValueError) as present:
        commands.op_get(vault, path=rel, **options)
    (vault / rel).unlink()
    with request_scope(_external()), pytest.raises(ValueError) as absent:
        commands.op_get(vault, path=rel, **options)

    assert str(present.value) == str(absent.value)


def test_l6_raw_is_exact_and_default_remains_raw_free(vault: Path) -> None:
    rel = _page(vault)
    _govern(vault, ceiling=egress.LEVEL_FULL)

    with request_scope(_external()):
        ordinary = commands.op_get(vault, path=rel)
        explicit_false = commands.op_get(vault, path=rel, include_raw=False)
        frontmatter_only = commands.op_get(vault, path=rel, frontmatter_only=True)
        raw = commands.op_get(vault, path=rel, include_raw=True)

    assert explicit_false == ordinary
    assert "content" not in ordinary
    assert frontmatter_only == {
        "path": rel,
        "frontmatter": {
            "type": "research-note",
            "project": "project-alpha",
        },
        "has_frontmatter": True,
    }
    assert raw["content"] == (vault / rel).read_bytes().decode("utf-8")
    assert raw["content_hash"] == ordinary["content_hash"]


def test_registry_proven_never_enrolled_preserves_l6_shapes_and_edit_guard(
    vault: Path,
    registry_proven_never_enrolled: None,
) -> None:
    rel = _page(vault)
    target = vault / rel
    exact_raw = (
        b"---\r\ntype: research-note\r\nproject: project-alpha\r\n---\r\n\r\n"
        b"# Registry-proven page\r\n\r\nbyte exact body\r\n"
    )
    target.write_bytes(exact_raw)

    with request_scope(RequestPrincipal(audience_id="owner", surface="cli")):
        ordinary = commands.op_get(vault, path=rel)
        explicit_false = commands.op_get(vault, path=rel, include_raw=False)
        frontmatter_only = commands.op_get(vault, path=rel, frontmatter_only=True)
        raw = commands.op_get(vault, path=rel, include_raw=True)
        edited = commands.op_edit(
            vault,
            path=rel,
            new_body=ordinary["body"] + "\ncommitted with the read hash\n",
            expected_hash=ordinary["content_hash"],
            why="prove never-enrolled get/edit hash compatibility",
        )

    assert explicit_false == ordinary
    assert set(ordinary) == {"path", "frontmatter", "body", "content_hash", "mtime"}
    assert frontmatter_only == {
        "path": rel,
        "frontmatter": {
            "type": "research-note",
            "project": "project-alpha",
        },
        "has_frontmatter": True,
    }
    assert raw["content"] == exact_raw.decode("utf-8")
    assert raw["content_hash"] == ordinary["content_hash"] == content_hash(
        exact_raw.decode("utf-8")
    )
    assert edited
    assert "committed with the read hash" in target.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "secret",
    [
        "AKIA" + "IOSFODNN7EXAMPLE",
        (
            "as1."
            + "AAECAwQFBgcICQoLDA0ODw"
            + "."
            + "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
        ),
    ],
)
def test_registry_proven_never_enrolled_secret_raw_is_blocked_and_edit_stays_stale(
    vault: Path,
    registry_proven_never_enrolled: None,
    secret: str,
) -> None:
    rel = _page(vault)
    target = vault / rel
    target.write_text(
        target.read_text(encoding="utf-8") + f"\n{secret}\n",
        encoding="utf-8",
    )

    with request_scope(RequestPrincipal(audience_id="owner", surface="cli")):
        ordinary = commands.op_get(vault, path=rel)
        with pytest.raises(ValueError, match="^SECRET_BLOCKED:") as blocked:
            commands.op_get(vault, path=rel, include_raw=True)
        before_drift = target.read_text(encoding="utf-8")
        target.write_text(
            before_drift + "\nout-of-band drift\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="^STALE_EDIT:"):
            commands.op_edit(
                vault,
                path=rel,
                new_body=ordinary["body"] + "\nmust not commit\n",
                expected_hash=ordinary["content_hash"],
                why="prove secret refusal does not alter stale edit semantics",
            )

    assert secret not in str(blocked.value)
    assert ordinary["content_hash"] == content_hash(before_drift)
    assert "must not commit" not in target.read_text(encoding="utf-8")


@pytest.mark.parametrize("governed", [False, True])
@pytest.mark.parametrize(
    "secret",
    [
        "AKIA" + "IOSFODNN7EXAMPLE",
        (
            "as1."
            + "AAECAwQFBgcICQoLDA0ODw"
            + "."
            + "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
        ),
    ],
)
def test_include_raw_secret_is_content_free_and_hash_semantics_survive(
    vault: Path, governed: bool, secret: str
) -> None:
    rel = _page(vault)
    target = vault / rel
    target.write_text(target.read_text(encoding="utf-8") + f"\n{secret}\n", encoding="utf-8")
    if governed:
        _govern(vault, ceiling=egress.LEVEL_FULL)

    scope = request_scope(_external()) if governed else request_scope(
        RequestPrincipal(audience_id="owner", surface="cli")
    )
    with scope:
        ordinary = commands.op_get(vault, path=rel)
        with pytest.raises(ValueError, match="^SECRET_BLOCKED:") as blocked:
            commands.op_get(vault, path=rel, include_raw=True)
        before_drift = target.read_text(encoding="utf-8")
        target.write_text(
            before_drift + "\nout-of-band drift\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="^STALE_EDIT:"):
            commands.op_edit(
                vault,
                path=rel,
                new_body=ordinary["body"] + "\nmust not commit\n",
                expected_hash=ordinary["content_hash"],
                why="prove raw refusal leaves stale edit semantics unchanged",
            )

    assert secret not in str(blocked.value)
    assert ordinary["content_hash"] == content_hash(before_drift)
    assert "must not commit" not in target.read_text(encoding="utf-8")


def test_l5_projection_omits_every_exact_provenance_channel(vault: Path) -> None:
    rel = _page(vault)
    target = vault / rel
    target.write_text(
        "---\n"
        "type: research-note\n"
        "sources: ['[[Knowledge Base/Sources/private]]']\n"
        "superseded_by: ['[[Knowledge Base/Notes/private]]']\n"
        "parent_media: Knowledge Base/Evidence/private.mp4\n"
        "---\n\n"
        "# Public-shaped excerpt\n\nBounded body only.\n",
        encoding="utf-8",
    )
    _govern(vault, ceiling=egress.LEVEL_EXCERPT)

    with request_scope(_external()):
        result = commands.op_get(
            vault,
            path=rel,
            include_raw=True,
            include_history=True,
            links=True,
        )

    assert set(result) == {"path", "body", "body_truncated", "release_level"}
    assert "private" not in str(result)


@pytest.mark.parametrize("ceiling", range(1, 6))
def test_below_l6_projection_redacts_forward_reverse_and_wire_provenance(
    vault: Path, monkeypatch: pytest.MonkeyPatch, ceiling: int
) -> None:
    rel = _page(vault)
    private_rel = "Knowledge Base/Private/private-source.md"
    private = vault / private_rel
    private.parent.mkdir(parents=True, exist_ok=True)
    private.write_text("# Private source\n", encoding="utf-8")
    (vault / rel).write_text(
        "---\n"
        "type: research-note\n"
        f'sources: ["[[{private_rel.removesuffix(".md")}]]"]\n'
        f'ingested_into: ["[[{private_rel.removesuffix(".md")}]]"]\n'
        f'supersedes: ["[[{private_rel.removesuffix(".md")}]]"]\n'
        f'superseded_by: ["[[{private_rel.removesuffix(".md")}]]"]\n'
        f"parent_media: {private_rel}\n"
        "---\n\n"
        f"Public sentence citing [[{private_rel.removesuffix('.md')}]].\n",
        encoding="utf-8",
    )
    scopes = vault / "Knowledge Base" / "_Governance" / "scopes"
    scopes.mkdir(parents=True, exist_ok=True)
    (scopes / "private.yaml").write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FC2\n"
        'paths: ["Private/**"]\n'
        "default_deny: true\n",
        encoding="utf-8",
    )
    _govern(
        vault,
        ceiling=ceiling,
        options=(
            "options:\n"
            "  notice: approved notice\n"
            "  constraint: approved constraint\n"
            "  abstract: approved abstract\n"
            "  bridge: approved bridge abstraction\n"
        ),
    )
    monkeypatch.setattr(
        commands.vault,
        "read_log_entries",
        lambda *_args, **_kwargs: [{"path": private_rel, "reason": "private"}],
    )
    monkeypatch.setattr(
        commands,
        "_link_summary",
        lambda *_args, **_kwargs: {
            "inbound": [{"path": private_rel}],
            "outbound": [private_rel],
        },
    )

    with request_scope(_external()):
        result = commands.op_get(
            vault,
            path=rel,
            include_raw=True,
            include_history=True,
            links=True,
        )

    if ceiling == egress.LEVEL_EXCERPT:
        assert set(result) == {"path", "body", "body_truncated", "release_level"}
    else:
        assert not {
            "path",
            "content",
            "content_hash",
            "frontmatter",
            "history",
            "links",
        }.intersection(result)
    assert private_rel.casefold() not in str(result).casefold()
    assert "private-source" not in str(result).casefold()


def test_l4_direct_read_without_exact_bridge_approval_lowers_to_l3(
    vault: Path,
) -> None:
    rel = _page(vault)
    opaque_bridge_id = "01ARZ3NDEKTSV4RRFFQ69G5FB1"
    _govern(
        vault,
        ceiling=egress.LEVEL_EXCERPT_REDACTED,
        options=(
            "options:\n"
            "  notice: must not appear\n"
            "  constraint: must not appear\n"
            "  abstract: safe fallback abstraction\n"
            f"  bridge: {opaque_bridge_id}\n"
        ),
    )

    with request_scope(_external()):
        result = commands.op_get(
            vault,
            path=rel,
            include_raw=True,
            include_history=True,
            links=True,
        )

    assert result["withheld"] is True
    assert result["level"] == egress.LEVEL_ABSTRACT
    assert result["abstract"] == "safe fallback abstraction"
    assert opaque_bridge_id not in str(result)
    assert "body text here" not in str(result)
    assert not {"path", "body", "content", "content_hash"}.intersection(result)
