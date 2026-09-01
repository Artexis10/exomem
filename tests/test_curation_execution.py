from __future__ import annotations

import json
from pathlib import Path

import pytest

from exomem import commands
from exomem.vault import content_hash


def _create_step(step_id: str, slug: str, *, sentence: str | None = None) -> dict[str, object]:
    return {
        "step_id": step_id,
        "kind": "create-note",
        "args": {
            "title": f"Curation {slug}",
            "slug": slug,
            "content": (
                "## Observations\n\n"
                f"- [finding] {sentence or f'Commit {slug} exactly once.'} ^{slug}\n"
            ),
            "relation_disposition": "reviewed_none",
            "relation_review_reason": "No honest relation exists for this isolated fixture.",
        },
    }


def _propose(vault: Path, *steps: dict[str, object]) -> dict[str, object]:
    from exomem import curation

    return curation.propose(
        vault,
        {"version": 1, "title": "Forward curation", "steps": list(steps)},
    )


def _apply(vault: Path, proposed: dict[str, object], *, why: str = "Approved exact plan."):
    from exomem import curation

    return curation.apply(
        vault,
        run_id=proposed["run_id"],
        plan_id=proposed["plan_id"],
        expected_plan_fingerprint=proposed["plan_fingerprint"],
        why=why,
    )


def _seed_note(vault: Path, slug: str, sentence: str = "Keep old text.") -> str:
    arguments = {
        "title": f"Curation {slug}",
        "slug": slug,
        "content": f"## Observations\n\n- [finding] {sentence} ^{slug}\n",
        "note_type": "insight",
    }
    validation = commands.op_remember(vault, validate_only=True, **arguments)
    result = commands.op_remember(
        vault,
        **arguments,
        draft_id=validation["draft_id"],
        draft_hash=validation["draft_hash"],
        draft_token=validation["draft_token"],
        relation_disposition="reviewed_none",
        relation_review_hash=validation["draft_hash"],
        relation_review_reason="No honest relation exists for this fixture.",
    )
    return result["path"]


def _seed_plain_file(vault: Path, slug: str, content: str) -> str:
    path = f"Knowledge Base/Reference/{slug}.md"
    validation = commands.op_manage_memory_file(
        vault,
        operation="create",
        path=path,
        content=content,
        validate_only=True,
    )
    result = commands.op_manage_memory_file(
        vault,
        operation="create",
        path=path,
        content=content,
        draft_id=validation["draft_id"],
        draft_hash=validation["draft_hash"],
        draft_token=validation["draft_token"],
    )
    return result["path"]


def _witness(vault: Path, proposed: dict[str, object], step_id: str) -> dict[str, object]:
    from exomem import curation

    operation = curation.operation_id(proposed["plan_id"], 0, step_id)
    path = curation.CurationStore(vault).evidence_dir(proposed["run_id"]) / f"{operation}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_exact_approval_executes_at_most_one_real_content_step_per_request(vault: Path) -> None:
    from exomem import curation

    proposed = _propose(
        vault,
        _create_step("first", "curation-first"),
        _create_step("second", "curation-second"),
    )

    first = _apply(vault, proposed)

    assert first["phase"] == "executing"
    assert first["step"]["step_id"] == "first"
    assert (vault / first["step"]["path"]).is_file()
    assert not (vault / "Knowledge Base/Notes/Insights/curation-second.md").exists()

    second = curation.resume(vault, run_id=proposed["run_id"], plan_id=proposed["plan_id"])
    assert second["phase"] == "completed"
    assert second["step"]["step_id"] == "second"
    assert (vault / second["step"]["path"]).is_file()


def test_approval_requires_exact_identity_fingerprint_and_bounded_single_line_why(
    vault: Path,
) -> None:
    from exomem import curation

    proposed = _propose(vault, _create_step("one", "curation-approval-guards"))
    destination = "Knowledge Base/Notes/Insights/curation-approval-guards.md"
    for changes, code in (
        ({"plan_id": "0" * 64}, "CURATION_PLAN_IDENTITY_MISMATCH"),
        ({"expected_plan_fingerprint": "0" * 64}, "CURATION_PLAN_IDENTITY_MISMATCH"),
        ({"why": "line one\nline two"}, "INVALID_CURATION_PLAN"),
        ({"why": "x" * 501}, "CURATION_PLAN_TOO_LARGE"),
    ):
        kwargs = {
            "run_id": proposed["run_id"],
            "plan_id": proposed["plan_id"],
            "expected_plan_fingerprint": proposed["plan_fingerprint"],
            "why": "Approved.",
            **changes,
        }
        with pytest.raises(curation.CurationError, match=code):
            curation.apply(vault, **kwargs)
        assert not (vault / destination).exists()


def test_content_effect_and_content_free_witness_share_the_leaf_batch(vault: Path) -> None:
    from exomem import curation

    sentence = "Todiste sitoutuu vaikutukseen mutta ei sisällä sisältöä."
    proposed = _propose(
        vault,
        _create_step("atomic", "curation-atomic-witness", sentence=sentence),
    )
    result = _apply(vault, proposed)
    store = curation.CurationStore(vault)
    operation = curation.operation_id(proposed["plan_id"], 0, "atomic")
    witness_path = store.evidence_dir(proposed["run_id"]) / f"{operation}.json"
    witness = json.loads(witness_path.read_text(encoding="utf-8"))

    assert (vault / result["step"]["path"]).is_file()
    assert witness["operation_id"] == operation
    assert witness["leaf_identity"] == "remember"
    assert witness["before"] == [
        {"path": result["step"]["path"], "absent": True}
    ]
    assert witness["after"][0]["path"] == result["step"]["path"]
    assert witness["result_digest"]
    assert sentence not in witness_path.read_text(encoding="utf-8")


def test_refused_leaf_never_leaves_a_witness(vault: Path) -> None:
    from exomem import curation

    proposed = _propose(vault, _create_step("refused", "curation-refused-witness"))
    destination = "Knowledge Base/Notes/Insights/curation-refused-witness.md"
    (vault / destination).parent.mkdir(parents=True, exist_ok=True)
    (vault / destination).write_text("collision", encoding="utf-8")

    with pytest.raises(curation.CurationError, match="CURATION_BINDING_STALE"):
        _apply(vault, proposed)
    assert not curation.CurationStore(vault).evidence_dir(proposed["run_id"]).exists()


def test_work_item_refuses_an_ancestor_symlink_without_reading_external_bytes(
    vault: Path, tmp_path: Path
) -> None:
    from exomem import curation

    external = tmp_path / "external"
    external.mkdir()
    secret = external / "secret.md"
    secret.write_text("outside-vault-secret", encoding="utf-8")
    linked = vault / "Knowledge Base/Notes/linked"
    linked.parent.mkdir(parents=True, exist_ok=True)
    linked.symlink_to(external, target_is_directory=True)

    with pytest.raises(curation.CurationError, match="CURATION_PATH_UNSAFE"):
        curation.work_item(vault, paths=["Knowledge Base/Notes/linked/secret.md"])

    assert secret.read_text(encoding="utf-8") == "outside-vault-secret"


def test_after_prepared_crash_retries_same_operation_once(vault: Path, monkeypatch) -> None:  # noqa: ANN001
    from exomem import curation

    proposed = _propose(vault, _create_step("prepared", "curation-prepared-crash"))

    def fault(name: str) -> None:
        if name == "after-prepared-state":
            raise curation.CurationFault(name)

    monkeypatch.setattr(curation, "_fault_barrier", fault)
    with pytest.raises(curation.CurationFault, match="after-prepared-state"):
        _apply(vault, proposed)
    status = curation.status(vault, run_id=proposed["run_id"])
    assert status["phase"] == "executing"
    assert status["recovery"] == "retry-uncommitted"
    assert not (vault / "Knowledge Base/Notes/Insights/curation-prepared-crash.md").exists()

    monkeypatch.setattr(curation, "_fault_barrier", lambda _name: None)
    resumed = curation.resume(vault, run_id=proposed["run_id"], plan_id=proposed["plan_id"])
    assert resumed["phase"] == "completed"
    assert resumed["step"]["operation_id"] == curation.operation_id(
        proposed["plan_id"], 0, "prepared"
    )


def test_after_leaf_crash_status_does_not_repair_and_resume_never_reexecutes_leaf(
    vault: Path, monkeypatch
) -> None:  # noqa: ANN001
    from exomem import curation

    proposed = _propose(vault, _create_step("leaf", "curation-leaf-crash"))
    calls = 0
    real = commands.op_remember

    def counted(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        nonlocal calls
        if not kwargs.get("validate_only"):
            calls += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(commands, "op_remember", counted)

    def fault(name: str) -> None:
        if name == "after-leaf-witness":
            raise curation.CurationFault(name)

    monkeypatch.setattr(curation, "_fault_barrier", fault)
    with pytest.raises(curation.CurationFault, match="after-leaf-witness"):
        _apply(vault, proposed)
    store = curation.CurationStore(vault)
    receipt_root = store.receipts_dir(proposed["run_id"])
    before = list(receipt_root.glob("*/*.json")) if receipt_root.exists() else []

    inspected = curation.status(vault, run_id=proposed["run_id"])
    after = list(receipt_root.glob("*/*.json")) if receipt_root.exists() else []
    assert inspected["recovery"] == "receipt-required"
    assert before == after == []
    assert calls == 1

    monkeypatch.setattr(curation, "_fault_barrier", lambda _name: None)
    recovered = curation.resume(vault, run_id=proposed["run_id"], plan_id=proposed["plan_id"])
    assert recovered["phase"] == "completed"
    assert recovered["step"]["outcome"] == "recovered-committed"
    assert calls == 1


def test_delete_proposal_uses_canonical_inbound_guard(vault: Path) -> None:
    from exomem import curation

    path = _seed_note(vault, "curation-delete-inbound")
    _seed_plain_file(
        vault,
        "curation-delete-inbound-source",
        f"See [[{path.removesuffix('.md')}]].\n",
    )

    with pytest.raises(curation.CurationError, match="INBOUND_LINKS"):
        _propose(
            vault,
            {
                "step_id": "delete",
                "kind": "delete",
                "args": {"path": path, "confirm": True},
            },
        )


def test_leaf_application_preserves_stable_error_code(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import curation

    proposed = _propose(vault, _create_step("create", "curation-stable-leaf-error"))

    class StableLeafRefusal(Exception):
        code = "RELATION_REVIEW_MISMATCH"
        reason = "the canonical leaf rejected a stale review"

    def refuse(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise StableLeafRefusal

    monkeypatch.setattr(
        commands,
        "op_remember",
        refuse,
    )

    with pytest.raises(curation.CurationError) as caught:
        _apply(vault, proposed)

    assert caught.value.code == "RELATION_REVIEW_MISMATCH"


def test_move_canonical_preflight_covers_every_rewritten_inbound_page(vault: Path) -> None:
    from exomem import move_file as move_module

    old_path = _seed_note(vault, "curation-move-manifest")
    new_path = "Knowledge Base/Notes/Insights/curation-move-manifest-new.md"
    inbound_path = _seed_plain_file(
        vault,
        "curation-move-inbound",
        f"See [[{old_path.removesuffix('.md')}]].\n",
    )
    inbound = vault / inbound_path
    validation = move_module.move_file(
        vault,
        old_path=old_path,
        new_path=new_path,
        update_wikilinks=True,
        validate_only=True,
    )
    assert isinstance(validation, move_module.MoveFileValidation)
    after_paths = {item["path"] for item in validation.after}

    assert after_paths == {old_path, new_path, inbound.relative_to(vault).as_posix()}
    assert old_path.removesuffix(".md") in inbound.read_text(encoding="utf-8")


def test_relocation_steps_refuse_before_transition_publication_or_rename(
    vault: Path, monkeypatch
) -> None:  # noqa: ANN001
    from exomem import curation, reserved_paths

    move_source = _seed_note(vault, "curation-unsupported-move")
    delete_source = _seed_note(vault, "curation-unsupported-delete")
    recover_source = _seed_note(vault, "curation-unsupported-recover")
    deleted = commands.op_manage_memory_file(
        vault,
        operation="delete",
        path=recover_source,
        confirm=True,
    )
    trash_path = deleted["trash_path"]
    rename_calls: list[tuple[object, ...]] = []

    def forbidden_rename(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        rename_calls.append((*args, kwargs))
        raise AssertionError("curation attempted a rename without lineage capability")

    monkeypatch.setattr(reserved_paths, "move_generic_path", forbidden_rename)
    plans = (
        {
            "step_id": "move",
            "kind": "move",
            "args": {
                "old_path": move_source,
                "new_path": move_source.replace(".md", "-new.md"),
            },
        },
        {
            "step_id": "delete",
            "kind": "delete",
            "args": {"path": delete_source, "confirm": True},
        },
        {
            "step_id": "recover",
            "kind": "recover",
            "args": {"trash_path": trash_path, "restore_path": recover_source},
        },
    )

    for step in plans:
        with pytest.raises(
            curation.CurationError, match="CURATION_RENAME_HISTORY_UNPROVABLE"
        ):
            _propose(vault, step)

    direct_plan = {
        "version": 1,
        "title": "Direct sealed relocation",
        "steps": [plans[0]],
    }
    direct = curation.CurationStore(vault).create_forward(
        direct_plan,
        binding_manifest=[curation._prepare_step(vault, plans[0], 0)],
        registry_ids=curation.registry_identities(vault),
    )
    with pytest.raises(
        curation.CurationError, match="CURATION_RENAME_HISTORY_UNPROVABLE"
    ):
        curation.apply(
            vault,
            run_id=direct["run_id"],
            plan_id=direct["plan_id"],
            expected_plan_fingerprint=direct["plan_fingerprint"],
            why="This must refuse before relocation authorization.",
        )

    assert rename_calls == []
    assert not curation.CurationStore(vault).approval_path(direct["run_id"]).exists()
    runs = vault / "Knowledge Base/_Governance/curation/runs"
    assert not runs.exists() or list(runs.rglob("transitions")) == []


def test_matching_relocation_hashes_cannot_authorize_placement_recovery(vault: Path) -> None:
    from exomem import curation

    path = _seed_note(vault, "curation-hash-is-not-lineage")
    digest = content_hash((vault / path).read_text(encoding="utf-8"))
    preparation = {
        "effect_before": [{"path": path, "absent": True}],
        "effect_after": [{"path": path, "content_hash": digest}],
        "rename_after": [{"path": path, "content_hash": digest}],
    }

    with pytest.raises(
        curation.CurationError, match="CURATION_RENAME_HISTORY_UNPROVABLE"
    ):
        curation._prepared_placement(vault, preparation)


def test_invalid_witness_blocks_instead_of_guessing_or_retrying(vault: Path, monkeypatch) -> None:  # noqa: ANN001
    from exomem import curation

    proposed = _propose(vault, _create_step("uncertain", "curation-uncertain"))

    def fault(name: str) -> None:
        if name == "after-leaf-witness":
            raise curation.CurationFault(name)

    monkeypatch.setattr(curation, "_fault_barrier", fault)
    with pytest.raises(curation.CurationFault):
        _apply(vault, proposed)
    store = curation.CurationStore(vault)
    operation = curation.operation_id(proposed["plan_id"], 0, "uncertain")
    evidence = store.evidence_dir(proposed["run_id"]) / f"{operation}.json"
    value = json.loads(evidence.read_text(encoding="utf-8"))
    value["result_digest"] = "0" * 64
    evidence.write_text(json.dumps(value), encoding="utf-8")

    monkeypatch.setattr(curation, "_fault_barrier", lambda _name: None)
    with pytest.raises(curation.CurationError, match="CURATION_OUTCOME_UNCERTAIN"):
        curation.resume(vault, run_id=proposed["run_id"], plan_id=proposed["plan_id"])
    assert curation.status(vault, run_id=proposed["run_id"])["phase"] == "blocked"


def test_witness_rejects_a_digest_valid_postimage_for_an_unreviewed_path(
    vault: Path, monkeypatch
) -> None:  # noqa: ANN001
    from exomem import curation

    proposed = _propose(vault, _create_step("scope", "curation-witness-scope"))

    def fault(name: str) -> None:
        if name == "after-leaf-witness":
            raise curation.CurationFault(name)

    monkeypatch.setattr(curation, "_fault_barrier", fault)
    with pytest.raises(curation.CurationFault):
        _apply(vault, proposed)
    store = curation.CurationStore(vault)
    operation = curation.operation_id(proposed["plan_id"], 0, "scope")
    evidence = store.evidence_dir(proposed["run_id"]) / f"{operation}.json"
    value = json.loads(evidence.read_text(encoding="utf-8"))
    actual = vault / value["after"][0]["path"]
    foreign = vault / "Knowledge Base/Notes/Insights/unreviewed-foreign.md"
    foreign.write_text(actual.read_text(encoding="utf-8"), encoding="utf-8")
    value["after"] = [
        {
            "path": foreign.relative_to(vault).as_posix(),
            "content_hash": content_hash(foreign.read_text(encoding="utf-8")),
        }
    ]
    basis = {
        key: item for key, item in value.items() if key not in {"result_digest", "committed_at"}
    }
    value["result_digest"] = curation._digest(basis)
    evidence.write_text(json.dumps(value), encoding="utf-8")

    monkeypatch.setattr(curation, "_fault_barrier", lambda _name: None)
    with pytest.raises(curation.CurationError, match="CURATION_OUTCOME_UNCERTAIN"):
        curation.resume(vault, run_id=proposed["run_id"], plan_id=proposed["plan_id"])


def test_terminal_receipt_crash_replays_without_reexecuting_leaf(vault: Path, monkeypatch) -> None:  # noqa: ANN001
    from exomem import curation

    proposed = _propose(vault, _create_step("terminal", "curation-terminal-crash"))
    calls = 0
    real = commands.op_remember

    def counted(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        nonlocal calls
        if not kwargs.get("validate_only"):
            calls += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(commands, "op_remember", counted)

    def fault(name: str) -> None:
        if name == "after-terminal-receipt":
            raise curation.CurationFault(name)

    monkeypatch.setattr(curation, "_fault_barrier", fault)
    with pytest.raises(curation.CurationFault, match="after-terminal-receipt"):
        _apply(vault, proposed)

    inspected = curation.status(vault, run_id=proposed["run_id"])
    assert inspected["phase"] == "completed"
    assert inspected["recovery"] is None
    assert calls == 1

    monkeypatch.setattr(curation, "_fault_barrier", lambda _name: None)
    replayed = curation.resume(vault, run_id=proposed["run_id"], plan_id=proposed["plan_id"])
    assert replayed["phase"] == "completed"
    assert replayed["outcome"] == "replayed"
    assert replayed["mutated"] is False
    assert calls == 1


def test_stale_next_step_records_partial_terminal_truth(vault: Path) -> None:
    from exomem import curation

    proposed = _propose(
        vault,
        _create_step("first", "curation-partial-first"),
        _create_step("second", "curation-partial-second"),
    )
    first = _apply(vault, proposed)
    assert first["phase"] == "executing"

    collision = vault / "Knowledge Base/Notes/Insights/curation-partial-second.md"
    collision.parent.mkdir(parents=True, exist_ok=True)
    collision.write_text("later work", encoding="utf-8")
    with pytest.raises(curation.CurationError, match="CURATION_BINDING_STALE"):
        curation.resume(vault, run_id=proposed["run_id"], plan_id=proposed["plan_id"])

    inspected = curation.status(vault, run_id=proposed["run_id"])
    assert inspected["phase"] == "partial"
    assert inspected["committed_steps"] == ["first"]
    assert inspected["failed_step"] == "second"
    assert inspected["next_action"] == "propose-compensation"


def test_retryable_precommit_failure_uses_next_attempt_and_same_operation(
    vault: Path, monkeypatch
) -> None:  # noqa: ANN001
    from exomem import curation

    proposed = _propose(vault, _create_step("retry", "curation-retryable"))
    real = curation._dispatch_step
    calls = 0

    def flaky(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("temporary writer failure")
        return real(*args, **kwargs)

    monkeypatch.setattr(curation, "_dispatch_step", flaky)
    with pytest.raises(curation.CurationError, match="CURATION_RETRYABLE_FAILURE"):
        _apply(vault, proposed)
    failed = curation.status(vault, run_id=proposed["run_id"])
    assert failed["phase"] == "failed"
    assert failed["next_action"] == "resume"

    completed = curation.resume(vault, run_id=proposed["run_id"], plan_id=proposed["plan_id"])
    assert completed["phase"] == "completed"
    assert completed["step"]["attempt"] == 2
    assert completed["step"]["operation_id"] == curation.operation_id(
        proposed["plan_id"], 0, "retry"
    )


def test_create_entity_uses_real_leaf_and_witness_binds_exact_postimage(vault: Path) -> None:
    proposed = _propose(
        vault,
        {
            "step_id": "entity",
            "kind": "create-entity",
            "args": {
                "entity_type": "concept",
                "name": "Governed Curation Entity",
                "slug": "governed-curation-entity",
                "summary": "A concept created through the ordinary typed entity writer.",
            },
        },
    )

    result = _apply(vault, proposed)
    path = vault / result["step"]["path"]
    witness = _witness(vault, proposed, "entity")

    assert path.is_file()
    assert witness["leaf_identity"] == "connect_memory:create-entity"
    assert witness["after"] == [
        {
            "path": result["step"]["path"],
            "content_hash": content_hash(path.read_text(encoding="utf-8")),
        }
    ]


def test_edit_and_supersede_use_real_leaves_with_exact_postimages(vault: Path) -> None:
    edit_path = _seed_note(vault, "curation-real-edit")
    before = (vault / edit_path).read_text(encoding="utf-8")
    edit = _propose(
        vault,
        {
            "step_id": "edit",
            "kind": "edit",
            "args": {
                "path": edit_path,
                "why": "Apply the reviewed exact wording.",
                "operation": {
                    "kind": "replace_string",
                    "old_string": "old text",
                    "new_string": "new text",
                    "expected_hash": content_hash(before),
                    "relation_disposition": "reviewed_none",
                    "relation_review_reason": "No relation changes.",
                },
            },
        },
    )
    _apply(vault, edit)
    edited = (vault / edit_path).read_text(encoding="utf-8")
    assert "new text" in edited
    assert _witness(vault, edit, "edit")["after"][0]["content_hash"] == content_hash(edited)

    supersede = _propose(
        vault,
        {
            "step_id": "supersede",
            "kind": "supersede",
            "args": {
                "old_path": edit_path,
                "title": "Governed Curation Successor",
                "slug": "governed-curation-successor",
                "note_type": "insight",
                "content": "## Observations\n\n- [finding] Preserve history. ^history\n",
                "reason": "The reviewed conclusion changed.",
            },
        },
    )
    superseded = _apply(vault, supersede)
    successor_path = superseded["step"]["path"]
    successor = (vault / successor_path).read_text(encoding="utf-8")
    supersede_after = _witness(vault, supersede, "supersede")["after"]
    assert {
        "path": successor_path,
        "content_hash": content_hash(successor),
    } in supersede_after
    assert any(item["path"] == edit_path and item.get("content_hash") for item in supersede_after)

def test_accept_relation_uses_the_real_review_queue_leaf_and_exact_postimage(
    vault: Path,
) -> None:
    from exomem import curation, find

    birch = _seed_note(vault, "curation-relation-birch", "A measured fact.")
    linked_arguments = {
        "title": "Curation relation acorn",
        "slug": "curation-relation-acorn",
        "content": (
            "## Observations\n\n- [finding] Body mentions "
            f"[[{birch.removesuffix('.md')}]] inline. ^curation-relation-acorn\n"
        ),
        "note_type": "insight",
    }
    validation = commands.op_remember(vault, validate_only=True, **linked_arguments)
    linked = commands.op_remember(
        vault,
        **linked_arguments,
        draft_id=validation["draft_id"],
        draft_hash=validation["draft_hash"],
        draft_token=validation["draft_token"],
    )
    acorn = linked["path"]
    find.clear_cache()
    review = commands.op_review_memory(vault, mode="relation-queue")
    candidate = next(
        item
        for group in review["groups"]
        for item in group["items"]
        if item["from"] == acorn and item["to"] == birch
    )
    expected_hash = next(
        group["content_hash"] for group in review["groups"] if group["path"] == acorn
    )
    proposed = _propose(
        vault,
        {
            "step_id": "relation",
            "kind": "accept-relation",
            "args": {
                "ref": candidate["ref"],
                "expected_hash": expected_hash,
                "why": "Accept the reviewed relation candidate.",
                "expected_fingerprint": candidate["fingerprint"],
            },
        },
    )
    _apply(vault, proposed)
    authored = (vault / acorn).read_text(encoding="utf-8")
    assert "## Relations" in authored
    assert _witness(vault, proposed, "relation")["after"] == [
        {"path": acorn, "content_hash": content_hash(authored)}
    ]
    assert curation.status(vault, run_id=proposed["run_id"])["phase"] == "completed"
