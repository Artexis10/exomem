"""Admission controls observations and publication of exact lifecycle state."""

import hashlib
import os
from dataclasses import replace
from pathlib import Path

import pytest
from test_connector_boundary import SCOPE
from test_connector_boundary import configured_boundary as configured_boundary
from test_lifecycle_admission import IDENTITY, PAGE, SOURCE

from exomem import relation_review, semantic_contract, semantic_writes, vocabulary_auxiliaries
from exomem import vault as vault_module
from exomem.governance import principal


def _write_primary(root: Path) -> tuple[str, vault_module.PathGuard]:
    page = root / PAGE
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(SOURCE, encoding="utf-8")
    anchor = root / "Knowledge Base/Entities/Organizations/General Context.md"
    anchor.parent.mkdir(parents=True, exist_ok=True)
    anchor.write_text(
        "---\ntype: entity\nentity_type: organization\ntitle: General Context\n"
        "status: active\nproject: public-project\n---\n# General Context\n",
        encoding="utf-8",
    )
    return vault_module.read_guarded_text(root, page)


def _deny_paths(root: Path, *paths: str) -> None:
    scope = root / "Knowledge Base/_Governance/scopes/private.yaml"
    scope.write_text(
        f"governance_version: 1\nid: {SCOPE}\nprojects: [private-project]\npaths:\n"
        + "".join(f"  - '{path}'\n" for path in paths),
        encoding="utf-8",
    )


def test_full_restricted_mcp_edit_ignores_denied_lifecycle_overflow(
    configured_boundary, vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Denied lifecycle state must not stop a supported compiled-page edit."""
    import asyncio
    import datetime as dt
    import json
    import shutil

    from fastmcp import Client

    from exomem import memory_refs, server, state_migration, temporal

    _, authenticate = configured_boundary
    _write_primary(vault)
    directory = relation_review.lifecycle_prepared_path(vault, IDENTITY).parent
    relative = directory.relative_to(vault).as_posix()
    _deny_paths(vault, f"{relative}/withheld-*.json")
    monkeypatch.setattr(temporal, "now", lambda: dt.datetime(2025, 1, 1, tzinfo=dt.UTC))
    monkeypatch.setattr(server, "load_dotenv", lambda *args, **kwargs: None)
    observations = []
    for world in ("absent", "withheld"):
        root = tmp_path / world
        shutil.copytree(vault, root)
        if world == "withheld":
            hidden_directory = root / relative
            hidden_directory.mkdir(parents=True)
            for index in range(270):
                (hidden_directory / f"withheld-{index}.json").write_bytes(b"not a decision")
        authority = state_migration.assert_offline_migration_authority(
            source="isolated full lifecycle edit proof",
        )
        state_migration.arm_connector_boundary_offline(root, authority=authority)
        monkeypatch.setenv("EXOMEM_VAULT_PATH", str(root))
        authenticate("limited")
        mcp = server.build_server(require_auth=False)

        async def invoke(mcp=mcp):
            async with Client(mcp) as client:
                response = await client.call_tool("edit_memory", {
                    "path": PAGE,
                    "why": "Record the completed round",
                    "operation": {
                        "kind": "replace_string",
                        "old_string": "An earlier round.",
                        "new_string": "The completed round.",
                    },
                })
                return response.data

        response = asyncio.run(invoke())
        written = response.get("result", response)
        assert written["status"] == "committed", json.dumps(written)
        final = (root / PAGE).read_text(encoding="utf-8")
        assert memory_refs.ref_from_markdown(final) == memory_refs.memory_ref(IDENTITY)
        prepared = relation_review.lifecycle_prepared_path(root, IDENTITY)
        record = json.loads(prepared.read_bytes())
        assert record["page_identity"] == IDENTITY
        assert record["before_source_hash"] == vault_module.content_hash(SOURCE)
        assert record["after_source_hash"] == vault_module.content_hash(final)
        observations.append((final, record["after_fingerprint"], {
            key: written.get(key) for key in ("status", "path", "relation_advisory")
        }))
        if world == "withheld":
            assert all(
                (root / relative / f"withheld-{index}.json").read_bytes() == b"not a decision"
                for index in range(270)
            )
    assert observations[0] == observations[1]


@pytest.mark.parametrize("target", ["prepared", "current_decision", "proposed_decision"])
def test_full_preflight_denied_target_refuses_before_observing_its_bytes(
    configured_boundary, vault: Path, target: str,
) -> None:
    """An unavailable required artifact has the same outcome before and after arrival."""
    _, authenticate = configured_boundary
    before, _ = _write_primary(vault)
    after = before.replace("An earlier round.", "The completed round.")
    refusals = []
    with principal.request_scope(authenticate("limited")):
        preview = semantic_writes.preflight_existing(
            vault, path=PAGE, after_source=after, operation="edit",
        )
        assert preview.applicability == "full"
        if target == "prepared":
            artifact = relation_review.lifecycle_prepared_path(vault, IDENTITY)
        else:
            state = preview.before if target == "current_decision" else preview.after
            artifact = relation_review.lifecycle_decision_path(
                vault, IDENTITY, state.review_fingerprint,
            )
        _deny_paths(vault, artifact.relative_to(vault).as_posix())
        for present in (False, True):
            if present:
                artifact.parent.mkdir(parents=True, exist_ok=True)
                artifact.write_bytes(b"{" * 20_000)
            with pytest.raises((ValueError, relation_review.RelationReviewError)) as refused:
                semantic_writes.preflight_existing(
                    vault, path=PAGE, after_source=after, operation="edit",
                )
            refusals.append((type(refused.value), str(refused.value)))
            assert (vault / PAGE).read_text(encoding="utf-8") == before
    assert refusals[0] == refusals[1]


def test_denied_lifecycle_siblings_do_not_spend_history_or_residue_limits(
    configured_boundary, vault: Path,
) -> None:
    """A restricted census must ignore denied overflow, aliases and unsafe leaves."""
    _, authenticate = configured_boundary
    before, guard = _write_primary(vault)
    directory = relation_review.lifecycle_prepared_path(vault, IDENTITY).parent
    relative = directory.relative_to(vault).as_posix()
    _deny_paths(vault, f"{relative}/0*.json", f"{relative}/withheld*", f"{relative}/.exomem-batch-*")
    with principal.request_scope(authenticate("limited")):
        admission = vocabulary_auxiliaries.lifecycle_admission(
            vault, primary_path=PAGE, current_source=before, proposed_source=before, primary_guard=guard,
        )
        assert relation_review.load_lifecycle_prepared(vault, IDENTITY, admission=admission) is None
        directory.mkdir(parents=True)
        for index in range(270):
            (directory / f"{index:064x}.json").write_bytes(b"not a decision")
        for index in range(70):
            (directory / f".exomem-batch-{index}").write_bytes(b"not a workspace")
        (directory / "withheld-alias.JSON").symlink_to("missing-target")
        (directory / "withheld-\udcff").write_bytes(b"not UTF-8 named")
        assert relation_review.load_lifecycle_prepared(vault, IDENTITY, admission=admission) is None
        _, names, census = relation_review._inspect_lifecycle_identity(vault, IDENTITY, admission=admission)
        assert names == ()
        assert census.entries == ()
        census.recheck(vault)
        with pytest.raises(relation_review.RelationReviewError):
            relation_review.load_lifecycle_prepared(vault, IDENTITY)


@pytest.mark.parametrize("target", ["prepared", "decision"])
def test_denied_required_target_has_the_same_refusal_when_absent_or_present(
    configured_boundary, vault: Path, target: str,
) -> None:
    """Required-target denial must precede existence, size and parser errors."""
    _, authenticate = configured_boundary
    before, guard = _write_primary(vault)
    fingerprint = semantic_contract.review_content_fingerprint(IDENTITY, before)
    path = (
        relation_review.lifecycle_prepared_path(vault, IDENTITY) if target == "prepared"
        else relation_review.lifecycle_decision_path(vault, IDENTITY, fingerprint)
    )
    with principal.request_scope(authenticate("limited")):
        admission = vocabulary_auxiliaries.lifecycle_admission(
            vault, primary_path=PAGE, current_source=before, proposed_source=before, primary_guard=guard,
        )
        _deny_paths(vault, path.relative_to(vault).as_posix())
        refusals = []
        for present in (False, True):
            if present:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"{" * 20_000)
            with pytest.raises(ValueError) as refused:
                if target == "prepared":
                    relation_review.load_lifecycle_prepared(vault, IDENTITY, admission=admission)
                else:
                    relation_review.load_lifecycle_decision(vault, IDENTITY, fingerprint, admission=admission)
            refusals.append(str(refused.value))
        assert refusals == ["WRITE_REFUSED: target is unavailable"] * 2


@pytest.mark.parametrize("change", ["proposed", "current", "permission", "excluded"])
def test_primary_or_policy_change_cannot_keep_operational_admission(
    configured_boundary, vault: Path, change: str,
) -> None:
    """An allowed primary must not authorize a denied result or a changed source."""
    _, authenticate = configured_boundary
    before, guard = _write_primary(vault)
    with principal.request_scope(authenticate("limited")):
        if change == "proposed":
            with pytest.raises(ValueError, match="target is unavailable"):
                vocabulary_auxiliaries.lifecycle_admission(
                    vault, primary_path=PAGE, current_source=before,
                    proposed_source=before.replace("public-project", "private-project"), primary_guard=guard,
                )
            return
        admission = vocabulary_auxiliaries.lifecycle_admission(
            vault, primary_path=PAGE, current_source=before, proposed_source=before, primary_guard=guard,
        )
        if change == "current":
            (vault / PAGE).write_text(before + "Changed externally.\n", encoding="utf-8")
        elif change == "permission":
            _deny_paths(vault, PAGE)
        else:
            (vault / "Knowledge Base/_access.yaml").write_text("excluded:\n  - Notes/Insights\n", encoding="utf-8")
        with pytest.raises(ValueError):
            admission(relation_review.lifecycle_prepared_path(vault, IDENTITY).relative_to(vault).as_posix())


def test_newly_withheld_primary_refuses_before_disclosing_staleness(
    configured_boundary, vault: Path,
) -> None:
    """A preflight cannot reveal later source changes after the target becomes withheld."""
    _, authenticate = configured_boundary
    before, guard = _write_primary(vault)
    with principal.request_scope(authenticate("limited")):
        admission = vocabulary_auxiliaries.lifecycle_admission(
            vault, primary_path=PAGE, current_source=before,
            proposed_source=before, primary_guard=guard,
        )
        (vault / PAGE).write_text(
            before.replace("public-project", "private-project"), encoding="utf-8",
        )
        with pytest.raises(ValueError, match="^WRITE_REFUSED: target is unavailable$"):
            path = relation_review.lifecycle_prepared_path(vault, IDENTITY)
            admission(path.relative_to(vault).as_posix())


def test_pending_suffix_cannot_hide_primary_drift_from_sealed_full_plan(
    configured_boundary, vault: Path,
) -> None:
    """Per-replacement admission must validate the binding of the complete plan."""
    _, authenticate = configured_boundary
    before, guard = _write_primary(vault)
    after = before.replace("An earlier round.", "The completed round.")
    with principal.request_scope(authenticate("limited")):
        admission = vocabulary_auxiliaries.lifecycle_admission(
            vault, primary_path=PAGE, current_source=before, proposed_source=after, primary_guard=guard,
        )
        prepared = relation_review.build_lifecycle_prepared_transition(
            transition_id="00000000-0000-4000-8000-0000000000ab", operation="edit",
            page_identity=IDENTITY, before_path=PAGE, before_source_hash=vault_module.content_hash(before),
            after_path=PAGE, after_source_hash=vault_module.content_hash(after),
            after_fingerprint=semantic_contract.review_content_fingerprint(IDENTITY, after), decision=None,
            transition_token="exact transition", auxiliary_hash=relation_review.lifecycle_auxiliary_hash((), vault),
        )
        plan = relation_review.plan_lifecycle_transition(
            vault, decision=None, prepared=prepared,
            current=relation_review.LifecyclePrimaryBinding(PAGE, vault_module.content_hash(before),
                                                           semantic_contract.review_content_fingerprint(IDENTITY, before)),
            admission=admission,
        )
        primary = vault_module.PlannedWrite(vault / PAGE, after, guard=guard)
        writes = (*plan.writes, primary)
        manifest = vocabulary_auxiliaries.seal(
            vault, primary=primary, derived=(("lifecycle-review", item) for item in plan.writes),
        )
        vault_module._validate_batch_write_access(vault, writes[1:], proposed=writes, auxiliary_manifest=manifest)
        drifted = (*plan.writes, replace(primary, content=after + "Extra content.\n"))
        with pytest.raises(ValueError, match="VOCABULARY_AUXILIARY_CHANGED"):
            vault_module._validate_batch_write_access(vault, writes[1:], proposed=drifted, auxiliary_manifest=manifest)
        forged = replace(manifest, _seal=None)
        with pytest.raises(ValueError, match="VOCABULARY_AUXILIARY_CHANGED"):
            vault_module._validate_batch_write_access(vault, writes, auxiliary_manifest=forged)
        assert hashlib.sha256((vault / PAGE).read_bytes()).hexdigest() == guard.expected_content_hash


def test_optional_creation_receipt_lookup_ignores_denied_sibling_aliases(
    configured_boundary, vault: Path,
) -> None:
    """Unrelated creation receipts retain independent admission before alias work."""
    _, authenticate = configured_boundary
    directory = relation_review.review_artifact_path(vault, IDENTITY).parent
    directory.mkdir(parents=True, exist_ok=True)
    _deny_paths(vault, f"{directory.relative_to(vault).as_posix()}/*")
    (directory / f"{IDENTITY}.JSON").write_bytes(b"not a receipt")
    (directory / "bad-\udcff.json").write_bytes(b"not a receipt")
    with principal.request_scope(authenticate("limited")):
        assert relation_review.load_creation_receipt(vault, IDENTITY) is None


def test_captured_admitted_entries_are_not_refiltered_on_recheck(
    configured_boundary, vault: Path,
) -> None:
    """Revoking an observed decision must invalidate its retained census."""
    _, authenticate = configured_boundary
    before, guard = _write_primary(vault)
    fingerprint = semantic_contract.review_content_fingerprint(IDENTITY, before)
    decision = relation_review.build_lifecycle_decision(
        page_identity=IDENTITY, after_fingerprint=fingerprint, reason="Reviewed current content",
    )
    path = relation_review.lifecycle_decision_path(vault, IDENTITY, fingerprint)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(relation_review.serialize_lifecycle_decision(decision), encoding="utf-8")
    with principal.request_scope(authenticate("limited")):
        admission = vocabulary_auxiliaries.lifecycle_admission(
            vault, primary_path=PAGE, current_source=before, proposed_source=before, primary_guard=guard,
        )
        _, _, census = relation_review._inspect_lifecycle_identity(vault, IDENTITY, admission=admission)
        assert len(census.entries) == 1
        _deny_paths(vault, path.relative_to(vault).as_posix())
        with pytest.raises(vault_module.PathGuardError, match="census changed"):
            census.recheck(vault)


def test_permission_revoked_during_artifact_read_precedes_parser_and_size_errors(
    configured_boundary, vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read that loses admission cannot expose the denied artifact's shape."""
    _, authenticate = configured_boundary
    before, guard = _write_primary(vault)
    fingerprint = semantic_contract.review_content_fingerprint(IDENTITY, before)
    path = relation_review.lifecycle_decision_path(vault, IDENTITY, fingerprint)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"{" * 20_000)
    identity = path.stat()
    real_read = os.read

    def revoke_when_reading(descriptor, size):
        info = os.fstat(descriptor)
        content = real_read(descriptor, size)
        if (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino):
            _deny_paths(vault, path.relative_to(vault).as_posix())
        return content

    with principal.request_scope(authenticate("limited")):
        admission = vocabulary_auxiliaries.lifecycle_admission(
            vault, primary_path=PAGE, current_source=before, proposed_source=before, primary_guard=guard,
        )
        monkeypatch.setattr(os, "read", revoke_when_reading)
        with pytest.raises(ValueError, match="^WRITE_REFUSED: target is unavailable$"):
            relation_review.load_lifecycle_decision(vault, IDENTITY, fingerprint, admission=admission)


@pytest.mark.parametrize("swap", ["leaf", "directory"])
def test_admitted_lifecycle_read_detects_leaf_or_directory_replacement(
    configured_boundary, vault: Path, monkeypatch: pytest.MonkeyPatch, swap: str,
) -> None:
    """Inherited admission must preserve the existing exact namespace guards."""
    _, authenticate = configured_boundary
    before, guard = _write_primary(vault)
    fingerprint = semantic_contract.review_content_fingerprint(IDENTITY, before)
    path = relation_review.lifecycle_decision_path(vault, IDENTITY, fingerprint)
    decision = relation_review.build_lifecycle_decision(
        page_identity=IDENTITY, after_fingerprint=fingerprint, reason="Reviewed current content",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(relation_review.serialize_lifecycle_decision(decision), encoding="utf-8")
    real_open = os.open
    swapped = False

    def replace_before_open(target, flags, *args, **kwargs):
        nonlocal swapped
        if not swapped and Path(target).name == path.name:
            swapped = True
            if swap == "leaf":
                replacement = path.with_suffix(".replacement")
                replacement.write_bytes(path.read_bytes())
                replacement.replace(path)
            else:
                displaced = path.parent.with_name("displaced")
                path.parent.rename(displaced)
                path.parent.mkdir()
                os.link(displaced / path.name, path)
        return real_open(target, flags, *args, **kwargs)

    with principal.request_scope(authenticate("limited")):
        admission = vocabulary_auxiliaries.lifecycle_admission(
            vault, primary_path=PAGE, current_source=before, proposed_source=before, primary_guard=guard,
        )
        monkeypatch.setattr(os, "open", replace_before_open)
        with pytest.raises(relation_review.RelationReviewError) as refused:
            relation_review.load_lifecycle_decision(vault, IDENTITY, fingerprint, admission=admission)
        assert refused.value.code == "RELATION_REVIEW_SWAPPED"


@pytest.mark.parametrize("existing_directory", [False, True])
def test_revocation_after_primary_replacement_rolls_back_exact_lifecycle_batch(
    configured_boundary, vault: Path, monkeypatch: pytest.MonkeyPatch, existing_directory: bool,
) -> None:
    """The completion check must catch revocation after the primary becomes visible."""
    _, authenticate = configured_boundary
    before, _ = _write_primary(vault)
    after = before.replace("An earlier round.", "The completed round.")
    prepared = relation_review.lifecycle_prepared_path(vault, IDENTITY)
    if existing_directory:
        prepared.parent.mkdir(parents=True)
    real_published = vault_module._after_batch_destination_published

    def revoke_after_primary(path):
        real_published(path)
        if path == vault / PAGE:
            (vault / "Knowledge Base/_access.yaml").write_text(
                "readonly:\n  - Notes/Insights\n", encoding="utf-8",
            )

    with principal.request_scope(authenticate("limited")):
        preflight = semantic_writes.preflight_existing(vault, path=PAGE, after_source=after, operation="edit")
        monkeypatch.setattr(vault_module, "_after_batch_destination_published", revoke_after_primary)
        with pytest.raises(ValueError, match="^WRITE_REFUSED: target is unavailable$"):
            semantic_writes.commit_existing(vault, preflight=preflight)
    assert (vault / PAGE).read_text(encoding="utf-8") == before
    assert not prepared.exists()
    assert prepared.parent.exists() is existing_directory


def test_restricted_committed_replay_remains_mutation_free_and_rechecks_permission(
    configured_boundary, vault: Path,
) -> None:
    """Replay must neither rewrite durable state nor retain revoked permission."""
    _, authenticate = configured_boundary
    before, _ = _write_primary(vault)
    after = before.replace("An earlier round.", "The completed round.")
    with principal.request_scope(authenticate("limited")):
        preflight = semantic_writes.preflight_existing(vault, path=PAGE, after_source=after, operation="edit")
        committed = semantic_writes.commit_existing(vault, preflight=preflight)
        prepared_path = relation_review.lifecycle_prepared_path(vault, IDENTITY)
        before_replay = ((vault / PAGE).read_bytes(), prepared_path.read_bytes())
        replay_preflight = semantic_writes.preflight_existing(
            vault, path=PAGE, after_source=after, operation="edit", transition_token=committed.transition_token,
        )
        replay = semantic_writes.commit_existing(vault, preflight=replay_preflight)
        assert replay.lifecycle_state == "committed_replay"
        assert not replay.mutated
        assert ((vault / PAGE).read_bytes(), prepared_path.read_bytes()) == before_replay
        _deny_paths(vault, prepared_path.relative_to(vault).as_posix())
        with pytest.raises(ValueError, match="target is unavailable"):
            semantic_writes.commit_existing(vault, preflight=replay_preflight)
        assert ((vault / PAGE).read_bytes(), prepared_path.read_bytes()) == before_replay
