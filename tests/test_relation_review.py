from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from exomem import activation_manifest, memory_refs, relation_review, semantic_contract, vault

_ID_A = "00000000-0000-4000-8000-000000000001"
_ID_B = "00000000-0000-4000-8000-000000000002"
_PAGE_A = "Knowledge Base/Notes/Insights/existing.md"
_PAGE_B = "Knowledge Base/Notes/Insights/candidate.md"
_COMPACT_UNIT = (
    "## Observations\n\n"
    "- [operating constraint] Keep retries bounded #reliability\n\n"
)
_DEFAULT_BODY = f"Body.\n\n{_COMPACT_UNIT}## Relations\n"


def _source(
    page_id: str,
    *,
    title: str = "Candidate",
    page_type: str = "insight",
    body: str = _DEFAULT_BODY,
    newline: str = "\n",
) -> str:
    source = (
        "---\n"
        f"title: {title}\n"
        f"type: {page_type}\n"
        "status: active\n"
        f"exomem_id: {page_id}\n"
        "---\n\n"
        f"{body}"
    )
    return source.replace("\n", newline)


def _write(root: Path, rel: str, source: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8", newline="")
    return path


def _sha_json(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
    return hashlib.sha256(payload).hexdigest()


def _auxiliary_hash(writes: tuple[vault.PlannedWrite, ...], root: Path) -> str:
    return _sha_json(
        {
            "schema_version": 1,
            "writes": [
                {
                    "path": write.path.relative_to(root).as_posix(),
                    "content_hash": hashlib.sha256(write.content.encode("utf-8")).hexdigest(),
                }
                for write in writes
            ],
        }
    )


def _artifact_payload(
    validation: relation_review.CreationDraftValidation,
    *,
    kind: str = "reviewed_none",
    reason: str | None = "No honest typed relation yet",
    auxiliary_hash: str | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": kind,
        "page_identity": validation.draft_id,
        "page_path_at_review": validation.destination,
        "content_fingerprint": validation.content_fingerprint,
        "draft_hash": validation.draft_hash,
        "auxiliary_hash": auxiliary_hash or _auxiliary_hash((), Path("/")),
        "reason": reason,
    }


def _seed_existing(root: Path) -> None:
    _write(root, _PAGE_A, _source(_ID_A, title="Existing"))


def _reviewed_validation(root: Path) -> tuple[str, relation_review.CreationDraftValidation]:
    _seed_existing(root)
    source = _source(_ID_B)
    validation = relation_review.validate_creation_draft(
        root,
        path=_PAGE_B,
        source=source,
        draft_id=_ID_B,
        operation="create",
    )
    assert validation.reviewed_none_required
    return source, validation


def test_lifecycle_trash_proof_type_mismatch_is_governed() -> None:
    source = _source(_ID_A)
    prepared = relation_review.build_lifecycle_prepared_transition(
        transition_id="00000000-0000-4000-8000-000000000099",
        operation="edit",
        page_identity=_ID_A,
        before_path=_PAGE_A,
        before_source_hash=vault.content_hash(source),
        after_path=_PAGE_A,
        after_source_hash=vault.content_hash(source),
        after_fingerprint=semantic_contract.review_content_fingerprint(
            _ID_A, source
        ),
        decision=None,
        transition_token="trash-proof-type-check",
        auxiliary_hash=hashlib.sha256(b"auxiliary").hexdigest(),
    )

    with pytest.raises(relation_review.RelationReviewError) as exc:
        relation_review.trash_proof_commits_prepared(
            prepared, object()  # type: ignore[arg-type]
        )

    assert exc.value.code == "LIFECYCLE_TRANSITION_MISMATCH"


def test_lifecycle_trash_proof_rejects_oversized_sidecar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(_ID_A)
    prepared = relation_review.build_lifecycle_prepared_transition(
        transition_id="00000000-0000-4000-8000-000000000098",
        operation="edit",
        page_identity=_ID_A,
        before_path=_PAGE_A,
        before_source_hash=vault.content_hash(source),
        after_path=_PAGE_A,
        after_source_hash=vault.content_hash(source),
        after_fingerprint=semantic_contract.review_content_fingerprint(
            _ID_A, source
        ),
        decision=None,
        transition_token="oversized-trash-sidecar",
        auxiliary_hash=hashlib.sha256(b"auxiliary").hexdigest(),
    )
    proof = relation_review.LifecycleTrashProof(
        page_identity=_ID_A,
        original_path=_PAGE_A,
        trash_path="Knowledge Base/_trash/oversized.md",
        source_hash=vault.content_hash(source),
        review_fingerprint=semantic_contract.review_content_fingerprint(
            _ID_A, source
        ),
        source_guard=None,  # type: ignore[arg-type]
        sidecar_source='{"padding":"xxxxxxxx"}',
        sidecar_guard=None,  # type: ignore[arg-type]
        live_owner_paths=(),
    )
    monkeypatch.setattr(
        relation_review, "_LIFECYCLE_TRASH_MAX_SIDECAR_BYTES", 8
    )

    with pytest.raises(relation_review.RelationReviewError) as exc:
        relation_review.trash_proof_commits_prepared(prepared, proof)

    assert exc.value.code == "LIFECYCLE_TRASH_LIMIT"


def _crash_commit_worker(
    root_text: str,
    source: str,
    draft_hash: str,
    auxiliaries: tuple[tuple[str, str], ...],
    crash_after: int,
) -> None:
    root = Path(root_text)
    planned = tuple(
        vault.PlannedWrite(root / relative, content) for relative, content in auxiliaries
    )
    targets = {
        relation_review.review_artifact_path(root, _ID_B),
        *(write.path for write in planned),
        root / _PAGE_B,
    }
    real_replace = os.replace
    replacements = 0

    def crash_on_boundary(src, dst):
        nonlocal replacements
        result = real_replace(src, dst)
        if Path(dst) in targets and str(src).endswith(".tmp"):
            replacements += 1
            if replacements == crash_after:
                os._exit(91)
        return result

    vault.os.replace = crash_on_boundary
    relation_review.commit_creation_draft(
        root,
        path=_PAGE_B,
        source=source,
        draft_id=_ID_B,
        operation="create",
        relation_disposition="reviewed_none",
        relation_review_hash=draft_hash,
        relation_review_reason="No honest typed relation yet",
        auxiliary_writes=planned,
    )


def test_validate_only_is_deterministic_normalized_and_nonmutating(
    tmp_path: Path,
) -> None:
    source = _source(_ID_A, newline="\r\n")
    before = tuple(tmp_path.rglob("*"))

    validation = relation_review.validate_creation_draft(
        tmp_path,
        path="Knowledge Base/Notes/Insights/first.md",
        source=source,
        draft_id=_ID_A,
        operation="create",
    )

    normalized = source.replace("\r\n", "\n")
    source_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    fingerprint = _sha_json(
        {
            "schema_version": 1,
            "page_identity": _ID_A,
            "normalized_source_hash": source_hash,
        }
    )
    expected_draft = _sha_json(
        {
            "schema_version": 1,
            "draft_id": _ID_A,
            "destination": "Knowledge Base/Notes/Insights/first.md",
            "content_fingerprint": fingerprint,
        }
    )
    assert validation.draft_id == _ID_A
    assert validation.content_fingerprint == fingerprint
    assert validation.draft_hash == expected_draft
    assert validation.mutated is False
    assert validation.relation_disposition == "bootstrap"
    assert validation.committable_without_review
    assert validation.as_dict()["contract_result"] == validation.contract_result.as_dict()
    assert tuple(tmp_path.rglob("*")) == before
    with pytest.raises(FrozenInstanceError):
        validation.destination = "changed.md"  # type: ignore[misc]


def test_new_draft_id_and_validation_reject_unsafe_or_mismatched_inputs(
    tmp_path: Path,
) -> None:
    assert memory_refs.normalize_id(relation_review.new_draft_id()) is not None
    cases = (
        ("/Knowledge Base/Notes/Insights/page.md", _source(_ID_A), _ID_A),
        ("../page.md", _source(_ID_A), _ID_A),
        ("Knowledge Base/Sources/page.md", _source(_ID_A, page_type="source"), _ID_A),
        (_PAGE_B, _source(_ID_A), _ID_B),
        (
            _PAGE_B,
            _source(_ID_A).replace(
                f"exomem_id: {_ID_A}",
                f"exomem_id: {_ID_A}\nexomem_id: {_ID_A}",
            ),
            _ID_A,
        ),
    )

    for path, source, draft_id in cases:
        with pytest.raises(relation_review.RelationReviewError):
            relation_review.validate_creation_draft(
                tmp_path,
                path=path,
                source=source,
                draft_id=draft_id,
                operation="create",
            )
    assert not list(tmp_path.rglob("*"))


def test_reviewed_none_commit_writes_artifact_then_auxiliaries_then_primary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, validation = _reviewed_validation(tmp_path)
    planned_directory = tmp_path / "Knowledge Base/Notes/Research/Planned Project"
    auxiliary = vault.PlannedWrite(
        tmp_path / "Knowledge Base/nav.md",
        "nav\n",
        create_only=True,
        expected_hash=vault.MISSING_CONTENT_HASH,
        ensure_directories=(planned_directory,),
    )
    real_batch = relation_review.vault.batch_atomic_write
    captured: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    captured_writes: list[tuple[vault.PlannedWrite, ...]] = []

    def capture(writes, **kwargs):
        detached = tuple(writes)
        captured_writes.append(detached)
        captured.append(
            (
                tuple(write.path.relative_to(tmp_path).as_posix() for write in detached),
                tuple(guard.target for guard in kwargs.get("required_guards", ())),
            )
        )
        return real_batch(detached, **kwargs)

    monkeypatch.setattr(relation_review.vault, "batch_atomic_write", capture)
    commit = relation_review.commit_creation_draft(
        tmp_path,
        path=_PAGE_B,
        source=source,
        draft_id=_ID_B,
        operation="create",
        relation_disposition="reviewed_none",
        relation_review_hash=validation.draft_hash,
        relation_review_reason="  No honest typed relation yet  ",
        auxiliary_writes=(auxiliary,),
    )

    artifact = relation_review.review_artifact_path(tmp_path, _ID_B)
    assert captured == [
        (
                (
                    artifact.relative_to(tmp_path).as_posix(),
                    "Knowledge Base/nav.md",
                    _PAGE_B,
                ),
                (
                    relation_review.lifecycle_prepared_path(
                        tmp_path, _ID_B
                    ).parent.relative_to(tmp_path).as_posix(),
                ),
            )
        ]
    assert commit.resumed_prepared is False
    assert commit.written_paths == captured[0][0]
    committed_auxiliary = captured_writes[0][1]
    assert committed_auxiliary.create_only is True
    assert committed_auxiliary.expected_hash == vault.MISSING_CONTENT_HASH
    assert committed_auxiliary.ensure_directories == (planned_directory,)
    assert planned_directory.is_dir()
    assert commit.review_state_current
    assert commit.review_reference == artifact.relative_to(tmp_path).as_posix()
    assert json.loads(artifact.read_text(encoding="utf-8"))["reason"] == (
        "No honest typed relation yet"
    )
    assert (tmp_path / _PAGE_B).read_text(encoding="utf-8") == source
    corpus = semantic_contract.build_corpus_context(tmp_path)
    page = corpus.pages[_PAGE_B]
    loaded = relation_review.load_relation_review(tmp_path, page, corpus=corpus)
    assert loaded is not None
    assert loaded.content_fingerprint == page.review_fingerprint


def test_first_page_bootstrap_persists_null_reason_artifact(tmp_path: Path) -> None:
    source = _source(_ID_A)
    commit = relation_review.commit_creation_draft(
        tmp_path,
        path="Knowledge Base/Notes/Insights/first.md",
        source=source,
        draft_id=_ID_A,
        operation="create",
    )

    artifact = relation_review.review_artifact_path(tmp_path, _ID_A)
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert payload["kind"] == "bootstrap"
    assert payload["reason"] is None
    assert commit.relation_disposition == "bootstrap"
    assert commit.review_state_current


def test_qualifying_relation_rejects_unnecessary_review_and_needs_no_artifact(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "Knowledge Base/Entities/target.md",
        _source(_ID_A, title="Target", page_type="entity"),
    )
    source = _source(
        _ID_B,
        body=f"Body.\n\n{_COMPACT_UNIT}## Relations\n- supports [[Target]]\n",
    )
    validation = relation_review.validate_creation_draft(
        tmp_path,
        path=_PAGE_B,
        source=source,
        draft_id=_ID_B,
        operation="create",
    )
    assert validation.relation_disposition == "qualifying_relation"
    assert validation.relation_candidates[0].qualifies

    with pytest.raises(relation_review.RelationReviewError) as exc:
        relation_review.commit_creation_draft(
            tmp_path,
            path=_PAGE_B,
            source=source,
            draft_id=_ID_B,
            operation="create",
            relation_disposition="reviewed_none",
            relation_review_hash=validation.draft_hash,
            relation_review_reason="unnecessary",
        )
    assert exc.value.code == "RELATION_REVIEW_NOT_APPLICABLE"

    commit = relation_review.commit_creation_draft(
        tmp_path,
        path=_PAGE_B,
        source=source,
        draft_id=_ID_B,
        operation="create",
    )
    assert commit.review_reference is None
    receipt = relation_review.load_creation_receipt(tmp_path, _ID_B)
    assert receipt is not None and receipt.kind == "qualifying"
    assert relation_review.load_relation_reviews(tmp_path) == ()


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b"{" + b"x" * (16 * 1024), "RELATION_REVIEW_TOO_LARGE"),
        (b"\xff", "RELATION_REVIEW_INVALID_ENCODING"),
        (b'{"schema_version":1,"schema_version":1}', "RELATION_REVIEW_DUPLICATE_KEY"),
        (b"not-json", "RELATION_REVIEW_INVALID_JSON"),
        (
            json.dumps(
                {
                    "schema_version": True,
                    "kind": "bootstrap",
                    "page_identity": _ID_A,
                    "page_path_at_review": _PAGE_A,
                    "content_fingerprint": "a" * 64,
                    "draft_hash": "b" * 64,
                    "auxiliary_hash": "c" * 64,
                    "reason": None,
                }
            ).encode(),
            "RELATION_REVIEW_INVALID_SCHEMA",
        ),
        (
            json.dumps(
                {
                    "schema_version": 2,
                    "kind": "bootstrap",
                    "page_identity": _ID_A,
                    "page_path_at_review": _PAGE_A,
                    "content_fingerprint": "a" * 64,
                    "draft_hash": "b" * 64,
                    "auxiliary_hash": "c" * 64,
                    "reason": None,
                }
            ).encode(),
            "RELATION_REVIEW_INVALID_SCHEMA",
        ),
        (
            json.dumps(
                {
                    "schema_version": 3,
                    "kind": "bootstrap",
                    "page_identity": _ID_A,
                    "page_path_at_review": _PAGE_A,
                    "content_fingerprint": "a" * 64,
                    "draft_hash": "b" * 64,
                    "auxiliary_hash": "c" * 64,
                    "reason": None,
                }
            ).encode(),
            "RELATION_REVIEW_UNSUPPORTED_VERSION",
        ),
    ],
)
def test_artifact_loader_error_codes_are_exact(tmp_path: Path, raw: bytes, code: str) -> None:
    path = relation_review.review_artifact_path(tmp_path, _ID_A)
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)

    with pytest.raises(relation_review.RelationReviewError) as exc:
        relation_review.load_relation_reviews(tmp_path)

    assert exc.value.code == code
    assert _ID_A not in exc.value.reason
    assert path.read_bytes() == raw


def test_alias_artifact_blocks_lookup_and_identity_reuse(tmp_path: Path) -> None:
    source, validation = _reviewed_validation(tmp_path)
    canonical = relation_review.review_artifact_path(tmp_path, _ID_B)
    alias = canonical.with_name(f"{_ID_B.upper()}.JSON")
    alias.parent.mkdir(parents=True)
    alias.write_text("{}", encoding="utf-8")

    with pytest.raises(relation_review.RelationReviewError) as load_exc:
        relation_review.load_relation_reviews(tmp_path)
    with pytest.raises(relation_review.RelationReviewError) as commit_exc:
        relation_review.commit_creation_draft(
            tmp_path,
            path=_PAGE_B,
            source=source,
            draft_id=_ID_B,
            operation="create",
            relation_disposition="reviewed_none",
            relation_review_hash=validation.draft_hash,
            relation_review_reason="No honest typed relation yet",
        )

    assert load_exc.value.code == "RELATION_REVIEW_ALIAS"
    assert commit_exc.value.code == "DRAFT_ID_IN_USE"
    assert not (tmp_path / _PAGE_B).exists()


def test_sync_conflict_duplicate_identity_fails_closed(tmp_path: Path) -> None:
    _write(tmp_path, _PAGE_A, _source(_ID_A))
    _write(
        tmp_path,
        "Knowledge Base/Private/copy.sync-conflict-1.MD",
        _source(_ID_A, title="Copy"),
    )

    with pytest.raises(relation_review.RelationReviewError) as exc:
        relation_review.validate_creation_draft(
            tmp_path,
            path=_PAGE_B,
            source=_source(_ID_A),
            draft_id=_ID_A,
            operation="create",
        )

    assert exc.value.code == "DRAFT_ID_IN_USE"


def test_exact_prepared_artifact_recovery_reuses_artifact_and_reapplies_auxiliaries(
    tmp_path: Path,
) -> None:
    source, validation = _reviewed_validation(tmp_path)
    auxiliaries = (
        vault.PlannedWrite(tmp_path / "Knowledge Base/nav.md", "new-nav\n"),
        vault.PlannedWrite(tmp_path / "Knowledge Base/log.md", "new-log\n"),
    )
    auxiliaries[0].path.write_text("old-nav\n", encoding="utf-8")
    artifact = relation_review.review_artifact_path(tmp_path, _ID_B)
    artifact.parent.mkdir(parents=True)
    artifact.write_text(
        json.dumps(
            _artifact_payload(
                validation,
                auxiliary_hash=_auxiliary_hash(auxiliaries, tmp_path),
            ),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    original_artifact = artifact.read_bytes()

    commit = relation_review.commit_creation_draft(
        tmp_path,
        path=_PAGE_B,
        source=source,
        draft_id=_ID_B,
        operation="create",
        relation_disposition="reviewed_none",
        relation_review_hash=validation.draft_hash,
        relation_review_reason="No honest typed relation yet",
        auxiliary_writes=auxiliaries,
    )

    assert commit.resumed_prepared is True
    assert artifact.read_bytes() == original_artifact
    assert commit.written_paths == ("Knowledge Base/nav.md", "Knowledge Base/log.md", _PAGE_B)
    assert [write.path.read_text(encoding="utf-8") for write in auxiliaries] == [
        "new-nav\n",
        "new-log\n",
    ]


def test_nonexact_prepared_auxiliary_sequence_remains_reserved(tmp_path: Path) -> None:
    source, validation = _reviewed_validation(tmp_path)
    original = (vault.PlannedWrite(tmp_path / "Knowledge Base/nav.md", "one\n"),)
    artifact = relation_review.review_artifact_path(tmp_path, _ID_B)
    artifact.parent.mkdir(parents=True)
    artifact.write_text(
        json.dumps(
            _artifact_payload(
                validation,
                auxiliary_hash=_auxiliary_hash(original, tmp_path),
            ),
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    before = artifact.read_bytes()

    with pytest.raises(relation_review.RelationReviewError) as exc:
        relation_review.commit_creation_draft(
            tmp_path,
            path=_PAGE_B,
            source=source,
            draft_id=_ID_B,
            operation="create",
            relation_disposition="reviewed_none",
            relation_review_hash=validation.draft_hash,
            relation_review_reason="No honest typed relation yet",
            auxiliary_writes=(vault.PlannedWrite(tmp_path / "Knowledge Base/nav.md", "changed\n"),),
        )

    assert exc.value.code == "DRAFT_ID_IN_USE"
    assert artifact.read_bytes() == before
    assert not (tmp_path / _PAGE_B).exists()


@pytest.mark.parametrize("crash_after", [1, 2, 3])
def test_process_crash_before_primary_resumes_exact_prepared_commit(
    tmp_path: Path, crash_after: int
) -> None:
    source, validation = _reviewed_validation(tmp_path)
    activation_manifest.ensure_manifest(tmp_path)
    auxiliary_values = (
        ("Knowledge Base/nav.md", "nav\n"),
        ("Knowledge Base/log.md", "log\n"),
    )
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_crash_commit_worker,
        args=(
            str(tmp_path),
            source,
            validation.draft_hash,
            auxiliary_values,
            crash_after,
        ),
    )
    process.start()
    process.join(20)

    assert process.exitcode == 91
    assert not (tmp_path / _PAGE_B).exists()
    artifact = relation_review.review_artifact_path(tmp_path, _ID_B)
    assert artifact.exists()
    artifact_bytes = artifact.read_bytes()
    commit = relation_review.commit_creation_draft(
        tmp_path,
        path=_PAGE_B,
        source=source,
        draft_id=_ID_B,
        operation="create",
        relation_disposition="reviewed_none",
        relation_review_hash=validation.draft_hash,
        relation_review_reason="No honest typed relation yet",
        auxiliary_writes=tuple(
            vault.PlannedWrite(tmp_path / rel, content) for rel, content in auxiliary_values
        ),
    )

    assert commit.resumed_prepared is True
    assert artifact.read_bytes() == artifact_bytes
    assert (tmp_path / _PAGE_B).exists()


def test_successful_replay_and_same_identity_copy_have_exact_precedence(
    tmp_path: Path,
) -> None:
    source, validation = _reviewed_validation(tmp_path)
    relation_review.commit_creation_draft(
        tmp_path,
        path=_PAGE_B,
        source=source,
        draft_id=_ID_B,
        operation="create",
        relation_disposition="reviewed_none",
        relation_review_hash=validation.draft_hash,
        relation_review_reason="No honest typed relation yet",
    )
    with pytest.raises(relation_review.RelationReviewError) as replay:
        relation_review.commit_creation_draft(
            tmp_path,
            path=_PAGE_B,
            source=source,
            draft_id=_ID_B,
            operation="create",
            relation_disposition="reviewed_none",
            relation_review_hash=validation.draft_hash,
            relation_review_reason="No honest typed relation yet",
        )
    assert replay.value.code == "DRAFT_ALREADY_COMMITTED"

    _write(
        tmp_path,
        "Knowledge Base/Private/direct-copy.md",
        source,
    )
    with pytest.raises(relation_review.RelationReviewError) as copied:
        relation_review.validate_creation_draft(
            tmp_path,
            path=_PAGE_B,
            source=source,
            draft_id=_ID_B,
            operation="create",
        )
    assert copied.value.code == "DRAFT_ID_IN_USE"


def test_exact_v2_bootstrap_retry_ignores_later_corpus_growth(tmp_path: Path) -> None:
    source = _source(_ID_A, title="First")
    relation_review.commit_creation_draft(
        tmp_path,
        path=_PAGE_A,
        source=source,
        draft_id=_ID_A,
        operation="create",
        draft_token="bootstrap-v2-token",
    )
    _write(tmp_path, _PAGE_B, _source(_ID_B, title="Later"))

    with pytest.raises(relation_review.RelationReviewError) as replay:
        relation_review.commit_creation_draft(
            tmp_path,
            path=_PAGE_A,
            source=source,
            draft_id=_ID_A,
            operation="create",
            draft_token="bootstrap-v2-token",
        )

    assert replay.value.code == "DRAFT_ALREADY_COMMITTED"


def test_exact_v2_reviewed_none_retry_ignores_later_inbound_relation(
    tmp_path: Path,
) -> None:
    source, validation = _reviewed_validation(tmp_path)
    relation_review.commit_creation_draft(
        tmp_path,
        path=_PAGE_B,
        source=source,
        draft_id=_ID_B,
        operation="create",
        relation_disposition="reviewed_none",
        relation_review_hash=validation.draft_hash,
        relation_review_reason="No honest typed relation yet",
    )
    inbound_id = "00000000-0000-4000-8000-000000000003"
    _write(
        tmp_path,
        "Knowledge Base/Notes/Insights/inbound.md",
        _source(
            inbound_id,
            title="Inbound",
            body=(
                f"Body.\n\n{_COMPACT_UNIT}"
                "## Relations\n- supports [[Candidate]]\n"
            ),
        ),
    )

    with pytest.raises(relation_review.RelationReviewError) as replay:
        relation_review.commit_creation_draft(
            tmp_path,
            path=_PAGE_B,
            source=source,
            draft_id=_ID_B,
            operation="create",
            relation_disposition="reviewed_none",
            relation_review_hash=validation.draft_hash,
            relation_review_reason="No honest typed relation yet",
        )

    assert replay.value.code == "DRAFT_ALREADY_COMMITTED"


def test_exact_v2_primary_rejects_otherwise_valid_wrong_kind_receipt(
    tmp_path: Path,
) -> None:
    source = _source(_ID_A, title="Kind-bound")
    token = "kind-bound-v2-token"
    relation_review.commit_creation_draft(
        tmp_path,
        path=_PAGE_A,
        source=source,
        draft_id=_ID_A,
        operation="create",
        draft_token=token,
    )
    artifact = relation_review.review_artifact_path(tmp_path, _ID_A)
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert payload["kind"] == "bootstrap"
    payload["kind"] = "qualifying"
    artifact.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(relation_review.RelationReviewError) as replay:
        relation_review.commit_creation_draft(
            tmp_path,
            path=_PAGE_A,
            source=source,
            draft_id=_ID_A,
            operation="create",
            draft_token=token,
        )

    assert replay.value.code == "DRAFT_ID_IN_USE"


def test_draft_token_hash_accepts_semantic_token_and_enforces_shared_bound() -> None:
    from exomem import semantic_writes

    encoded = semantic_writes.DraftToken(
        "note",
        "create",
        "Knowledge Base/Notes/Insights/large-token.md",
        "2026-07-14",
        (
            semantic_writes.DraftRegistration(
                "large-token-project", "domain", "x" * 9000
            ),
        ),
    ).encode()
    assert semantic_writes.DraftToken.decode(encoded).encode() == encoded
    assert relation_review.draft_token_hash(encoded) == hashlib.sha256(
        encoded.encode("utf-8")
    ).hexdigest()

    maximum = "x" * relation_review.MAX_DRAFT_TOKEN_ENCODED_BYTES
    assert relation_review.draft_token_hash(maximum) == hashlib.sha256(
        maximum.encode("utf-8")
    ).hexdigest()
    with pytest.raises(relation_review.RelationReviewError) as over:
        relation_review.draft_token_hash(maximum + "x")
    assert over.value.code == "INVALID_DRAFT_TOKEN"
    with pytest.raises(semantic_writes.SemanticWriteError) as decode_over:
        semantic_writes.DraftToken.decode(maximum + "x")
    assert decode_over.value.code == "INVALID_DRAFT_TOKEN"


def test_load_reviews_returns_sorted_immutable_records_and_ignores_temp_residue(
    tmp_path: Path,
) -> None:
    source = _source(_ID_A)
    relation_review.commit_creation_draft(
        tmp_path,
        path="Knowledge Base/Notes/Insights/first.md",
        source=source,
        draft_id=_ID_A,
        operation="create",
    )
    directory = relation_review.review_artifact_path(tmp_path, _ID_A).parent
    (directory / ".leftover.tmp").write_text("partial", encoding="utf-8")
    (directory / ".leftover.bak").write_text("backup", encoding="utf-8")

    records = relation_review.load_relation_reviews(tmp_path)

    assert type(records) is tuple
    assert [record.page_identity for record in records] == [_ID_A]
    assert records[0].reference.endswith(f"/{_ID_A}.json")
    with pytest.raises(FrozenInstanceError):
        records[0].reason = "changed"  # type: ignore[misc]


def test_attempt_loaders_and_corpus_walk_are_each_called_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    counts = {"relations": 0, "language": 0, "contracts": 0, "corpus": 0}
    seams = (
        (relation_review.relation_registry, "load_registry", "relations"),
        (relation_review.semantic_language_registry, "load_registry", "language"),
        (relation_review.memory_schema, "load_saved_contracts", "contracts"),
        (relation_review.semantic_contract, "build_corpus_context", "corpus"),
    )
    for owner, name, key in seams:
        original = getattr(owner, name)

        def counted(*args, _original=original, _key=key, **kwargs):
            counts[_key] += 1
            return _original(*args, **kwargs)

        monkeypatch.setattr(owner, name, counted)

    relation_review.validate_creation_draft(
        tmp_path,
        path="Knowledge Base/Notes/Insights/first.md",
        source=_source(_ID_A),
        draft_id=_ID_A,
        operation="create",
    )

    assert counts == {"relations": 1, "language": 1, "contracts": 1, "corpus": 1}


def test_hash_mismatch_is_rejected_before_activation_mutates_vault(
    tmp_path: Path,
) -> None:
    source, _ = _reviewed_validation(tmp_path)

    with pytest.raises(relation_review.RelationReviewError) as exc:
        relation_review.commit_creation_draft(
            tmp_path,
            path=_PAGE_B,
            source=source,
            draft_id=_ID_B,
            operation="create",
            relation_disposition="reviewed_none",
            relation_review_hash="0" * 64,
            relation_review_reason="No honest typed relation yet",
        )

    assert exc.value.code == "DRAFT_HASH_MISMATCH"
    assert not activation_manifest.manifest_path(tmp_path).exists()
    assert not relation_review.review_artifact_path(tmp_path, _ID_B).exists()
    assert not (tmp_path / _PAGE_B).exists()


@pytest.mark.parametrize("existing_lifecycle_root", [False, True])
def test_creation_commit_rechecks_lifecycle_uuid_reservation_at_atomic_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing_lifecycle_root: bool,
) -> None:
    source, validation = _reviewed_validation(tmp_path)
    lifecycle_root = relation_review.lifecycle_prepared_path(
        tmp_path, _ID_B
    ).parent.parent
    if existing_lifecycle_root:
        lifecycle_root.mkdir(parents=True)
    real_batch = relation_review.vault.batch_atomic_write

    def inject_lifecycle_reservation(*args, **kwargs):
        decision = relation_review.build_lifecycle_decision(
            page_identity=_ID_B,
            after_fingerprint=validation.content_fingerprint,
            reason="Concurrent lifecycle reservation",
        )
        decision_path = relation_review.lifecycle_decision_path(
            tmp_path, _ID_B, validation.content_fingerprint
        )
        decision_path.parent.mkdir(parents=True, exist_ok=True)
        decision_path.write_text(
            relation_review.serialize_lifecycle_decision(decision), encoding="utf-8"
        )
        return real_batch(*args, **kwargs)

    monkeypatch.setattr(
        relation_review.vault, "batch_atomic_write", inject_lifecycle_reservation
    )

    with pytest.raises(relation_review.RelationReviewError) as reserved:
        relation_review.commit_creation_draft(
            tmp_path,
            path=_PAGE_B,
            source=source,
            draft_id=_ID_B,
            operation="create",
            relation_disposition="reviewed_none",
            relation_review_hash=validation.draft_hash,
            relation_review_reason="No honest typed relation yet",
        )

    assert reserved.value.code == "DRAFT_ID_IN_USE"
    assert not relation_review.review_artifact_path(tmp_path, _ID_B).exists()
    assert not (tmp_path / _PAGE_B).exists()


def test_review_load_survives_move_and_returns_stale_evidence_after_edit(
    tmp_path: Path,
) -> None:
    original = "Knowledge Base/Notes/Insights/first.md"
    moved = "Knowledge Base/Notes/Insights/moved.md"
    source = _source(_ID_A)
    relation_review.commit_creation_draft(
        tmp_path,
        path=original,
        source=source,
        draft_id=_ID_A,
        operation="create",
    )
    (tmp_path / original).rename(tmp_path / moved)

    moved_corpus = semantic_contract.build_corpus_context(tmp_path)
    moved_page = moved_corpus.pages[moved]
    current = relation_review.load_relation_review(tmp_path, moved_page, corpus=moved_corpus)
    assert current is not None
    assert current.content_fingerprint == moved_page.review_fingerprint

    (tmp_path / moved).write_text(source + "Edited.\n", encoding="utf-8")
    edited_corpus = semantic_contract.build_corpus_context(tmp_path)
    edited_page = edited_corpus.pages[moved]
    stale = relation_review.load_relation_review(tmp_path, edited_page, corpus=edited_corpus)
    assert stale is not None
    assert stale.content_fingerprint != edited_page.review_fingerprint


def test_same_draft_thread_race_has_one_winner_and_one_exact_replay(
    tmp_path: Path,
) -> None:
    source, validation = _reviewed_validation(tmp_path)

    def commit() -> str:
        try:
            relation_review.commit_creation_draft(
                tmp_path,
                path=_PAGE_B,
                source=source,
                draft_id=_ID_B,
                operation="create",
                relation_disposition="reviewed_none",
                relation_review_hash=validation.draft_hash,
                relation_review_reason="No honest typed relation yet",
            )
        except relation_review.RelationReviewError as error:
            return error.code
        return "committed"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = sorted(executor.map(lambda _: commit(), range(2)))

    assert outcomes == ["DRAFT_ALREADY_COMMITTED", "committed"]


def test_review_directory_parent_symlink_fails_closed(tmp_path: Path) -> None:
    kb = tmp_path / "Knowledge Base"
    outside = tmp_path / "outside"
    kb.mkdir()
    (outside / "relation-reviews").mkdir(parents=True)
    try:
        (kb / "_Schema").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable")

    with pytest.raises(relation_review.RelationReviewError) as exc:
        relation_review.load_relation_reviews(tmp_path)

    assert exc.value.code == "RELATION_REVIEW_DIRECTORY_UNSAFE"


@pytest.mark.skipif(os.name != "nt", reason="Windows directory descriptor regression")
def test_existing_review_directory_opens_on_windows(tmp_path: Path) -> None:
    directory = tmp_path / "Knowledge Base/_Schema/relation-reviews"
    directory.mkdir(parents=True)

    assert relation_review.load_relation_reviews(tmp_path) == ()


def test_auxiliary_traversal_and_final_symlink_fail_before_activation(
    tmp_path: Path,
) -> None:
    source, validation = _reviewed_validation(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    symlink = tmp_path / "Knowledge Base/symlink.md"
    symlink.symlink_to(outside)
    cases = (
        vault.PlannedWrite(tmp_path / "Knowledge Base/nested/../nav.md", "nav\n"),
        vault.PlannedWrite(symlink, "changed\n"),
    )

    for auxiliary in cases:
        with pytest.raises(relation_review.RelationReviewError) as exc:
            relation_review.commit_creation_draft(
                tmp_path,
                path=_PAGE_B,
                source=source,
                draft_id=_ID_B,
                operation="create",
                relation_disposition="reviewed_none",
                relation_review_hash=validation.draft_hash,
                relation_review_reason="No honest typed relation yet",
                auxiliary_writes=(auxiliary,),
            )

        assert exc.value.code == "INVALID_AUXILIARY_WRITE"
        assert not activation_manifest.manifest_path(tmp_path).exists()
        assert not (tmp_path / _PAGE_B).exists()
    assert outside.read_text(encoding="utf-8") == "outside\n"


def test_exact_replay_rejects_symlinked_auxiliary_without_following_it(
    tmp_path: Path,
) -> None:
    source, validation = _reviewed_validation(tmp_path)
    auxiliary = vault.PlannedWrite(tmp_path / "Knowledge Base/nav.md", "nav\n")
    relation_review.commit_creation_draft(
        tmp_path,
        path=_PAGE_B,
        source=source,
        draft_id=_ID_B,
        operation="create",
        relation_disposition="reviewed_none",
        relation_review_hash=validation.draft_hash,
        relation_review_reason="No honest typed relation yet",
        auxiliary_writes=(auxiliary,),
    )
    outside = tmp_path / "outside-nav.md"
    outside.write_text("nav\n", encoding="utf-8")
    auxiliary.path.unlink()
    auxiliary.path.symlink_to(outside)

    with pytest.raises(relation_review.RelationReviewError) as exc:
        relation_review.commit_creation_draft(
            tmp_path,
            path=_PAGE_B,
            source=source,
            draft_id=_ID_B,
            operation="create",
            relation_disposition="reviewed_none",
            relation_review_hash=validation.draft_hash,
            relation_review_reason="No honest typed relation yet",
            auxiliary_writes=(auxiliary,),
        )

    assert exc.value.code == "INVALID_AUXILIARY_WRITE"
    assert outside.read_text(encoding="utf-8") == "nav\n"


def test_artifact_directory_swap_is_detected_even_when_file_inode_is_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    relation_review.commit_creation_draft(
        tmp_path,
        path="Knowledge Base/Notes/Insights/first.md",
        source=_source(_ID_A),
        draft_id=_ID_A,
        operation="create",
    )
    artifact = relation_review.review_artifact_path(tmp_path, _ID_A)
    directory = artifact.parent
    displaced = directory.with_name("relation-reviews-displaced")
    real_open = os.open
    swapped = False

    def swap_before_artifact_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if not swapped and Path(path).name == artifact.name:
            swapped = True
            directory.rename(displaced)
            directory.mkdir()
            os.link(displaced / artifact.name, directory / artifact.name)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(relation_review.os, "open", swap_before_artifact_open)

    with pytest.raises(relation_review.RelationReviewError) as exc:
        relation_review.load_relation_reviews(tmp_path)

    assert exc.value.code == "RELATION_REVIEW_SWAPPED"


def test_prepared_bootstrap_invalidated_by_corpus_growth_reports_contract_failure(
    tmp_path: Path,
) -> None:
    source = _source(_ID_B)
    validation = relation_review.validate_creation_draft(
        tmp_path,
        path=_PAGE_B,
        source=source,
        draft_id=_ID_B,
        operation="create",
    )
    assert validation.relation_disposition == "bootstrap"
    artifact = relation_review.review_artifact_path(tmp_path, _ID_B)
    artifact.parent.mkdir(parents=True)
    artifact.write_text(
        json.dumps(
            _artifact_payload(validation, kind="bootstrap", reason=None),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    original_artifact = artifact.read_bytes()
    _seed_existing(tmp_path)

    with pytest.raises(relation_review.RelationReviewError) as exc:
        relation_review.commit_creation_draft(
            tmp_path,
            path=_PAGE_B,
            source=source,
            draft_id=_ID_B,
            operation="create",
        )

    assert exc.value.code == "SEMANTIC_CONTRACT_BLOCKED"
    # Only code and reason survive out to the MCP caller. A bare prefix forces a
    # second validate_only round-trip purely to learn what blocked the write.
    assert exc.value.reason != "semantic contract has blocking findings", (
        "the blocked-commit error must name its blocking findings"
    )
    assert artifact.read_bytes() == original_artifact
    assert not (tmp_path / _PAGE_B).exists()
    assert not activation_manifest.manifest_path(tmp_path).exists()
