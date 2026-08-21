"""Fail-closed propagation for unclassified non-Markdown artifacts."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from exomem import reserved_paths
from exomem.governance import egress, lifecycle, policy
from exomem.governance.principal import RequestPrincipal, owner_principal
from exomem.governance.tool import GovernanceError, op_govern_memory

SCOPE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
RULE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FB0"
ASSET = "Knowledge Base/Notes/asset.bin"


@pytest.fixture(autouse=True)
def _governance_dispatcher_authority():
    with reserved_paths._owner_authority_scope("govern_memory"):
        yield


def _write_semantic_scope(vault: Path, *, default_deny: bool = False) -> None:
    root = vault / "Knowledge Base" / "_Governance"
    scopes = root / "scopes"
    scopes.mkdir(parents=True, exist_ok=True)
    (scopes / "semantic.yaml").write_text(
        "governance_version: 1\n"
        f"id: {SCOPE_ID}\n"
        "types: [\"source\"]\n"
        + ("default_deny: true\n" if default_deny else ""),
        encoding="utf-8",
    )


def _write_asset(vault: Path) -> bytes:
    data = b"\x00opaque binary"
    target = vault / ASSET
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return data


def _semantic_documents() -> dict[str, str]:
    return {
        "scopes/semantic.yaml": (
            "governance_version: 1\n"
            f"id: {SCOPE_ID}\n"
            "types: [\"source\"]\n"
        ),
        "rules/semantic.yaml": (
            "governance_version: 1\n"
            f"id: {RULE_ID}\n"
            f"scope_ids: [\"{SCOPE_ID}\"]\n"
            "audience: external\nceiling: 1\n"
        ),
    }


def test_unresolved_non_markdown_direct_egress_has_no_decision(vault: Path) -> None:
    _write_semantic_scope(vault, default_deny=True)
    _write_asset(vault)
    loaded = policy.load(vault)

    assert egress._decide_path(
        vault,
        ASSET,
        policy=loaded,
        audience="external",
        purpose=None,
        grants_hash="",
    ) is None
    assert egress.release_level_for(
        vault, ASSET, principal=RequestPrincipal(audience_id="external", surface="mcp")
    ) is None
    assert egress.release_level_for_path_only(
        vault, ASSET, principal=RequestPrincipal(audience_id="external", surface="mcp")
    ) == egress.LEVEL_NONE


def test_proposal_refuses_unresolved_non_markdown_membership(vault: Path) -> None:
    _write_asset(vault)

    with pytest.raises(GovernanceError) as raised:
        op_govern_memory(
            vault,
            operation="propose",
            principal=owner_principal(),
            intent="Restrict sources",
            documents=_semantic_documents(),
            selector_paths=[],
            target_ceiling=1,
            duration="standing",
        )

    assert raised.value.code == "MEMBERSHIP_UNRESOLVED"
    assert ASSET not in str(raised.value)


@pytest.mark.parametrize(
    "operational_rel",
    [
        "Knowledge Base/.embeddings.sqlite",
        "Knowledge Base/.clip.sqlite-wal",
        "Knowledge Base/.graph.sqlite-shm",
        "Knowledge Base/.claims.sqlite",
        "Knowledge Base/.refs.sqlite-wal",
        "Knowledge Base/.lexical.sqlite",
        "Knowledge Base/.deferred-index.sqlite",
        "Knowledge Base/.media-jobs.sqlite",
        "Knowledge Base/.media-worker.lock",
        "Knowledge Base/.voice_profiles.json",
        "Knowledge Base/.review-state.json",
        "Knowledge Base/.graph-sync.json",
        "Knowledge Base/.graph-sync-floor.json",
        "Knowledge Base/.graph-commit-receipts/0123456789abcdef01234567.json",
        "Knowledge Base/.graph-rebuild-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbbbbbbbbbb.sqlite",
        "Knowledge Base/..review-state.json.abc123_4.tmp",
        "Knowledge Base/.lexical.sqlite.rebuild-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.tmp",
        "Knowledge Base/.lexical.sqlite.rebuild-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.tmp-wal",
        "Knowledge Base/.lexical.sqlite.rebuild-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.tmp-shm",
        "Knowledge Base/.lexical.sqlite.rebuild-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.tmp-journal",
    ],
)
def test_proposal_skips_operational_state_but_refuses_a_genuine_binary(
    vault: Path, operational_rel: str
) -> None:
    state = vault / operational_rel
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_bytes(b"runtime state")

    proposal = op_govern_memory(
        vault,
        operation="propose",
        principal=owner_principal(),
        intent="Restrict sources",
        documents=_semantic_documents(),
        selector_paths=[],
        target_ceiling=1,
        duration="standing",
    )

    assert proposal["proposal_id"]
    _write_asset(vault)
    with pytest.raises(GovernanceError) as raised:
        op_govern_memory(
            vault,
            operation="propose",
            principal=owner_principal(),
            intent="Restrict sources",
            documents=_semantic_documents(),
            selector_paths=[],
            target_ceiling=1,
            duration="standing",
        )
    assert raised.value.code == "MEMBERSHIP_UNRESOLVED"


@pytest.mark.parametrize(
    "artifact_rel",
    [
        "Knowledge Base/Notes/_attachments/nested.bin",
        "Knowledge Base/Notes/_archive/nested.bin",
        "Knowledge Base/Notes/_trash/nested.bin",
        (
            "Knowledge Base/Notes/"
            ".graph-rebuild-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-"
            "bbbbbbbbbbbbbbbbbbbbbbbb.sqlite"
        ),
        "Knowledge Base/.review-state.json.abc123_4.tmp",
        "Knowledge Base/Notes/..review-state.json.abc123_4.tmp",
        "Knowledge Base/.lexical.sqlite.rebuild-not-a-digest.tmp",
        "Knowledge Base/Notes/.lexical.sqlite.rebuild-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.tmp",
    ],
)
def test_proposal_refuses_governance_bearing_and_transactional_lookalikes(
    vault: Path, artifact_rel: str
) -> None:
    artifact = vault / artifact_rel
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(b"user artifact")

    with pytest.raises(GovernanceError) as raised:
        op_govern_memory(
            vault,
            operation="propose",
            principal=owner_principal(),
            intent="Restrict sources",
            documents=_semantic_documents(),
            selector_paths=[],
            target_ceiling=1,
            duration="standing",
        )

    assert raised.value.code == "MEMBERSHIP_UNRESOLVED"


def test_proposal_refuses_a_graph_rebuild_lookalike(vault: Path) -> None:
    lookalike = vault / "Knowledge Base/.graph-rebuild-user-copy.sqlite"
    lookalike.write_bytes(b"user artifact")

    with pytest.raises(GovernanceError) as raised:
        op_govern_memory(
            vault,
            operation="propose",
            principal=owner_principal(),
            intent="Restrict sources",
            documents=_semantic_documents(),
            selector_paths=[],
            target_ceiling=1,
            duration="standing",
        )

    assert raised.value.code == "MEMBERSHIP_UNRESOLVED"


def test_proposal_refuses_unresolved_scene_frames(vault: Path) -> None:
    frame = vault / "Knowledge Base/Evidence/call.mp4.frames/scene-001.jpg"
    frame.parent.mkdir(parents=True, exist_ok=True)
    frame.write_bytes(b"frame pixels")

    with pytest.raises(GovernanceError) as raised:
        op_govern_memory(
            vault,
            operation="propose",
            principal=owner_principal(),
            intent="Restrict sources",
            documents=_semantic_documents(),
            selector_paths=[],
            target_ceiling=1,
            duration="standing",
        )

    assert raised.value.code == "MEMBERSHIP_UNRESOLVED"


def test_purpose_direction_is_conservative_for_unresolved_scene_frames(tmp_path: Path) -> None:
    from exomem.governance import tool

    root = tmp_path / "vault"
    frame = root / "Knowledge Base/Evidence/call.mp4.frames/scene-001.jpg"
    frame.parent.mkdir(parents=True, exist_ok=True)
    frame.write_bytes(b"frame pixels")
    governance = root / "Knowledge Base/_Governance"
    (governance / "scopes").mkdir(parents=True)
    (governance / "scopes/semantic.yaml").write_text(
        "governance_version: 1\n"
        f"id: {SCOPE_ID}\n"
        "types: [\"source\"]\n",
        encoding="utf-8",
    )
    (governance / "rules").mkdir()
    (governance / "rules/semantic.yaml").write_text(
        "governance_version: 1\n"
        f"id: {RULE_ID}\n"
        f"scope_ids: [\"{SCOPE_ID}\"]\n"
        "audience: external\npurpose: approved\nceiling: 1\n",
        encoding="utf-8",
    )

    assert tool._purpose_direction(
        root, audience="external", before_purpose=None, after_purpose="approved"
    ) == "widening"


@pytest.mark.parametrize(
    "artifact_rel",
    [
        "Knowledge Base/Notes/_attachments/nested.bin",
        "Knowledge Base/Notes/_archive/nested.bin",
        (
            "Knowledge Base/Notes/"
            ".graph-rebuild-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-"
            "bbbbbbbbbbbbbbbbbbbbbbbb.sqlite"
        ),
    ],
)
def test_purpose_direction_is_conservative_for_unresolved_internal_tree_artifacts(
    tmp_path: Path, artifact_rel: str
) -> None:
    from exomem.governance import tool

    root = tmp_path / "vault"
    artifact = root / artifact_rel
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(b"user artifact")
    governance = root / "Knowledge Base/_Governance"
    (governance / "scopes").mkdir(parents=True)
    (governance / "scopes/semantic.yaml").write_text(
        "governance_version: 1\n"
        f"id: {SCOPE_ID}\n"
        "types: [\"source\"]\n",
        encoding="utf-8",
    )
    (governance / "rules").mkdir()
    (governance / "rules/semantic.yaml").write_text(
        "governance_version: 1\n"
        f"id: {RULE_ID}\n"
        f"scope_ids: [\"{SCOPE_ID}\"]\n"
        "audience: external\npurpose: approved\nceiling: 1\n",
        encoding="utf-8",
    )

    assert tool._purpose_direction(
        root, audience="external", before_purpose=None, after_purpose="approved"
    ) == "widening"


def test_lifecycle_treats_unresolved_non_markdown_as_governed(vault: Path) -> None:
    _write_semantic_scope(vault, default_deny=True)
    data = _write_asset(vault)
    item = lifecycle.ManifestItem(
        source_path=ASSET,
        trash_path="Knowledge Base/_trash/asset.bin",
        content_hash=hashlib.sha256(data).hexdigest(),
        size=len(data),
        kind="file",
        affected_ref="",
    )

    assert lifecycle._is_governed(vault, (item,)) is True
