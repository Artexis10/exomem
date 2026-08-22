"""Owner-reviewed, receipt-first governance companion backfill."""

from __future__ import annotations

import hashlib
import inspect
import math
from pathlib import Path

import numpy as np
import pytest

from exomem import commands, reserved_paths
from exomem import vault as vault_module
from exomem.clip_index import CLIP_DIM, ClipIndex
from exomem.governance import companions, membership, policy, receipts, store
from exomem.governance.principal import RequestPrincipal, owner_principal
from exomem.governance.tool import GovernanceCrash, GovernanceError, op_govern_memory
from exomem.preserve import _render_sidecar


@pytest.fixture(autouse=True)
def _governance_dispatcher_authority():
    with reserved_paths._owner_authority_scope("govern_memory"):
        yield


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write(vault: Path, rel: str, value: bytes) -> Path:
    target = vault / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(value)
    return target


def _input(
    vault: Path,
    *,
    artifact_class: str,
    artifact_path: str,
    companion_path: str,
    semantics: dict[str, list[str]] | None = None,
    **extra: object,
) -> dict[str, object]:
    artifact = (vault / artifact_path).read_bytes()
    companion = (vault / companion_path).read_bytes()
    return {
        "version": 1,
        "artifact_class": artifact_class,
        "artifact_path": artifact_path,
        "expected_artifact_sha256": _sha(artifact),
        "expected_artifact_size": len(artifact),
        "expected_companion_path": companion_path,
        "expected_companion_sha256": _sha(companion),
        "semantics": semantics
        if semantics is not None
        else {
            "projects": ["client-a"],
            "tags": ["confidential"],
            "types": ["source"],
            "classes": ["pii"],
        },
        **extra,
    }


def _legacy_binary(vault: Path) -> tuple[str, str, bytes, bytes]:
    artifact_path = "Knowledge Base/Evidence/Private/Files/secret.bin"
    companion_path = f"{artifact_path}.md"
    artifact = b"\x00opaque-secret\xff"
    companion = (
        b"---\n"
        b"type: source\n"
        b"project: page-only-project\n"
        b"tags: [page-only-tag]\n"
        b"classes: [page-only-class]\n"
        b"---\n"
        b"\n# Existing page\n\nBody must remain byte exact.\n"
    )
    _write(vault, artifact_path, artifact)
    _write(vault, companion_path, companion)
    return artifact_path, companion_path, artifact, companion


def _preview(vault: Path, payload: dict[str, object], **extra: object) -> dict:
    return op_govern_memory(
        vault,
        operation="backfill_companion",
        backfill_action="preview",
        companion_input=payload,
        principal=owner_principal(),
        **extra,
    )


def _commit(
    vault: Path,
    proposal_id: str,
    payload: dict[str, object],
    **extra: object,
) -> dict:
    return op_govern_memory(
        vault,
        operation="backfill_companion",
        backfill_action="commit",
        proposal_id=proposal_id,
        companion_input=payload,
        principal=owner_principal(),
        **extra,
    )


def _frontmatter(path: Path) -> dict[str, object]:
    parsed, _body, marker = vault_module.parse_frontmatter(
        path.read_text(encoding="utf-8"), strict=True
    )
    assert marker is not None
    return parsed


def test_backfill_is_a_generated_owner_only_surface_operation(vault: Path) -> None:
    assert "backfill_companion" in commands._GovernanceOperation.__args__
    signature = inspect.signature(commands.op_govern_memory)
    assert "backfill_action" in signature.parameters
    assert "companion_input" in signature.parameters

    artifact_path, companion_path, _artifact, companion = _legacy_binary(vault)
    payload = _input(
        vault,
        artifact_class="binary",
        artifact_path=artifact_path,
        companion_path=companion_path,
    )
    outsider = RequestPrincipal("principal:outsider", surface="rest")
    with pytest.raises(GovernanceError) as caught:
        op_govern_memory(
            vault,
            operation="backfill_companion",
            backfill_action="preview",
            companion_input=payload,
            principal=outsider,
        )
    assert caught.value.code == "GOVERNANCE_OWNER_REQUIRED"
    assert (vault / companion_path).read_bytes() == companion


def test_preview_and_commit_preserve_every_non_descriptor_byte(vault: Path) -> None:
    artifact_path, companion_path, artifact, companion = _legacy_binary(vault)
    semantic_scope = (
        vault
        / "Knowledge Base"
        / "_Governance"
        / "scopes"
        / "confidential.yaml"
    )
    semantic_scope.parent.mkdir(parents=True, exist_ok=True)
    semantic_scope.write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FAV\n"
        'tags: ["confidential"]\n',
        encoding="utf-8",
    )
    loaded_policy = policy.load(vault)
    before = membership.evaluate_path_only(vault, artifact_path, loaded_policy)
    assert before.state == "unresolved"
    assert before.reason == "descriptor_missing"
    semantics = {
        "projects": ["client-a"],
        "tags": ["confidential"],
        "types": ["source"],
        "classes": ["pii"],
    }
    payload = _input(
        vault,
        artifact_class="binary",
        artifact_path=artifact_path,
        companion_path=companion_path,
        semantics=semantics,
    )

    preview = _preview(vault, payload, now=100.0)
    assert preview["status"] == "preview"
    assert preview["descriptor"] == {
        "version": 1,
        "state": "classified",
        "artifact_class": "binary",
        "artifact_path": artifact_path,
        "artifact_sha256": _sha(artifact),
        "artifact_size": len(artifact),
        "semantics": semantics,
    }
    assert {item["role"] for item in preview["identities"]} == {
        "artifact",
        "companion",
    }
    assert (vault / companion_path).read_bytes() == companion
    assert receipts.event_records(vault) == []

    committed = _commit(vault, preview["proposal_id"], payload, now=101.0)
    assert committed["status"] == "committed"
    assert committed["proposal_id"] == preview["proposal_id"]
    assert committed["direction"] == "widening"
    assert (vault / artifact_path).read_bytes() == artifact
    current = (vault / companion_path).read_bytes()
    assert current != companion
    descriptor = _frontmatter(vault / companion_path)["governance_companion"]
    assert descriptor == preview["descriptor"]
    assert current.replace(
        b"governance_companion:\n"
        b"  version: 1\n"
        b"  state: classified\n"
        b"  artifact_class: binary\n"
        + f"  artifact_path: {artifact_path}\n".encode()
        + f"  artifact_sha256: {_sha(artifact)}\n".encode()
        + f"  artifact_size: {len(artifact)}\n".encode()
        + b"  semantics:\n"
        b"    projects:\n"
        b"    - client-a\n"
        b"    tags:\n"
        b"    - confidential\n"
        b"    types:\n"
        b"    - source\n"
        b"    classes:\n"
        b"    - pii\n",
        b"",
    ) == companion

    classified = companions.classify(vault, artifact_path)
    assert classified.projects == ("client-a",)
    assert classified.tags == ("confidential",)
    after = membership.evaluate_path_only(vault, artifact_path, loaded_policy)
    assert after.state == "classified"
    assert after.scope_ids == frozenset({"01ARZ3NDEKTSV4RRFFQ69G5FAV"})

    replayed = _commit(vault, preview["proposal_id"], payload, now=102.0)
    assert replayed == committed
    event_rows = [
        row for row in receipts.event_records(vault) if row["event_id"].startswith(committed["event_id"])
    ]
    assert [row["phase"] for row in event_rows] == ["intent", "committed"]
    conn = store.open_connection(vault)
    try:
        journal = conn.execute(
            "SELECT principal_id, proposal_id, prior_digest, final_digest, phase "
            "FROM governance_operation_journals WHERE event_id=?",
            (committed["event_id"],),
        ).fetchone()
    finally:
        conn.close()
    assert tuple(journal[:2]) == ("owner", preview["proposal_id"])
    assert journal[2] != journal[3]
    assert journal[4] == "closed"


def test_page_metadata_is_never_inferred_and_incomplete_input_writes_nothing(
    vault: Path,
) -> None:
    artifact_path, companion_path, _artifact, companion = _legacy_binary(vault)
    payload = _input(
        vault,
        artifact_class="binary",
        artifact_path=artifact_path,
        companion_path=companion_path,
    )
    payload["semantics"] = {
        "projects": ["client-a"],
        "tags": ["confidential"],
        "types": ["source"],
    }
    with pytest.raises(GovernanceError) as caught:
        _preview(vault, payload)
    assert caught.value.code == "INVALID_COMPANION_BACKFILL"
    assert (vault / companion_path).read_bytes() == companion
    assert receipts.event_records(vault) == []


@pytest.mark.parametrize("drift", ["artifact", "companion"])
def test_commit_refuses_identity_or_byte_drift_without_partial_descriptor(
    vault: Path, drift: str
) -> None:
    artifact_path, companion_path, _artifact, companion = _legacy_binary(vault)
    payload = _input(
        vault,
        artifact_class="binary",
        artifact_path=artifact_path,
        companion_path=companion_path,
    )
    preview = _preview(vault, payload, now=200.0)
    target = vault / (artifact_path if drift == "artifact" else companion_path)
    target.write_bytes(target.read_bytes() + b"drift")

    with pytest.raises(GovernanceError) as caught:
        _commit(vault, preview["proposal_id"], payload, now=201.0)
    assert caught.value.code == "STALE_COMPANION_BACKFILL"
    current = (vault / companion_path).read_bytes()
    assert b"governance_companion:" not in current
    if drift == "artifact":
        assert current == companion
    assert receipts.event_records(vault) == []


@pytest.mark.parametrize(
    ("crash_at", "expected_descriptor", "expected_phases"),
    [
        ("after_reservation", False, []),
        ("after_intent", False, ["intent", "aborted"]),
        ("after_arming", False, ["intent", "aborted"]),
        ("after_publish", True, ["intent", "committed"]),
        ("after_terminal", True, ["intent", "committed"]),
    ],
)
def test_backfill_recovery_is_predecessor_or_complete_target(
    vault: Path,
    crash_at: str,
    expected_descriptor: bool,
    expected_phases: list[str],
) -> None:
    from exomem.governance.recovery import reconcile_governance_operations

    artifact_path, companion_path, _artifact, companion = _legacy_binary(vault)
    payload = _input(
        vault,
        artifact_class="binary",
        artifact_path=artifact_path,
        companion_path=companion_path,
    )
    preview = _preview(vault, payload, now=300.0)
    with pytest.raises(GovernanceCrash):
        _commit(
            vault,
            preview["proposal_id"],
            payload,
            now=301.0,
            crash_at=crash_at,
        )
    result = reconcile_governance_operations(vault)
    assert result["blocked"] is False
    current = (vault / companion_path).read_bytes()
    assert (b"governance_companion:" in current) is expected_descriptor
    if not expected_descriptor:
        assert current == companion
    phases = [row["phase"] for row in receipts.event_records(vault)]
    assert phases == expected_phases


def test_media_and_dataset_class_fields_are_bound(vault: Path) -> None:
    media_path = "Knowledge Base/Evidence/Private/Media/interview.mp3"
    media_bytes = b"ID3legacy"
    media_companion = f"{media_path}.md"
    _write(vault, media_path, media_bytes)
    _write(
        vault,
        media_companion,
        _render_sidecar(
            artifact_name="interview.mp3",
            scope="Private",
            category="Media",
            date_iso="2026-08-21",
            media_type="audio",
            evidence_file=media_path,
            extracted_by="pending",
        ).encode(),
    )
    media_input = _input(
        vault,
        artifact_class="media",
        artifact_path=media_path,
        companion_path=media_companion,
        media_type="audio",
        original_filename="interview.mp3",
    )
    media_preview = _preview(vault, media_input)
    _commit(vault, media_preview["proposal_id"], media_input)
    assert companions.classify(vault, media_path).classes == ("pii",)

    dataset_path = "Knowledge Base/Data/private.json"
    dataset_bytes = b'[{"secret":true}]\n'
    card_path = "Knowledge Base/Notes/Datasets/private.md"
    _write(vault, dataset_path, dataset_bytes)
    _write(
        vault,
        card_path,
        (
            "---\ntype: dataset\ntitle: Private data\n"
            f"data_file: {dataset_path}\nformat: json\n"
            "tags: [page-only]\n---\n\nExisting dataset notes.\n"
        ).encode(),
    )
    dataset_input = _input(
        vault,
        artifact_class="dataset",
        artifact_path=dataset_path,
        companion_path=card_path,
        format="json",
    )
    dataset_preview = _preview(vault, dataset_input)
    _commit(vault, dataset_preview["proposal_id"], dataset_input)
    assert companions.classify(vault, dataset_path).tags == ("confidential",)


@pytest.mark.parametrize(
    ("extracted_by", "text"),
    [
        ("pending", None),
        ("upload", "Uploader supplied text."),
        ("whisper", "Transcript."),
    ],
)
def test_minimal_pending_and_completed_media_sidecars_backfill_without_rewrite(
    vault: Path, extracted_by: str, text: str | None
) -> None:
    artifact_path = f"Knowledge Base/Evidence/Private/Media/{extracted_by}.mp3"
    companion_path = f"{artifact_path}.md"
    artifact = f"ID3-{extracted_by}".encode()
    _write(vault, artifact_path, artifact)
    companion = _render_sidecar(
        artifact_name=Path(artifact_path).name,
        scope="Private",
        category="Media",
        date_iso="2026-08-21",
        text=text,
        media_type="audio",
        evidence_file=artifact_path,
        extracted_by=extracted_by,
    ).encode()
    _write(vault, companion_path, companion)
    payload = _input(
        vault,
        artifact_class="media",
        artifact_path=artifact_path,
        companion_path=companion_path,
        media_type="audio",
        original_filename=Path(artifact_path).name,
    )
    preview = _preview(vault, payload)
    _commit(vault, preview["proposal_id"], payload)

    assert (vault / artifact_path).read_bytes() == artifact
    current = (vault / companion_path).read_bytes()
    assert current.endswith(companion.split(b"---\n", 2)[-1])
    page = _frontmatter(vault / companion_path)
    assert page["extracted_by"] == extracted_by
    assert page["tags"] == ["evidence", "private", "media"]
    assert page["governance_companion"]["semantics"]["projects"] == ["client-a"]


def test_crlf_companion_stays_crlf_and_body_exact(vault: Path) -> None:
    artifact_path, companion_path, _artifact, companion = _legacy_binary(vault)
    companion = companion.replace(b"\n", b"\r\n")
    (vault / companion_path).write_bytes(companion)
    payload = _input(
        vault,
        artifact_class="binary",
        artifact_path=artifact_path,
        companion_path=companion_path,
    )
    preview = _preview(vault, payload)
    _commit(vault, preview["proposal_id"], payload)
    current = (vault / companion_path).read_bytes()
    assert b"\n" not in current.replace(b"\r\n", b"")
    assert current.endswith(b"\r\n# Existing page\r\n\r\nBody must remain byte exact.\r\n")


def _legacy_scene(
    vault: Path,
    *,
    frame_ts: float,
    filename_ms: int,
    indexed_ts: list[float],
) -> tuple[str, str, dict[str, object]]:
    parent_path = "Knowledge Base/Evidence/Private/Media/video.mp4"
    frame_path = (
        f"{parent_path}.frames/scene-000-t{filename_ms}ms.jpg"
    )
    companion_path = f"{frame_path}.md"
    _write(vault, parent_path, b"video-parent")
    _write(vault, frame_path, b"jpeg-frame")
    _write(
        vault,
        companion_path,
        _render_sidecar(
            artifact_name=Path(frame_path).name,
            scope="Private",
            category="Media",
            date_iso="2026-08-21",
            media_type="image",
            evidence_file=frame_path,
            extracted_by="pending",
            parent_media=parent_path,
            frame_ts=frame_ts,
        ).encode(),
    )
    vector = np.zeros(CLIP_DIM, dtype=np.float32)
    ClipIndex(vault).upsert_frames(
        parent_path, [(value, vector) for value in indexed_ts], mtime=1.0
    )
    payload = _input(
        vault,
        artifact_class="scene_frame",
        artifact_path=frame_path,
        companion_path=companion_path,
        parent_path=parent_path,
        expected_parent_sha256=_sha((vault / parent_path).read_bytes()),
        frame_timestamp_ms=filename_ms,
    )
    return frame_path, companion_path, payload


@pytest.mark.parametrize(
    ("seconds", "milliseconds"),
    [(1.25, 1250), (1.0005, 1000), (1.0015, 1002)],
)
def test_legacy_scene_timestamp_uses_binary64_ties_to_even(
    vault: Path, seconds: float, milliseconds: int
) -> None:
    frame_path, _companion_path, payload = _legacy_scene(
        vault,
        frame_ts=seconds,
        filename_ms=milliseconds,
        indexed_ts=[seconds],
    )
    preview = _preview(vault, payload)
    assert preview["descriptor"]["frame_timestamp_ms"] == milliseconds
    _commit(vault, preview["proposal_id"], payload)
    assert companions.classify(vault, frame_path).types == ("source",)


@pytest.mark.parametrize(
    ("seconds", "filename_ms", "indexed", "code"),
    [
        (-0.1, 0, [-0.1], "INVALID_COMPANION_BACKFILL"),
        (math.inf, 0, [math.inf], "INVALID_COMPANION_BACKFILL"),
        (math.nan, 0, [math.nan], "INVALID_COMPANION_BACKFILL"),
        (4_294_967.296, 4_294_967_296, [4_294_967.296], "INVALID_COMPANION_BACKFILL"),
        (1.25, 1251, [1.25], "STALE_COMPANION_BACKFILL"),
        (1.25, 1250, [1.251], "STALE_COMPANION_BACKFILL"),
        (1.25, 1250, [1.2504], "STALE_COMPANION_BACKFILL"),
        (1.25, 1250, [1.25, 1.2504], "STALE_COMPANION_BACKFILL"),
    ],
)
def test_legacy_scene_timestamp_refuses_invalid_or_ambiguous_sources(
    vault: Path,
    seconds: float,
    filename_ms: int,
    indexed: list[float],
    code: str,
) -> None:
    _frame_path, companion_path, payload = _legacy_scene(
        vault, frame_ts=seconds, filename_ms=filename_ms, indexed_ts=indexed
    )
    prior = (vault / companion_path).read_bytes()
    with pytest.raises(GovernanceError) as caught:
        _preview(vault, payload)
    assert caught.value.code == code
    assert (vault / companion_path).read_bytes() == prior
    assert receipts.event_records(vault) == []


def test_legacy_scene_parent_drift_refuses_without_descriptor(vault: Path) -> None:
    _frame_path, companion_path, payload = _legacy_scene(
        vault, frame_ts=1.25, filename_ms=1250, indexed_ts=[1.25]
    )
    parent = vault / str(payload["parent_path"])
    parent.write_bytes(parent.read_bytes() + b"drift")
    with pytest.raises(GovernanceError) as caught:
        _preview(vault, payload)
    assert caught.value.code == "STALE_COMPANION_BACKFILL"
    assert b"governance_companion:" not in (vault / companion_path).read_bytes()


def test_generic_publish_content_cas_rejects_same_identity_byte_drift(vault: Path) -> None:
    rel = "Knowledge Base/Notes/ordinary.md"
    path = _write(vault, rel, b"before")
    snapshot = reserved_paths.read_generic_bytes(vault, rel)
    path.write_bytes(b"same-inode-drift")
    assert path.stat().st_ino == snapshot.identity.inode

    with pytest.raises(reserved_paths.ReservedPathLeafError) as caught:
        reserved_paths.publish_generic_bytes(
            vault,
            rel,
            b"target",
            expected_identity=snapshot.identity,
            expected_sha256=_sha(snapshot.data),
        )
    assert caught.value.code == "IDENTITY_CHANGED"
    assert path.read_bytes() == b"same-inode-drift"


def test_dataset_duplicate_card_refuses_without_metadata(vault: Path) -> None:
    dataset_path = "Knowledge Base/Data/private.json"
    card_path = "Knowledge Base/Notes/Datasets/private.md"
    _write(vault, dataset_path, b"[]\n")
    card = (
        "---\ntype: dataset\n"
        f"data_file: {dataset_path}\nformat: json\n"
        "---\n\nOne.\n"
    ).encode()
    _write(vault, card_path, card)
    _write(vault, "Knowledge Base/Notes/Datasets/duplicate.md", card)
    payload = _input(
        vault,
        artifact_class="dataset",
        artifact_path=dataset_path,
        companion_path=card_path,
        format="json",
    )
    with pytest.raises(GovernanceError) as caught:
        _preview(vault, payload)
    assert caught.value.code == "STALE_COMPANION_BACKFILL"
    assert b"governance_companion:" not in (vault / card_path).read_bytes()
