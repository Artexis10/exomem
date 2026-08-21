"""Closed internal-state registry and reserved-path classifier contract."""

from __future__ import annotations

import ast
import datetime as dt
import inspect
import os
import pickle
import threading
from contextlib import contextmanager, nullcontext
from dataclasses import replace as dataclass_replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import (
    commands,
    find_corpus,
    media_processing,
    reserved_paths,
    structured_collections,
    vault,
    video_frames,
    writer_lease,
)
from exomem import find as find_module
from exomem.append_to_file import AppendError, append_to_file
from exomem.create_directory import CreateDirectoryError, create_directory
from exomem.create_file import CreateFileError, create_file
from exomem.delete_directory import DeleteDirectoryError, delete_directory
from exomem.delete_file import DeleteFileError, delete_file
from exomem.edit import EditError, edit
from exomem.get_page import GetError, prepare_page_read
from exomem.list_directory import ListDirectoryError, list_directory
from exomem.move_file import MoveFileError, move_file
from exomem.observe_memory import ObserveMemoryError, observe_memory
from exomem.overview import overview
from exomem.query_data import QueryDataError, query_data
from exomem.recover_from_trash import RecoverError, recover_from_trash
from exomem.replace import ReplaceError
from exomem.replace import replace as replace_page


@pytest.mark.parametrize(
    ("raw", "descriptor_id"),
    [
        ("_Governance", "governance-tree"),
        ("Knowledge Base/_Governance/rules/private.yaml", "governance-tree"),
        ("_Consolidation/runs/run.json", "consolidation-tree"),
        ("Knowledge Base/.governance.sqlite", "governance-store"),
        (".embeddings.sqlite-wal", "embeddings-store"),
        (".clip.sqlite-shm", "clip-store"),
        (".lexical.sqlite-journal", "lexical-store"),
        (".graph.sqlite-wal", "graph-store"),
        (".claims.sqlite", "claims-store"),
        (".references.sqlite", "references-store"),
        (".refs.sqlite-shm", "refs-store"),
        (".freshness.sqlite-journal", "freshness-store"),
        (".deferred-index.sqlite", "deferred-index-store"),
        (".deferred_index.sqlite-wal", "deferred-index-store"),
        (".media-jobs.sqlite-shm", "media-jobs-store"),
        (".media_jobs.sqlite", "media-jobs-store"),
        (".idempotency.sqlite-journal", "idempotency-store"),
        (".idempotency.jsonl", "idempotency-store"),
        (".voice_profiles.json", "voice-profile-store"),
        (".media-worker.lock", "media-jobs-store"),
        (".graph-sync.json", "graph-handoff"),
        (".graph-sync-floor.json", "graph-handoff"),
        (".graph-commit-receipts", "graph-receipts"),
        (".graph-commit-receipts/0123456789abcdef01234567.json", "graph-receipts"),
        (".review-state.json", "review-state"),
        ("..review-state.json.abc123_4.tmp", "review-state"),
        (".lexical.sqlite.rebuild-0123456789abcdef0123456789abcdef.tmp", "lexical-rebuild"),
        (
            ".lexical.sqlite.rebuild-0123456789abcdef0123456789abcdef.tmp-wal",
            "lexical-rebuild",
        ),
        (".lexical.sqlite.quarantine-0123456789abcdef0123456789abcdef", "lexical-quarantine"),
        (
            ".lexical.sqlite-wal.quarantine-0123456789abcdef0123456789abcdef",
            "lexical-quarantine",
        ),
        (
            ".graph-rebuild-"
            + "a" * 64
            + "-"
            + "b" * 24
            + ".sqlite-shm",
            "graph-rebuild",
        ),
        (".graph-reset-" + "c" * 24 + "/.manifest.json", "graph-reset"),
        (".authorization-projections", "authorization-projections"),
        (".authorization-projections/generation/rows.sqlite", "authorization-projections"),
        (
            "Notes/.exomem-batch-0123456789abcdef0123456789abcdef/stage-0.tmp",
            "batch-workspace",
        ),
        (
            "Notes/.exomem-held-publish-0123456789abcdef0123456789abcdef",
            "held-publication",
        ),
    ],
)
def test_closed_registry_reserves_exact_static_sqlite_temp_and_quarantine_families(
    raw: str, descriptor_id: str
) -> None:
    result = reserved_paths.classify_logical(raw)

    assert result.disposition is reserved_paths.PathDisposition.RESERVED
    assert result.descriptor_id == descriptor_id


@pytest.mark.parametrize(
    "raw",
    [
        "Knowledge Base/Notes/.governance.sqlite",
        "Knowledge Base/_Governance.md",
        ".review-state.json.abc123_4.tmp",
        "..review-state.json.abc123-4.tmp",
        "..review-state.json.abc12345.tmp-more",
        ".lexical.sqlite.rebuild-0123.tmp",
        ".lexical.sqlite.rebuild-" + "g" * 32 + ".tmp",
        ".lexical.sqlite.quarantine-" + "0" * 31,
        ".graph-rebuild-user-copy.sqlite",
        ".graph-reset-" + "0" * 23,
        ".authorization-projections-user",
    ],
)
def test_exact_shapes_do_not_reserve_lookalikes(raw: str) -> None:
    result = reserved_paths.classify_logical(raw)

    assert result.disposition is reserved_paths.PathDisposition.ORDINARY
    assert result.descriptor_id is None


@pytest.mark.parametrize(
    "raw",
    [
        "KNOWLEDGE BASE\\_GOVERNANCE\\rules\\policy.yaml",
        "knowledge base/_governance/rules/policy.yaml",
        "Ｋｎｏｗｌｅｄｇｅ Ｂａｓｅ／＿Ｇｏｖｅｒｎａｎｃｅ／rules／policy.yaml",
        "Knowledge Base/.GOVERNANCE.SQLITE-WAL",
    ],
)
def test_logical_classifier_normalizes_prefix_case_nfkc_and_separators(raw: str) -> None:
    result = reserved_paths.classify_logical(raw)

    assert result.disposition is reserved_paths.PathDisposition.RESERVED


@pytest.mark.parametrize(
    "raw",
    [
        "/Knowledge Base/_Governance/rules/policy.yaml",
        "//server/share/Knowledge Base/_Governance/rules/policy.yaml",
        r"\\server\share\Knowledge Base\_Governance\rules\policy.yaml",
        "C:" + r"\Knowledge Base\_Governance\rules\policy.yaml",
        "Knowledge Base/_Governance/../Notes/page.md",
        "Knowledge Base/./_Governance/rules/policy.yaml",
        "Knowledge Base/.governance.sqlite:stream",
        "Knowledge Base//_Governance/rules/policy.yaml",
        "exomem://memory/00000000-0000-4000-8000-000000000000",
    ],
)
def test_noncanonical_platform_and_reference_spellings_are_refused(raw: str) -> None:
    result = reserved_paths.classify_logical(raw)

    assert result.disposition is reserved_paths.PathDisposition.INVALID
    assert result.blocked


def test_closed_registry_matches_independent_owner_inventory(tmp_path: Path) -> None:
    from exomem import (
        claims,
        deferred_index,
        epistemic_graph,
        graph_sync,
        index_paths,
        lexstore,
        media_jobs,
        memory_refs,
        review_state,
        voice_profiles,
    )
    from exomem.governance import policy as governance_policy

    expected = {
        "authorization-projections",
        "batch-workspace",
        "claims-store",
        "clip-store",
        "consolidation-tree",
        "deferred-index-store",
        "embeddings-store",
        "freshness-store",
        "governance-store",
        "governance-tree",
        "graph-handoff",
        "graph-rebuild",
        "graph-receipts",
        "graph-reset",
        "graph-store",
        "idempotency-store",
        "held-publication",
        "lexical-quarantine",
        "lexical-rebuild",
        "lexical-store",
        "media-jobs-store",
        "references-store",
        "refs-store",
        "review-state",
        "voice-profile-store",
    }

    registry = reserved_paths.internal_state_registry()

    assert reserved_paths.REGISTRY_VERSION == 1
    assert {descriptor.id for descriptor in registry} == expected
    assert all(descriptor.owner and descriptor.owning_command != "*" for descriptor in registry)
    assert len(registry) == len({descriptor.id for descriptor in registry})

    root = tmp_path / "vault"
    graph_live = epistemic_graph.sidecar_path(root)
    lexical_live = lexstore.lexical_path(root)
    token32 = "1" * 32
    claims_from_owners = [
        (governance_policy.governance_root(root), "governance-tree"),
        (index_paths.governance_sidecar_path(root), "governance-store"),
        (index_paths.sidecar_path(root), "embeddings-store"),
        (index_paths.clip_sidecar_path(root), "clip-store"),
        (lexical_live, "lexical-store"),
        (graph_live, "graph-store"),
        (claims.sidecar_path(root), "claims-store"),
        (memory_refs.sidecar_path(root), "refs-store"),
        (deferred_index.store_path(root), "deferred-index-store"),
        (media_jobs.job_store_path(root), "media-jobs-store"),
        (media_jobs.worker_lock_path(root), "media-jobs-store"),
        (voice_profiles.voice_profiles_path(root), "voice-profile-store"),
        (graph_sync.checkpoint_path(root), "graph-handoff"),
        (graph_sync.floor_path(root), "graph-handoff"),
        (
            graph_sync.graph_commit_receipt_path(root, "2" * 24),
            "graph-receipts",
        ),
        (review_state.state_path(root), "review-state"),
        (
            review_state.state_path(root).with_name(
                f".{review_state.STATE_FILENAME}.abc123_4.tmp"
            ),
            "review-state",
        ),
        (
            lexical_live.with_name(f"{lexical_live.name}.rebuild-{token32}.tmp"),
            "lexical-rebuild",
        ),
        (
            lexical_live.with_name(f"{lexical_live.name}-wal.quarantine-{token32}"),
            "lexical-quarantine",
        ),
        (
            graph_sync.temporary_sidecar_path(
                graph_live,
                SimpleNamespace(checkpoint_sha256="3" * 64),
            ),
            "graph-rebuild",
        ),
        (graph_sync._reset_directory(root, "4" * 24), "graph-reset"),
    ]
    for primary, descriptor_id in tuple(claims_from_owners):
        if primary.name.endswith(".sqlite"):
            claims_from_owners.extend(
                (primary.with_name(f"{primary.name}{suffix}"), descriptor_id)
                for suffix in ("-wal", "-shm", "-journal")
            )

    seen: set[str] = set()
    for owned_path, descriptor_id in claims_from_owners:
        relative = owned_path.relative_to(root).as_posix()
        assert relative not in seen
        seen.add(relative)
        classified = reserved_paths.classify_logical(relative)
        assert classified.disposition is reserved_paths.PathDisposition.RESERVED, relative
        assert classified.descriptor_id == descriptor_id, relative


def test_identity_catalogue_enumerates_only_from_the_held_kb_anchor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    governance = tmp_path / "Knowledge Base" / "_Governance"
    workspace = (
        tmp_path
        / "Knowledge Base"
        / "Notes"
        / ".exomem-batch-0123456789abcdef0123456789abcdef"
    )
    governance.mkdir(parents=True)
    workspace.mkdir(parents=True)
    (governance / "policy.yaml").write_text("version: 1\n", encoding="utf-8")
    (workspace / "stage-0.tmp").write_bytes(b"private staging")

    real_scandir = os.scandir

    def retained_scandir(target):  # noqa: ANN001
        if not isinstance(target, int):
            pytest.fail("identity catalogue reopened the Knowledge Base by pathname")
        return real_scandir(target)

    monkeypatch.setattr(os, "scandir", retained_scandir)

    catalogue = reserved_paths.IdentityCatalogue.from_vault(tmp_path)

    assert "governance-tree" in catalogue.identities.values()
    assert "batch-workspace" in catalogue.identities.values()


def test_physical_alias_symlink_and_hardlink_targets_fail_closed(tmp_path: Path) -> None:
    kb = tmp_path / "Knowledge Base"
    governance = kb / "_Governance"
    notes = kb / "Notes"
    governance.mkdir(parents=True)
    notes.mkdir()
    policy = governance / "policy.bin"
    policy.write_bytes(b"private")

    symlink = notes / "policy-symlink.bin"
    try:
        symlink.symlink_to(policy)
    except (NotImplementedError, OSError):
        pytest.skip("symlinks are unavailable")

    catalogue = reserved_paths.IdentityCatalogue.from_vault(tmp_path)
    symlink_result = reserved_paths.classify_physical(
        tmp_path,
        symlink.relative_to(tmp_path).as_posix(),
        identities=catalogue,
    )
    assert symlink_result.blocked

    hardlink = notes / "policy-hardlink.bin"
    try:
        os.link(policy, hardlink)
    except OSError:
        pytest.skip("hard links are unavailable")
    hardlink_result = reserved_paths.classify_physical(
        tmp_path,
        hardlink.relative_to(tmp_path).as_posix(),
        identities=catalogue,
    )
    assert hardlink_result.disposition is reserved_paths.PathDisposition.RESERVED
    assert hardlink_result.descriptor_id == "governance-tree"


def test_generic_read_refuses_parent_exchange_after_leaf_acquisition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import held_fs

    notes = tmp_path / "Knowledge Base" / "Notes"
    notes.mkdir(parents=True)
    ordinary = notes / "ordinary.md"
    ordinary.write_text("ordinary", encoding="utf-8")

    acquired = held_fs.acquire(tmp_path)
    assert acquired.ok
    filesystem_type = type(acquired.require())
    acquired.require().close()
    real_file = filesystem_type.file
    acquired_leaf = threading.Event()
    release = threading.Event()
    paused = False

    def pause_after_leaf(self, parent, leaf, **kwargs):  # noqa: ANN001
        nonlocal paused
        result = real_file(self, parent, leaf, **kwargs)
        if leaf == ordinary.name and result.ok and not paused:
            paused = True
            acquired_leaf.set()
            assert release.wait(5)
        return result

    monkeypatch.setattr(filesystem_type, "file", pause_after_leaf)
    outcomes: list[object] = []

    def read() -> None:
        try:
            outcomes.append(
                reserved_paths.read_generic_bytes(
                    tmp_path,
                    "Knowledge Base/Notes/ordinary.md",
                )
            )
        except BaseException as error:  # noqa: BLE001 - thread outcome assertion
            outcomes.append(error)

    worker = threading.Thread(target=read, daemon=True)
    worker.start()
    assert acquired_leaf.wait(5)
    displaced = tmp_path / "Knowledge Base" / "Notes-displaced"
    exchange_blocked = False
    try:
        notes.rename(displaced)
    except PermissionError:
        exchange_blocked = True
    else:
        notes.mkdir()
    finally:
        release.set()
    worker.join(5)

    assert not worker.is_alive()
    assert len(outcomes) == 1
    if exchange_blocked:
        assert isinstance(outcomes[0], reserved_paths.GenericFileSnapshot)
        assert outcomes[0].data == b"ordinary"
        assert ordinary.read_text(encoding="utf-8") == "ordinary"
        assert not displaced.exists()
        return
    assert isinstance(outcomes[0], reserved_paths.ReservedPathLeafError)
    assert outcomes[0].code == "IDENTITY_CHANGED"
    assert (displaced / ordinary.name).read_text(encoding="utf-8") == "ordinary"
    assert not (notes / ordinary.name).exists()


def test_published_private_identity_is_implicitly_consumed_by_generic_leaf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kb = tmp_path / "Knowledge Base"
    notes = kb / "Notes"
    notes.mkdir(parents=True)
    private = kb / ".embeddings.sqlite"
    private.write_bytes(b"private index bytes")
    alias = notes / "ordinary.bin"
    alias.write_bytes(b"same physical file through a platform alias")
    identity = reserved_paths._lstat_identity(alias)

    with reserved_paths._subsystem_authority_scope("embedding_index"):
        with reserved_paths._identity_coordination_scope(tmp_path):
            reserved_paths._publish_owner_identities(
                tmp_path,
                "embeddings-store",
                {"Knowledge Base/.embeddings.sqlite": identity},
            )

    monkeypatch.setattr(
        reserved_paths,
        "_published_identity_catalogue",
        lambda _vault_root: reserved_paths.IdentityCatalogue(
            {(identity.device, identity.inode, identity.kind): "embeddings-store"}
        ),
    )

    with pytest.raises(reserved_paths.ReservedPathLeafError) as error:
        reserved_paths.read_generic_bytes(
            tmp_path,
            "Knowledge Base/Notes/ordinary.bin",
        )

    assert error.value.code == "RESERVED_PATH"


def test_owner_identity_publication_requires_exact_authority_and_coordination(
    tmp_path: Path,
) -> None:
    kb = tmp_path / "Knowledge Base"
    kb.mkdir()
    private = kb / ".embeddings.sqlite"
    private.write_bytes(b"private")
    identities = {
        "Knowledge Base/.embeddings.sqlite": reserved_paths._lstat_identity(private)
    }

    with pytest.raises(RuntimeError, match="authority"):
        reserved_paths._publish_owner_identities(
            tmp_path,
            "embeddings-store",
            identities,
        )

    with reserved_paths._subsystem_authority_scope("embedding_index"):
        with pytest.raises(RuntimeError, match="coordination"):
            reserved_paths._publish_owner_identities(
                tmp_path,
                "embeddings-store",
                identities,
            )

    with reserved_paths._subsystem_authority_scope("claims"):
        with reserved_paths._identity_coordination_scope(tmp_path):
            with pytest.raises(RuntimeError, match="authority"):
                reserved_paths._publish_owner_identities(
                    tmp_path,
                    "embeddings-store",
                    identities,
                )


def test_identity_coordination_does_not_take_the_content_mutation_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "state"
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=state_dir),
        mutation_timeout_seconds=0.05,
    )
    content_boundary = manager._mutation_coordinator_for(vault_root)
    holding = threading.Event()
    release = threading.Event()

    def hold_content_boundary() -> None:
        with content_boundary.hold(
            operation="content-write",
            holder_kind="test",
        ):
            holding.set()
            assert release.wait(5)

    worker = threading.Thread(target=hold_content_boundary, daemon=True)
    worker.start()
    assert holding.wait(2)
    monkeypatch.setattr(writer_lease, "active_manager", lambda: manager)

    try:
        with reserved_paths._identity_coordination_scope(vault_root):
            assert reserved_paths._identity_coordination_active(vault_root)
    finally:
        release.set()
        worker.join(5)

    assert not worker.is_alive()


def test_owner_identity_domains_are_independent_but_generic_scope_covers_all(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.cli_ops import OpError

    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=tmp_path / "state"),
        mutation_timeout_seconds=0.05,
    )
    monkeypatch.setattr(writer_lease, "active_manager", lambda: manager)
    graph_holding = threading.Event()
    release_graph = threading.Event()

    def hold_graph_identity() -> None:
        with reserved_paths._subsystem_authority_scope("epistemic_graph"):
            with reserved_paths._identity_coordination_scope(vault_root):
                graph_holding.set()
                assert release_graph.wait(5)

    worker = threading.Thread(target=hold_graph_identity, daemon=True)
    worker.start()
    assert graph_holding.wait(2)

    try:
        with reserved_paths._subsystem_authority_scope("graph_sync"):
            with reserved_paths._identity_coordination_scope(vault_root):
                assert reserved_paths._identity_coordination_active(vault_root)

        with pytest.raises(OpError, match="MUTATION_BUSY"):
            with reserved_paths._identity_coordination_scope(vault_root):
                pytest.fail("generic coordination omitted a private owner domain")
    finally:
        release_graph.set()
        worker.join(5)

    assert not worker.is_alive()


def test_one_owner_coordinates_only_the_exact_descriptor_it_is_opening(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=tmp_path / "state"),
        mutation_timeout_seconds=0.05,
    )
    monkeypatch.setattr(writer_lease, "active_manager", lambda: manager)
    rebuild_holding = threading.Event()
    release_rebuild = threading.Event()

    def hold_rebuild_identity() -> None:
        with reserved_paths._subsystem_authority_scope("epistemic_graph"):
            with reserved_paths._identity_coordination_scope(
                vault_root,
                descriptor_ids=("graph-rebuild",),
            ):
                rebuild_holding.set()
                assert release_rebuild.wait(5)

    worker = threading.Thread(target=hold_rebuild_identity, daemon=True)
    worker.start()
    assert rebuild_holding.wait(2)

    try:
        with reserved_paths._subsystem_authority_scope("epistemic_graph"):
            with reserved_paths._identity_coordination_scope(
                vault_root,
                descriptor_ids=("graph-store",),
            ):
                assert reserved_paths._identity_coordination_active(
                    vault_root,
                    "graph-store",
                )
                assert not reserved_paths._identity_coordination_active(
                    vault_root,
                    "graph-rebuild",
                )
    finally:
        release_rebuild.set()
        worker.join(5)

    assert not worker.is_alive()


def test_published_private_directory_identity_is_checked_before_enumeration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kb = tmp_path / "Knowledge Base"
    governance = kb / "_Governance"
    notes = kb / "Notes"
    governance.mkdir(parents=True)
    notes.mkdir()
    (governance / "policy.yaml").write_text("secret: true\n", encoding="utf-8")
    alias = notes / "ordinary-folder"
    alias.mkdir()
    (alias / "policy.yaml").write_text("secret: true\n", encoding="utf-8")
    identity = reserved_paths._lstat_identity(alias)

    with reserved_paths._owner_authority_scope("govern_memory"):
        with reserved_paths._identity_coordination_scope(tmp_path):
            reserved_paths._publish_owner_identities(
                tmp_path,
                "governance-tree",
                {"Knowledge Base/_Governance": identity},
            )

    monkeypatch.setattr(
        reserved_paths,
        "_published_identity_catalogue",
        lambda _vault_root: reserved_paths.IdentityCatalogue(
            {(identity.device, identity.inode, identity.kind): "governance-tree"}
        ),
    )

    with pytest.raises(reserved_paths.ReservedPathLeafError) as error:
        reserved_paths.read_generic_tree(
            tmp_path,
            "Knowledge Base/Notes/ordinary-folder",
        )

    assert error.value.code == "RESERVED_PATH"


def test_create_directory_refuses_published_private_parent_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alias = tmp_path / "Knowledge Base" / "Notes" / "ordinary-folder"
    alias.mkdir(parents=True)
    identity = reserved_paths._lstat_identity(alias)
    catalogue = reserved_paths.IdentityCatalogue(
        {
            (identity.device, identity.inode, identity.kind): "governance-tree",
        }
    )
    monkeypatch.setattr(
        reserved_paths,
        "_published_identity_catalogue",
        lambda _vault_root: catalogue,
    )

    with pytest.raises(CreateDirectoryError) as error:
        create_directory(
            tmp_path,
            path="Knowledge Base/Notes/ordinary-folder/new",
        )

    assert error.value.code == "RESERVED_PATH"
    assert not (alias / "new").exists()


def test_ordinary_generic_read_does_not_take_cross_process_identity_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    note = tmp_path / "Knowledge Base" / "Notes" / "ordinary.md"
    note.parent.mkdir(parents=True)
    note.write_text("ordinary", encoding="utf-8")
    assert reserved_paths.read_generic_bytes(
        tmp_path,
        "Knowledge Base/Notes/ordinary.md",
    ).data == b"ordinary"
    monkeypatch.setattr(
        writer_lease,
        "active_manager",
        lambda: pytest.fail("warmed ordinary held reads must not acquire the mutation lock"),
    )

    snapshot = reserved_paths.read_generic_bytes(
        tmp_path,
        "Knowledge Base/Notes/ordinary.md",
    )

    assert snapshot.data == b"ordinary"


def test_first_generic_leaf_automatically_consumes_bootstrap_identity_catalogue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    note = tmp_path / "Knowledge Base" / "Notes" / "ordinary.bin"
    note.parent.mkdir(parents=True)
    note.write_bytes(b"must stay hidden")
    identity = reserved_paths._lstat_identity(note)
    calls = 0

    def catalogue(_vault_root: Path) -> reserved_paths.IdentityCatalogue:
        nonlocal calls
        calls += 1
        return reserved_paths.IdentityCatalogue(
            {
                (identity.device, identity.inode, identity.kind): "governance-tree",
            }
        )

    monkeypatch.setattr(
        reserved_paths.IdentityCatalogue,
        "from_vault",
        staticmethod(catalogue),
    )

    with pytest.raises(reserved_paths.ReservedPathLeafError) as error:
        reserved_paths.read_generic_bytes(
            tmp_path,
            "Knowledge Base/Notes/ordinary.bin",
        )

    assert error.value.code == "RESERVED_PATH"
    assert calls == 1


def test_direct_read_consumes_held_leaf_and_hides_private_aliases(tmp_path: Path) -> None:
    kb = tmp_path / "Knowledge Base"
    governance = kb / "_Governance"
    notes = kb / "Notes"
    governance.mkdir(parents=True)
    notes.mkdir()
    policy = governance / "policy.md"
    policy.write_text("private policy", encoding="utf-8")

    hardlink = notes / "ordinary-looking.md"
    try:
        os.link(policy, hardlink)
    except OSError:
        pytest.skip("hard links are unavailable")

    with pytest.raises(GetError) as hardlink_error:
        prepare_page_read(tmp_path, path="Knowledge Base/Notes/ordinary-looking.md")

    assert hardlink_error.value.code == "NOT_FOUND"
    assert "private policy" not in hardlink_error.value.reason

    symlink = notes / "ordinary-symlink.md"
    try:
        symlink.symlink_to(policy)
    except (NotImplementedError, OSError):
        pytest.skip("symlinks are unavailable")

    with pytest.raises(GetError) as symlink_error:
        prepare_page_read(tmp_path, path="Knowledge Base/Notes/ordinary-symlink.md")

    assert symlink_error.value.code == "NOT_FOUND"
    assert "private policy" not in symlink_error.value.reason


def test_direct_read_uses_held_bytes_for_an_ordinary_file(tmp_path: Path) -> None:
    note = tmp_path / "Knowledge Base" / "Notes" / "ordinary.md"
    note.parent.mkdir(parents=True)
    note.write_text("ordinary bytes", encoding="utf-8")

    prepared = prepare_page_read(tmp_path, path="Knowledge Base/Notes/ordinary.md")

    assert prepared.raw == b"ordinary bytes"


def test_public_file_move_consumes_held_source_and_refuses_private_hardlink(
    tmp_path: Path,
) -> None:
    governance = tmp_path / "Knowledge Base" / "_Governance"
    notes = tmp_path / "Knowledge Base" / "Notes"
    governance.mkdir(parents=True)
    notes.mkdir()
    policy = governance / "policy.bin"
    policy.write_bytes(b"private")
    alias = notes / "ordinary.bin"
    try:
        os.link(policy, alias)
    except OSError:
        pytest.skip("hard links are unavailable")

    with pytest.raises(MoveFileError) as error:
        move_file(
            tmp_path,
            old_path="Knowledge Base/Notes/ordinary.bin",
            new_path="Knowledge Base/Notes/moved.bin",
            update_wikilinks=False,
        )

    assert error.value.code == "RESERVED_PATH"
    assert alias.exists()
    assert not (notes / "moved.bin").exists()


def test_public_file_move_refuses_private_hardlink_destination_before_planning(
    tmp_path: Path,
) -> None:
    governance = tmp_path / "Knowledge Base" / "_Governance"
    notes = tmp_path / "Knowledge Base" / "Notes"
    governance.mkdir(parents=True)
    notes.mkdir()
    source = notes / "source.bin"
    source.write_bytes(b"ordinary")
    private = governance / "policy.bin"
    private.write_bytes(b"private")
    destination = notes / "destination.bin"
    try:
        os.link(private, destination)
    except OSError:
        pytest.skip("hard links are unavailable")

    with pytest.raises(MoveFileError) as error:
        move_file(
            tmp_path,
            old_path="Knowledge Base/Notes/source.bin",
            new_path="Knowledge Base/Notes/destination.bin",
            update_wikilinks=False,
        )

    assert error.value.code == "RESERVED_PATH"
    assert source.read_bytes() == b"ordinary"
    assert private.read_bytes() == b"private"
    assert destination.exists()


@pytest.mark.parametrize(
    "private_relative",
    [
        "Knowledge Base/.graph-sync.json",
        "Knowledge Base/.graph-sync-floor.json",
        "Knowledge Base/.graph-commit-receipts/" + "1" * 24 + ".json",
        "Knowledge Base/.review-state.json",
        "Knowledge Base/..review-state.json.abc123_4.tmp",
        (
            "Knowledge Base/.lexical.sqlite.rebuild-"
            + "2" * 32
            + ".tmp"
        ),
        (
            "Knowledge Base/.lexical.sqlite.rebuild-"
            + "2" * 32
            + ".tmp-wal"
        ),
        "Knowledge Base/.lexical.sqlite.quarantine-" + "3" * 32,
        "Knowledge Base/.lexical.sqlite-wal.quarantine-" + "3" * 32,
        "Knowledge Base/.lexical.sqlite-shm.quarantine-" + "3" * 32,
        (
            "Knowledge Base/.graph-rebuild-"
            + "4" * 64
            + "-"
            + "5" * 24
            + ".sqlite-shm"
        ),
    ],
)
def test_new_private_family_hardlink_aliases_refuse_as_move_source_and_destination(
    tmp_path: Path,
    private_relative: str,
) -> None:
    private = tmp_path / private_relative
    private.parent.mkdir(parents=True, exist_ok=True)
    private.write_bytes(b"private state")
    notes = tmp_path / "Knowledge Base" / "Notes"
    notes.mkdir()
    source_alias = notes / "source-alias.bin"
    destination_alias = notes / "destination-alias.bin"
    try:
        os.link(private, source_alias)
        os.link(private, destination_alias)
    except OSError:
        pytest.skip("hard links are unavailable")

    with pytest.raises(MoveFileError) as source_error:
        move_file(
            tmp_path,
            old_path="Knowledge Base/Notes/source-alias.bin",
            new_path="Knowledge Base/Notes/moved.bin",
            update_wikilinks=False,
        )

    assert source_error.value.code == "RESERVED_PATH"
    assert source_alias.exists()
    assert not (notes / "moved.bin").exists()

    ordinary = notes / "ordinary.bin"
    ordinary.write_bytes(b"ordinary")
    with pytest.raises(MoveFileError) as destination_error:
        move_file(
            tmp_path,
            old_path="Knowledge Base/Notes/ordinary.bin",
            new_path="Knowledge Base/Notes/destination-alias.bin",
            update_wikilinks=False,
        )

    assert destination_error.value.code == "RESERVED_PATH"
    assert ordinary.read_bytes() == b"ordinary"
    assert destination_alias.read_bytes() == b"private state"


def test_public_file_move_uses_retained_rename_for_ordinary_file(tmp_path: Path) -> None:
    notes = tmp_path / "Knowledge Base" / "Notes"
    notes.mkdir(parents=True)
    source = notes / "ordinary.bin"
    source.write_bytes(b"ordinary")

    result = move_file(
        tmp_path,
        old_path="Knowledge Base/Notes/ordinary.bin",
        new_path="Knowledge Base/Notes/moved.bin",
        update_wikilinks=False,
    )

    assert result.new_path == "Knowledge Base/Notes/moved.bin"
    assert not source.exists()
    assert (notes / "moved.bin").read_bytes() == b"ordinary"


def test_generic_overwrite_refuses_private_hardlink_before_staging(
    tmp_path: Path,
) -> None:
    governance = tmp_path / "Knowledge Base" / "_Governance"
    notes = tmp_path / "Knowledge Base" / "Notes"
    governance.mkdir(parents=True)
    notes.mkdir()
    private = governance / "policy.bin"
    private.write_bytes(b"private")
    alias = notes / "ordinary.bin"
    try:
        os.link(private, alias)
    except OSError:
        pytest.skip("hard links are unavailable")

    with pytest.raises(CreateFileError) as error:
        create_file(
            tmp_path,
            path="Knowledge Base/Notes/ordinary.bin",
            content="replacement",
            overwrite=True,
        )

    assert error.value.code == "RESERVED_PATH"
    assert private.read_bytes() == b"private"
    assert not list(notes.glob(".exomem-batch-*"))


def test_generic_publication_refuses_private_alias_appearing_after_missing_plan(
    tmp_path: Path,
) -> None:
    governance = tmp_path / "Knowledge Base" / "_Governance"
    notes = tmp_path / "Knowledge Base" / "Notes"
    governance.mkdir(parents=True)
    notes.mkdir()
    private = governance / "policy.bin"
    private.write_bytes(b"private")
    target = notes / "generated.json"
    target_rel = target.relative_to(tmp_path).as_posix()

    with pytest.raises(reserved_paths.ReservedPathLeafError) as missing:
        reserved_paths.inspect_generic_file(tmp_path, target_rel)
    assert missing.value.code == "MISSING"
    try:
        os.link(private, target)
    except OSError:
        pytest.skip("hard links are unavailable")

    with pytest.raises(reserved_paths.ReservedPathLeafError) as error:
        reserved_paths.publish_generic_bytes(
            tmp_path,
            target_rel,
            b"generated metadata",
            expected_identity=None,
        )

    assert error.value.code == "RESERVED_PATH"
    assert private.read_bytes() == b"private"
    assert target.read_bytes() == b"private"


def test_generic_directory_create_refuses_reserved_precreate_spelling(
    tmp_path: Path,
) -> None:
    (tmp_path / "Knowledge Base").mkdir()

    with pytest.raises(CreateDirectoryError) as error:
        create_directory(
            tmp_path,
            path="Knowledge Base/_Consolidation/runs",
        )

    assert error.value.code == "RESERVED_PATH"
    assert not (tmp_path / "Knowledge Base" / "_Consolidation").exists()


def test_generic_directory_create_preserves_nonreserved_invalid_path_errors(
    tmp_path: Path,
) -> None:
    from exomem import create_directory

    with pytest.raises(create_directory.CreateDirectoryError) as error:
        create_directory.create_directory(tmp_path, path="../outside")

    assert error.value.code == "INVALID_PATH"


def test_generic_directory_create_refuses_symlink_parent_to_private_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import writer_lease

    governance = tmp_path / "Knowledge Base" / "_Governance"
    notes = tmp_path / "Knowledge Base" / "Notes"
    governance.mkdir(parents=True)
    notes.mkdir()
    alias = notes / "ordinary-parent"
    try:
        alias.symlink_to(governance, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("symlinks are unavailable")
    manager = SimpleNamespace(
        mutation_guard=lambda *_args, **_kwargs: nullcontext(),
        consistency_guard=lambda *_args, **_kwargs: nullcontext(),
        reserved_identity_guard=lambda *_args, **_kwargs: nullcontext(),
    )
    monkeypatch.setattr(writer_lease, "active_manager", lambda: manager)
    monkeypatch.setattr(writer_lease, "active_mutation_request_id", lambda: None)

    with pytest.raises(CreateDirectoryError) as error:
        create_directory(
            tmp_path,
            path="Knowledge Base/Notes/ordinary-parent/new-child",
        )

    assert error.value.code == "RESERVED_PATH"
    assert not (governance / "new-child").exists()


def test_generic_append_refuses_private_hardlink_before_read_or_staging(
    tmp_path: Path,
) -> None:
    governance = tmp_path / "Knowledge Base" / "_Governance"
    notes = tmp_path / "Knowledge Base" / "Notes"
    governance.mkdir(parents=True)
    notes.mkdir()
    private = governance / "policy.md"
    private.write_text("private", encoding="utf-8")
    alias = notes / "ordinary.md"
    try:
        os.link(private, alias)
    except OSError:
        pytest.skip("hard links are unavailable")

    with pytest.raises(AppendError) as error:
        append_to_file(
            tmp_path,
            path="Knowledge Base/Notes/ordinary.md",
            content="\nreplacement",
        )

    assert error.value.code == "RESERVED_PATH"
    assert private.read_text(encoding="utf-8") == "private"
    assert not list(notes.glob(".exomem-batch-*"))


def test_edit_refuses_private_hardlink_before_parse_or_staging(tmp_path: Path) -> None:
    governance = tmp_path / "Knowledge Base" / "_Governance"
    notes = tmp_path / "Knowledge Base" / "Notes"
    governance.mkdir(parents=True)
    notes.mkdir()
    private = governance / "policy.md"
    private.write_text(
        "---\ntype: policy\nstatus: active\n---\n\nprivate policy\n",
        encoding="utf-8",
    )
    alias = notes / "ordinary.md"
    try:
        os.link(private, alias)
    except OSError:
        pytest.skip("hard links are unavailable")

    with pytest.raises(EditError) as error:
        edit(
            tmp_path,
            path="Knowledge Base/Notes/ordinary.md",
            why="test reserved alias",
            new_body="replacement",
        )

    assert error.value.code == "RESERVED_PATH"
    assert "private policy" in private.read_text(encoding="utf-8")
    assert not list(notes.glob(".exomem-batch-*"))


def test_observe_refuses_private_hardlink_before_parse_or_staging(tmp_path: Path) -> None:
    governance = tmp_path / "Knowledge Base" / "_Governance"
    notes = tmp_path / "Knowledge Base" / "Notes"
    governance.mkdir(parents=True)
    notes.mkdir()
    private = governance / "policy.md"
    private.write_text(
        "---\ntype: research-note\nstatus: active\n---\n\nprivate policy\n",
        encoding="utf-8",
    )
    alias = notes / "ordinary.md"
    try:
        os.link(private, alias)
    except OSError:
        pytest.skip("hard links are unavailable")

    with pytest.raises(ObserveMemoryError) as error:
        observe_memory(
            tmp_path,
            path="Knowledge Base/Notes/ordinary.md",
            operation="add",
            category="Finding",
            content="replacement",
        )

    assert error.value.code == "RESERVED_PATH"
    assert "private policy" in private.read_text(encoding="utf-8")
    assert not list(notes.glob(".exomem-batch-*"))


def test_replace_refuses_private_hardlink_before_parse_or_planning(tmp_path: Path) -> None:
    governance = tmp_path / "Knowledge Base" / "_Governance"
    notes = tmp_path / "Knowledge Base" / "Notes"
    governance.mkdir(parents=True)
    notes.mkdir()
    private = governance / "policy.md"
    private.write_text(
        "---\ntype: insight\nstatus: active\n---\n\nprivate policy\n",
        encoding="utf-8",
    )
    alias = notes / "ordinary.md"
    try:
        os.link(private, alias)
    except OSError:
        pytest.skip("hard links are unavailable")

    with pytest.raises(ReplaceError) as error:
        replace_page(
            tmp_path,
            old_path="Knowledge Base/Notes/ordinary.md",
            content="replacement",
            note_type="insight",
            title="Replacement",
        )

    assert error.value.code == "RESERVED_PATH"
    assert "private policy" in private.read_text(encoding="utf-8")
    assert not list(notes.glob(".exomem-batch-*"))


def test_public_trash_refuses_private_hardlink_before_marker_or_destination(
    tmp_path: Path,
) -> None:
    governance = tmp_path / "Knowledge Base" / "_Governance"
    notes = tmp_path / "Knowledge Base" / "Notes"
    governance.mkdir(parents=True)
    notes.mkdir()
    policy = governance / "policy.bin"
    policy.write_bytes(b"private")
    alias = notes / "ordinary.bin"
    try:
        os.link(policy, alias)
    except OSError:
        pytest.skip("hard links are unavailable")

    with pytest.raises(DeleteFileError) as error:
        delete_file(
            tmp_path,
            path="Knowledge Base/Notes/ordinary.bin",
            confirm=True,
            force_orphan=True,
        )

    assert error.value.code == "RESERVED_PATH"
    assert alias.exists()
    assert not (tmp_path / "Knowledge Base" / "_trash").exists()
    assert not (
        governance / "deletion-tombstones"
    ).exists()


def test_public_trash_rechecks_source_identity_before_frontmatter_parse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    governance = tmp_path / "Knowledge Base" / "_Governance"
    notes = tmp_path / "Knowledge Base" / "Notes"
    governance.mkdir(parents=True)
    notes.mkdir()
    source = notes / "ordinary.md"
    source.write_text("# Ordinary\n", encoding="utf-8")
    private = governance / "policy.md"
    private.write_text(
        "---\nsuperseded_by: private-target\n---\n\nprivate policy\n",
        encoding="utf-8",
    )
    real_inspect = reserved_paths.inspect_generic_file
    exchanged = False

    def inspect_then_exchange(vault_root: Path, value: object, **kwargs):  # noqa: ANN001
        nonlocal exchanged
        identity = real_inspect(vault_root, value, **kwargs)
        if value == "Knowledge Base/Notes/ordinary.md" and not exchanged:
            exchanged = True
            source.unlink()
            os.link(private, source)
        return identity

    monkeypatch.setattr(
        reserved_paths,
        "inspect_generic_file",
        inspect_then_exchange,
    )

    with pytest.raises(DeleteFileError) as error:
        delete_file(
            tmp_path,
            path="Knowledge Base/Notes/ordinary.md",
            confirm=True,
            force_orphan=True,
        )

    assert exchanged
    assert error.value.code == "RESERVED_PATH"
    assert private.read_text(encoding="utf-8").endswith("private policy\n")
    assert not (tmp_path / "Knowledge Base" / "_trash").exists()


def test_public_trash_refuses_private_hardlink_at_generated_destination(
    tmp_path: Path,
) -> None:
    governance = tmp_path / "Knowledge Base" / "_Governance"
    notes = tmp_path / "Knowledge Base" / "Notes"
    trash = tmp_path / "Knowledge Base" / "_trash" / "2026-08-21"
    governance.mkdir(parents=True)
    notes.mkdir()
    trash.mkdir(parents=True)
    source = notes / "ordinary.bin"
    source.write_bytes(b"ordinary")
    private = governance / "policy.bin"
    private.write_bytes(b"private")
    destination = trash / "123456-Notes__ordinary.bin"
    try:
        os.link(private, destination)
    except OSError:
        pytest.skip("hard links are unavailable")

    with pytest.raises(DeleteFileError) as error:
        delete_file(
            tmp_path,
            path="Knowledge Base/Notes/ordinary.bin",
            confirm=True,
            force_orphan=True,
            now=dt.datetime(2026, 8, 21, 12, 34, 56),
        )

    assert error.value.code == "RESERVED_PATH"
    assert source.read_bytes() == b"ordinary"
    assert private.read_bytes() == b"private"
    assert destination.exists()
    assert not (trash / "123456-Notes__ordinary-2.bin").exists()


def test_public_trash_refuses_private_hardlink_at_generated_metadata_destination(
    tmp_path: Path,
) -> None:
    governance = tmp_path / "Knowledge Base" / "_Governance"
    notes = tmp_path / "Knowledge Base" / "Notes"
    trash = tmp_path / "Knowledge Base" / "_trash" / "2026-08-21"
    governance.mkdir(parents=True)
    notes.mkdir()
    trash.mkdir(parents=True)
    source = notes / "ordinary.bin"
    source.write_bytes(b"ordinary")
    private = governance / "policy.bin"
    private.write_bytes(b"private")
    metadata = trash / "123456-Notes__ordinary.bin.meta.json"
    try:
        os.link(private, metadata)
    except OSError:
        pytest.skip("hard links are unavailable")

    with pytest.raises(DeleteFileError) as error:
        delete_file(
            tmp_path,
            path="Knowledge Base/Notes/ordinary.bin",
            confirm=True,
            force_orphan=True,
            now=dt.datetime(2026, 8, 21, 12, 34, 56),
        )

    assert error.value.code == "RESERVED_PATH"
    assert source.read_bytes() == b"ordinary"
    assert private.read_bytes() == b"private"
    assert metadata.read_bytes() == b"private"
    assert not (trash / "123456-Notes__ordinary.bin").exists()


def test_recursive_trash_refuses_private_child_alias_before_count_or_marker(
    tmp_path: Path,
) -> None:
    governance = tmp_path / "Knowledge Base" / "_Governance"
    ordinary = tmp_path / "Knowledge Base" / "Notes" / "ordinary-tree"
    governance.mkdir(parents=True)
    ordinary.mkdir(parents=True)
    private = governance / "policy.bin"
    private.write_bytes(b"private")
    alias = ordinary / "ordinary.bin"
    try:
        os.link(private, alias)
    except OSError:
        pytest.skip("hard links are unavailable")

    with pytest.raises(DeleteDirectoryError) as error:
        delete_directory(
            tmp_path,
            path="Knowledge Base/Notes/ordinary-tree",
            confirm=True,
            recursive=True,
            force_orphan=True,
        )

    assert error.value.code == "RESERVED_PATH"
    assert ordinary.exists()
    assert private.read_bytes() == b"private"
    assert not (tmp_path / "Knowledge Base" / "_trash").exists()
    assert not (governance / "deletion-tombstones").exists()


def test_recursive_trash_refuses_private_alias_at_generated_destination(
    tmp_path: Path,
) -> None:
    governance = tmp_path / "Knowledge Base" / "_Governance"
    source = tmp_path / "Knowledge Base" / "Notes" / "ordinary-tree"
    trash = tmp_path / "Knowledge Base" / "_trash" / "2026-08-21"
    governance.mkdir(parents=True)
    source.mkdir(parents=True)
    trash.mkdir(parents=True)
    (source / "ordinary.bin").write_bytes(b"ordinary")
    (governance / "policy.yaml").write_text("private: true\n", encoding="utf-8")
    destination = trash / "123456-Notes__ordinary-tree"
    try:
        destination.symlink_to(governance, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("directory symlinks are unavailable")

    with pytest.raises(DeleteDirectoryError) as error:
        delete_directory(
            tmp_path,
            path="Knowledge Base/Notes/ordinary-tree",
            confirm=True,
            recursive=True,
            force_orphan=True,
            now=dt.datetime(2026, 8, 21, 12, 34, 56),
        )

    assert error.value.code == "RESERVED_PATH"
    assert (source / "ordinary.bin").read_bytes() == b"ordinary"
    assert destination.is_symlink()
    assert not (trash / "123456-Notes__ordinary-tree-2").exists()


def test_recursive_trash_refuses_private_alias_at_generated_metadata_destination(
    tmp_path: Path,
) -> None:
    governance = tmp_path / "Knowledge Base" / "_Governance"
    source = tmp_path / "Knowledge Base" / "Notes" / "ordinary-tree"
    trash = tmp_path / "Knowledge Base" / "_trash" / "2026-08-21"
    governance.mkdir(parents=True)
    source.mkdir(parents=True)
    trash.mkdir(parents=True)
    page = source / "page.md"
    page.write_text("# Ordinary\n", encoding="utf-8")
    private = governance / "policy.bin"
    private.write_bytes(b"private")
    metadata = trash / "123456-Notes__ordinary-tree.meta.json"
    try:
        os.link(private, metadata)
    except OSError:
        pytest.skip("hard links are unavailable")

    with pytest.raises(DeleteDirectoryError) as error:
        delete_directory(
            tmp_path,
            path="Knowledge Base/Notes/ordinary-tree",
            confirm=True,
            recursive=True,
            force_orphan=True,
            now=dt.datetime(2026, 8, 21, 12, 34, 56),
        )

    assert error.value.code == "RESERVED_PATH"
    assert page.read_text(encoding="utf-8") == "# Ordinary\n"
    assert private.read_bytes() == b"private"
    assert metadata.read_bytes() == b"private"
    assert not (trash / "123456-Notes__ordinary-tree").exists()


@pytest.mark.parametrize("explicit", [False, True])
def test_recovery_refuses_explicit_or_metadata_reserved_destination_before_effect(
    tmp_path: Path,
    explicit: bool,
) -> None:
    trash = tmp_path / "Knowledge Base" / "_trash" / "2026-08-21"
    trash.mkdir(parents=True)
    source = trash / "ordinary.bin"
    source.write_bytes(b"ordinary")
    sidecar = trash / "ordinary.bin.meta.json"
    sidecar.write_text(
        '{"original_path":"Knowledge Base/.embeddings.sqlite"}',
        encoding="utf-8",
    )

    with pytest.raises(RecoverError) as error:
        recover_from_trash(
            tmp_path,
            trash_path="Knowledge Base/_trash/2026-08-21/ordinary.bin",
            restore_path=(
                "Knowledge Base/.embeddings.sqlite" if explicit else None
            ),
        )

    assert error.value.code == "RESERVED_PATH"
    assert source.read_bytes() == b"ordinary"
    assert sidecar.exists()
    assert not (tmp_path / "Knowledge Base" / ".embeddings.sqlite").exists()


def test_recovery_refuses_private_hardlink_destination_before_existence_result(
    tmp_path: Path,
) -> None:
    governance = tmp_path / "Knowledge Base" / "_Governance"
    trash = tmp_path / "Knowledge Base" / "_trash" / "2026-08-21"
    notes = tmp_path / "Knowledge Base" / "Notes"
    governance.mkdir(parents=True)
    trash.mkdir(parents=True)
    notes.mkdir()
    source = trash / "ordinary.bin"
    source.write_bytes(b"ordinary")
    sidecar = trash / "ordinary.bin.meta.json"
    sidecar.write_text(
        '{"original_path":"Knowledge Base/Notes/destination.bin"}',
        encoding="utf-8",
    )
    private = governance / "policy.bin"
    private.write_bytes(b"private")
    destination = notes / "destination.bin"
    try:
        os.link(private, destination)
    except OSError:
        pytest.skip("hard links are unavailable")

    with pytest.raises(RecoverError) as error:
        recover_from_trash(
            tmp_path,
            trash_path="Knowledge Base/_trash/2026-08-21/ordinary.bin",
        )

    assert error.value.code == "RESERVED_PATH"
    assert source.read_bytes() == b"ordinary"
    assert sidecar.exists()
    assert private.read_bytes() == b"private"
    assert destination.exists()


def test_recovery_refuses_private_hardlink_source_before_sidecar_or_planning(
    tmp_path: Path,
) -> None:
    governance = tmp_path / "Knowledge Base" / "_Governance"
    trash = tmp_path / "Knowledge Base" / "_trash" / "2026-08-21"
    governance.mkdir(parents=True)
    trash.mkdir(parents=True)
    private = governance / "policy.bin"
    private.write_bytes(b"private")
    source = trash / "ordinary.bin"
    try:
        os.link(private, source)
    except OSError:
        pytest.skip("hard links are unavailable")
    (trash / "ordinary.bin.meta.json").write_text(
        '{"original_path":"Knowledge Base/Notes/restored.bin"}',
        encoding="utf-8",
    )

    with pytest.raises(RecoverError) as error:
        recover_from_trash(
            tmp_path,
            trash_path="Knowledge Base/_trash/2026-08-21/ordinary.bin",
        )

    assert error.value.code == "RESERVED_PATH"
    assert private.read_bytes() == b"private"
    assert source.exists()
    assert not (tmp_path / "Knowledge Base" / "Notes" / "restored.bin").exists()


def test_recovery_refuses_private_hardlink_sidecar_before_metadata_parse(
    tmp_path: Path,
) -> None:
    governance = tmp_path / "Knowledge Base" / "_Governance"
    trash = tmp_path / "Knowledge Base" / "_trash" / "2026-08-21"
    governance.mkdir(parents=True)
    trash.mkdir(parents=True)
    source = trash / "ordinary.bin"
    source.write_bytes(b"ordinary")
    private = governance / "recovery.json"
    private.write_text(
        '{"original_path":"Knowledge Base/Notes/restored.bin"}',
        encoding="utf-8",
    )
    sidecar = trash / "ordinary.bin.meta.json"
    try:
        os.link(private, sidecar)
    except OSError:
        pytest.skip("hard links are unavailable")

    with pytest.raises(RecoverError) as error:
        recover_from_trash(
            tmp_path,
            trash_path="Knowledge Base/_trash/2026-08-21/ordinary.bin",
        )

    assert error.value.code == "RESERVED_PATH"
    assert source.read_bytes() == b"ordinary"
    assert private.exists()
    assert sidecar.exists()
    assert not (tmp_path / "Knowledge Base" / "Notes" / "restored.bin").exists()


def test_dataset_read_hides_private_hardlink_before_parse_or_count(tmp_path: Path) -> None:
    governance = tmp_path / "Knowledge Base" / "_Governance"
    datasets = tmp_path / "Knowledge Base" / "Datasets"
    governance.mkdir(parents=True)
    datasets.mkdir()
    private = governance / "private.json"
    private.write_text('[{"secret":"do-not-release"}]', encoding="utf-8")
    alias = datasets / "ordinary.json"
    try:
        os.link(private, alias)
    except OSError:
        pytest.skip("hard links are unavailable")

    with pytest.raises(QueryDataError) as error:
        query_data(tmp_path, path="Knowledge Base/Datasets/ordinary.json")

    assert error.value.code == "NOT_FOUND"
    assert "do-not-release" not in error.value.reason


def test_directory_walk_structurally_omits_private_tree_and_hardlink_alias(
    tmp_path: Path,
) -> None:
    governance = tmp_path / "Knowledge Base" / "_Governance"
    notes = tmp_path / "Knowledge Base" / "Notes"
    governance.mkdir(parents=True)
    notes.mkdir()
    private = governance / "private.md"
    private.write_text("private", encoding="utf-8")
    ordinary = notes / "ordinary.md"
    ordinary.write_text("ordinary", encoding="utf-8")
    alias = notes / "private-alias.md"
    try:
        os.link(private, alias)
    except OSError:
        pytest.skip("hard links are unavailable")

    result = list_directory(
        tmp_path,
        path="Knowledge Base",
        recursive=True,
        include_hidden=True,
    )

    paths = {entry.path for entry in result.entries}
    assert "Knowledge Base/Notes/ordinary.md" in paths
    assert not any("_Governance" in path for path in paths)
    assert "Knowledge Base/Notes/private-alias.md" not in paths

    with pytest.raises(ListDirectoryError) as error:
        list_directory(
            tmp_path,
            path="Knowledge Base/_Governance",
            recursive=True,
            include_hidden=True,
        )
    assert error.value.code == "NOT_FOUND"


def test_list_trash_treats_private_sidecar_alias_as_physically_absent(
    tmp_path: Path,
) -> None:
    from exomem import list_trash as list_trash_module

    governance = tmp_path / "Knowledge Base" / "_Governance"
    trash = tmp_path / "Knowledge Base" / "_trash" / "2026-08-21"
    governance.mkdir(parents=True)
    trash.mkdir(parents=True)
    target = trash / "ordinary.bin"
    target.write_bytes(b"ordinary")
    before = list_trash_module.list_trash(tmp_path).as_dict()
    private = governance / "private.json"
    private.write_text(
        '{"original_path":"PRIVATE-TRASH-SENTINEL"}', encoding="utf-8"
    )
    sidecar = trash / "ordinary.bin.meta.json"
    try:
        os.link(private, sidecar)
    except OSError:
        pytest.skip("hard links are unavailable")

    after = list_trash_module.list_trash(tmp_path).as_dict()

    assert after == before
    assert "PRIVATE-TRASH-SENTINEL" not in repr(after)


def test_list_trash_treats_private_target_alias_as_physically_absent(
    tmp_path: Path,
) -> None:
    from exomem import list_trash as list_trash_module

    governance = tmp_path / "Knowledge Base" / "_Governance"
    trash = tmp_path / "Knowledge Base" / "_trash" / "2026-08-21"
    governance.mkdir(parents=True)
    trash.mkdir(parents=True)
    sidecar = trash / "ordinary.bin.meta.json"
    sidecar.write_text(
        '{"original_path":"Knowledge Base/Notes/ordinary.bin"}',
        encoding="utf-8",
    )
    before = list_trash_module.list_trash(tmp_path).as_dict()
    private = governance / "private.bin"
    private.write_bytes(b"PRIVATE-TRASH-TARGET")
    target = trash / "ordinary.bin"
    try:
        os.link(private, target)
    except OSError:
        pytest.skip("hard links are unavailable")

    after = list_trash_module.list_trash(tmp_path).as_dict()

    assert after == before
    assert "PRIVATE-TRASH-TARGET" not in repr(after)


def test_nonrecursive_list_does_not_probe_nested_aliases(tmp_path: Path) -> None:
    governance = tmp_path / "Knowledge Base" / "_Governance"
    notes = tmp_path / "Knowledge Base" / "Notes"
    governance.mkdir(parents=True)
    notes.mkdir()
    (governance / "private.md").write_text("private", encoding="utf-8")
    alias = notes / "nested-private-alias"
    try:
        alias.symlink_to(governance, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("directory symlinks are unavailable")

    result = list_directory(
        tmp_path,
        path="Knowledge Base",
        recursive=False,
        include_hidden=True,
    )

    assert [entry.path for entry in result.entries] == ["Knowledge Base/Notes"]


def test_recursive_list_omits_nested_private_symlink_alias(tmp_path: Path) -> None:
    governance = tmp_path / "Knowledge Base" / "_Governance"
    notes = tmp_path / "Knowledge Base" / "Notes"
    governance.mkdir(parents=True)
    notes.mkdir()
    (governance / "private.md").write_text("private", encoding="utf-8")
    (notes / "ordinary.md").write_text("ordinary", encoding="utf-8")
    alias = notes / "nested-private-alias"
    try:
        alias.symlink_to(governance, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("directory symlinks are unavailable")

    result = list_directory(
        tmp_path,
        path="Knowledge Base",
        recursive=True,
        include_hidden=True,
    )

    paths = {entry.path for entry in result.entries}
    assert "Knowledge Base/Notes" in paths
    assert "Knowledge Base/Notes/ordinary.md" in paths
    assert "Knowledge Base/Notes/nested-private-alias" not in paths
    assert not any("_Governance" in path for path in paths)


def test_recall_walk_and_keyword_search_omit_private_hardlink_alias(
    tmp_path: Path,
) -> None:
    governance = tmp_path / "Knowledge Base" / "_Governance"
    notes = tmp_path / "Knowledge Base" / "Notes"
    governance.mkdir(parents=True)
    notes.mkdir()
    private = governance / "private.md"
    private.write_text(
        "# Private\n\nRESERVED-SEARCH-SENTINEL-41f7\n", encoding="utf-8"
    )
    alias = notes / "ordinary.md"
    try:
        os.link(private, alias)
    except OSError:
        pytest.skip("hard links are unavailable")
    find_corpus.CACHE.clear()

    walked = set(find_corpus.walk_md(tmp_path / "Knowledge Base"))
    hits = find_module.find(
        tmp_path,
        query="RESERVED-SEARCH-SENTINEL-41f7",
        mode="keyword",
        graph=False,
        limit=5,
    )

    assert alias not in walked
    assert hits == []


@pytest.mark.parametrize(
    "private_relative",
    [
        "_Consolidation",
        ".authorization-projections/audience-v1",
        ".graph-commit-receipts",
    ],
)
def test_recall_walk_prunes_reserved_trees_before_enumerating_children(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    private_relative: str,
) -> None:
    kb = tmp_path / "Knowledge Base"
    visible = kb / "Notes" / "visible.md"
    visible.parent.mkdir(parents=True)
    visible.write_text("# Visible\n", encoding="utf-8")
    private = kb / private_relative
    private.mkdir(parents=True)
    (private / "private.md").write_text("# Private\n", encoding="utf-8")

    real_iterdir = Path.iterdir

    def guarded_iterdir(path: Path):
        if path == private:
            pytest.fail("reserved tree reached corpus child enumeration")
        return real_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", guarded_iterdir)

    assert list(find_corpus.walk_md(kb)) == [visible]


def test_inbound_index_omits_private_hardlink_alias_before_link_counts(
    tmp_path: Path,
) -> None:
    governance = tmp_path / "Knowledge Base" / "_Governance"
    notes = tmp_path / "Knowledge Base" / "Notes"
    governance.mkdir(parents=True)
    notes.mkdir()
    target = notes / "target.md"
    target.write_text("# Target\n", encoding="utf-8")
    private = governance / "private-linker.md"
    private.write_text("[[Knowledge Base/Notes/target]]\n", encoding="utf-8")
    alias = notes / "ordinary-linker.md"
    try:
        os.link(private, alias)
    except OSError:
        pytest.skip("hard links are unavailable")
    vault.clear_inbound_index()

    links = vault.find_inbound_wikilinks(
        tmp_path, "Knowledge Base/Notes/target.md"
    )

    assert links == []


def test_audit_does_not_resolve_links_through_private_hardlink_alias(
    tmp_path: Path,
) -> None:
    from exomem import audit as audit_module

    governance = tmp_path / "Knowledge Base" / "_Governance"
    notes = tmp_path / "Knowledge Base" / "Notes"
    governance.mkdir(parents=True)
    notes.mkdir()
    (notes / "source.md").write_text(
        "# Source\n\n[[ordinary-looking]]\n", encoding="utf-8"
    )
    private = governance / "private.md"
    private.write_text(
        "---\ntitle: Private Alias Title\n---\n\nprivate policy\n",
        encoding="utf-8",
    )
    alias = notes / "ordinary-looking.md"
    try:
        os.link(private, alias)
    except OSError:
        pytest.skip("hard links are unavailable")

    report = audit_module.audit(
        tmp_path, categories=["broken_wikilink", "forward_reference"]
    )

    assert any("ordinary-looking" in finding.detail for finding in report.findings)
    assert all("private.md" not in finding.detail for finding in report.findings)


def test_audit_does_not_resolve_attachment_through_private_hardlink_alias(
    tmp_path: Path,
) -> None:
    from exomem import audit as audit_module

    governance = tmp_path / "Knowledge Base" / "_Governance"
    notes = tmp_path / "Knowledge Base" / "Notes"
    governance.mkdir(parents=True)
    notes.mkdir()
    (notes / "source.md").write_text(
        "# Source\n\n[[Knowledge Base/Notes/ordinary.pdf]]\n",
        encoding="utf-8",
    )
    private = governance / "private.bin"
    private.write_bytes(b"private attachment bytes")
    alias = notes / "ordinary.pdf"
    try:
        os.link(private, alias)
    except OSError:
        pytest.skip("hard links are unavailable")

    report = audit_module.audit(tmp_path, categories=["broken_wikilink"])

    assert any("ordinary.pdf" in finding.detail for finding in report.findings)
    assert all("private attachment bytes" not in finding.detail for finding in report.findings)


def test_media_sidecar_audit_does_not_follow_private_directory_alias(
    tmp_path: Path,
) -> None:
    from exomem import audit as audit_module
    from exomem import sidecar_repair

    governance = tmp_path / "Knowledge Base" / "_Governance"
    evidence = tmp_path / "Knowledge Base" / "Evidence"
    governance.mkdir(parents=True)
    evidence.mkdir()
    (governance / "private.pdf.md").write_text(
        "# Evidence\n\n## Extracted text\n\nprivate\n\n"
        "## Preserved notes\n\n## Extracted text\n\nprivate\n",
        encoding="utf-8",
    )
    alias = evidence / "ordinary-folder"
    try:
        alias.symlink_to(governance, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("directory symlinks are unavailable")

    assert list(sidecar_repair.iter_media_sidecars(tmp_path)) == []
    assert audit_module._check_duplicated_sidecars(tmp_path) == []


def test_index_drift_audit_ignores_private_hardlink_alias(tmp_path: Path) -> None:
    from exomem import audit as audit_module

    governance = tmp_path / "Knowledge Base" / "_Governance"
    governance.mkdir(parents=True)
    private = governance / "private-counts.md"
    private.write_text("- Sources: 99\n", encoding="utf-8")
    alias = tmp_path / "Knowledge Base" / "index.md"
    try:
        os.link(private, alias)
    except OSError:
        pytest.skip("hard links are unavailable")

    assert audit_module._check_index_drift(tmp_path) == []


def test_overview_omits_private_hardlink_alias_before_counts_and_samples(
    tmp_path: Path,
) -> None:
    governance = tmp_path / "Knowledge Base" / "_Governance"
    notes = tmp_path / "Knowledge Base" / "Notes"
    governance.mkdir(parents=True)
    notes.mkdir()
    (notes / "ordinary.md").write_text("# Ordinary\n", encoding="utf-8")
    private = governance / "private.md"
    private.write_text("# Private\n\nprivate browse bytes\n", encoding="utf-8")
    before = overview(tmp_path, include_hidden=True)
    alias = notes / "private-alias.md"
    try:
        os.link(private, alias)
    except OSError:
        pytest.skip("hard links are unavailable")

    after = overview(tmp_path, include_hidden=True)

    assert after == before


def test_records_discovery_omits_private_hardlink_manifest_before_parse(
    tmp_path: Path,
) -> None:
    governance = tmp_path / "Knowledge Base" / "_Governance"
    collection = tmp_path / "Knowledge Base" / "Records" / "private-alias"
    governance.mkdir(parents=True)
    collection.mkdir(parents=True)
    private = governance / "private.md"
    private.write_text("private non-manifest bytes", encoding="utf-8")
    alias = collection / "_collection.md"
    try:
        os.link(private, alias)
    except OSError:
        pytest.skip("hard links are unavailable")

    assert structured_collections.discover_collections(tmp_path) == ()


def test_records_explicit_manifest_hides_private_hardlink_before_parse(
    tmp_path: Path,
) -> None:
    governance = tmp_path / "Knowledge Base" / "_Governance"
    collection = tmp_path / "Knowledge Base" / "Records" / "private-alias"
    governance.mkdir(parents=True)
    collection.mkdir(parents=True)
    private = governance / "private.md"
    private.write_text("private non-manifest bytes", encoding="utf-8")
    alias = collection / "_collection.md"
    try:
        os.link(private, alias)
    except OSError:
        pytest.skip("hard links are unavailable")

    missing = collection.parent / "missing" / "_collection.md"
    with pytest.raises(structured_collections.CollectionError) as missing_error:
        structured_collections.load_manifest(tmp_path, missing)
    with pytest.raises(structured_collections.CollectionError) as error:
        structured_collections.load_manifest(tmp_path, alias)

    assert (error.value.code, error.value.reason) == (
        missing_error.value.code,
        missing_error.value.reason,
    )
    assert "private non-manifest bytes" not in error.value.reason


def test_video_frames_hides_private_hardlink_before_media_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    governance = tmp_path / "Knowledge Base" / "_Governance"
    evidence = tmp_path / "Knowledge Base" / "Evidence"
    governance.mkdir(parents=True)
    evidence.mkdir()
    private = governance / "private.bin"
    private.write_bytes(b"private media-shaped bytes")
    alias = evidence / "ordinary.mp4"
    try:
        os.link(private, alias)
    except OSError:
        pytest.skip("hard links are unavailable")
    monkeypatch.setattr(
        video_frames,
        "_probe_duration",
        lambda _source: pytest.fail("private aliases must be refused before media probing"),
    )

    with pytest.raises(video_frames.VideoFramesError) as error:
        video_frames.get_frames(tmp_path, alias.relative_to(tmp_path).as_posix())

    assert error.value.code == "NOT_FOUND"
    assert "private media-shaped bytes" not in error.value.reason


def test_media_reconciliation_hides_private_hardlink_before_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    governance = tmp_path / "Knowledge Base" / "_Governance"
    evidence = tmp_path / "Knowledge Base" / "Evidence"
    governance.mkdir(parents=True)
    evidence.mkdir()
    private = governance / "private.bin"
    private.write_bytes(b"private media-shaped bytes")
    alias = evidence / "ordinary.mp4"
    try:
        os.link(private, alias)
    except OSError:
        pytest.skip("hard links are unavailable")
    monkeypatch.setattr(
        media_processing,
        "_read_provenance",
        lambda *_args, **_kwargs: pytest.fail(
            "private aliases must be refused before provenance"
        ),
    )

    with pytest.raises(media_processing.MediaProcessingError) as error:
        media_processing.reconcile_media(tmp_path, alias, explicit=True)

    assert error.value.code == "MEDIA_NOT_FOUND"
    assert "private media-shaped bytes" not in error.value.reason


def test_media_reconciliation_hides_private_sidecar_alias_before_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    governance = tmp_path / "Knowledge Base" / "_Governance"
    evidence = tmp_path / "Knowledge Base" / "Evidence"
    governance.mkdir(parents=True)
    evidence.mkdir()
    binary = evidence / "ordinary.mp4"
    binary.write_bytes(b"ordinary media bytes")
    private = governance / "private.md"
    private.write_text("private sidecar bytes", encoding="utf-8")
    sidecar = binary.with_name(f"{binary.name}.md")
    try:
        os.link(private, sidecar)
    except OSError:
        pytest.skip("hard links are unavailable")
    monkeypatch.setattr(
        media_processing,
        "_read_provenance",
        lambda *_args, **_kwargs: pytest.fail(
            "private sidecar aliases must be refused before provenance"
        ),
    )

    with pytest.raises(media_processing.MediaProcessingError) as error:
        media_processing.reconcile_media(tmp_path, binary, explicit=True)

    assert error.value.code == "MEDIA_NOT_FOUND"
    assert "private sidecar bytes" not in error.value.reason


def test_process_media_private_alias_matches_absent_before_existence_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.cli_ops import OpError

    governance = tmp_path / "Knowledge Base" / "_Governance"
    evidence = tmp_path / "Knowledge Base" / "Evidence"
    governance.mkdir(parents=True)
    evidence.mkdir()
    relative = "Knowledge Base/Evidence/ordinary.mp4"

    class Manager:
        def consistency_guard(self, *_args: object, **_kwargs: object):
            return nullcontext()

        def reserved_identity_guard(self, *_args: object, **_kwargs: object):
            return nullcontext()

        def mutation_guard(self, *_args: object, **_kwargs: object):
            pytest.fail("a refused media probe must not acquire the mutation guard")

    monkeypatch.setattr(writer_lease, "active_manager", lambda: Manager())

    with pytest.raises(OpError) as absent:
        commands.op_process_media(tmp_path, path=relative, operation="process")

    private = governance / "private.bin"
    private.write_bytes(b"private media-shaped bytes")
    alias = tmp_path / relative
    try:
        os.link(private, alias)
    except OSError:
        pytest.skip("hard links are unavailable")

    with pytest.raises(OpError) as present:
        commands.op_process_media(tmp_path, path=relative, operation="process")

    assert present.value.as_public_dict() == absent.value.as_public_dict()
    assert "private media-shaped bytes" not in present.value.message


def test_dispatcher_injects_nonserializable_governance_owner_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = next(
        item for item in commands.PRODUCT_COMMANDS if item.name == "govern_memory"
    )
    observed: list[object] = []

    def leaf(_vault_root: Path, **_kwargs: object) -> dict[str, bool]:
        authority = reserved_paths._active_owner_authority()
        observed.append(authority)
        assert reserved_paths.owner_authorized("governance-tree")
        assert not reserved_paths.owner_authorized("governance-store")
        assert not reserved_paths.owner_authorized("consolidation-tree")
        with pytest.raises((pickle.PickleError, TypeError)):
            pickle.dumps(authority)
        return {"authorized": True}

    dispatch = dataclass_replace(command, leaf=leaf)

    class Manager:
        def invoke(
            self,
            current,
            injected,
            kwargs,
            **_dispatch_kwargs,
        ):
            return current.leaf(*injected, **kwargs)

    monkeypatch.setattr(writer_lease, "get_manager", lambda: Manager())

    result = writer_lease.invoke_command(
        dispatch,
        tmp_path,
        operation="list",
    )

    assert result == {"authorized": True}
    assert len(observed) == 1
    assert reserved_paths._active_owner_authority() is None


def test_future_consolidation_command_has_no_fallback_owner_authority() -> None:
    with reserved_paths._owner_authority_scope("consolidate_memory"):
        assert reserved_paths._active_owner_authority() is None
        assert not reserved_paths.owner_authorized("consolidation-tree")


def test_named_subsystem_authority_is_exact_and_nonserializable() -> None:
    with reserved_paths._subsystem_authority_scope("governance.store"):
        authority = reserved_paths._active_owner_authority()
        assert reserved_paths.owner_authorized("governance-store")
        assert not reserved_paths.owner_authorized("governance-tree")
        assert not reserved_paths.owner_authorized("embeddings-store")
        with pytest.raises((pickle.PickleError, TypeError)):
            pickle.dumps(authority)

    assert reserved_paths._active_owner_authority() is None
    for inactive_owner in (
        "consolidation.future",
        "references.legacy",
        "freshness.legacy",
        "governance.projections",
    ):
        with reserved_paths._subsystem_authority_scope(inactive_owner):
            assert reserved_paths._active_owner_authority() is None


def test_owner_byte_publication_requires_exact_named_authority(
    tmp_path: Path,
) -> None:
    target = tmp_path / "Knowledge Base" / ".voice_profiles.json"

    with pytest.raises(RuntimeError, match="authority"):
        reserved_paths._publish_owner_bytes(
            tmp_path,
            target,
            "voice-profile-store",
            b"{}\n",
        )

    with reserved_paths._subsystem_authority_scope("claims"):
        with pytest.raises(RuntimeError, match="authority"):
            reserved_paths._publish_owner_bytes(
                tmp_path,
                target,
                "voice-profile-store",
                b"{}\n",
            )


@pytest.mark.parametrize(
    ("owner", "descriptor_id", "relative"),
    [
        ("graph_sync", "graph-handoff", "Knowledge Base/.graph-sync.json"),
        ("graph_sync", "graph-handoff", "Knowledge Base/.graph-sync-floor.json"),
        (
            "graph_sync",
            "graph-receipts",
            "Knowledge Base/.graph-commit-receipts/" + "a" * 24 + ".json",
        ),
        ("review_state", "review-state", "Knowledge Base/.review-state.json"),
        (
            "review_state",
            "review-state",
            "Knowledge Base/..review-state.json.abc123_4.tmp",
        ),
    ],
)
def test_owner_publication_refuses_parent_exchange_after_precreate_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    owner: str,
    descriptor_id: str,
    relative: str,
) -> None:
    from exomem import held_fs

    target = tmp_path / relative
    target.parent.mkdir(parents=True)
    acquired = held_fs.acquire(tmp_path)
    assert acquired.ok
    filesystem_type = type(acquired.require())
    acquired.require().close()
    real_file = filesystem_type.file
    probed_missing = threading.Event()
    release = threading.Event()
    paused = False

    def pause_after_missing_probe(self, parent, leaf, **kwargs):  # noqa: ANN001
        nonlocal paused
        result = real_file(self, parent, leaf, **kwargs)
        if (
            leaf == target.name
            and not result.ok
            and result.error is not None
            and result.error.code == "MISSING"
            and not paused
        ):
            paused = True
            probed_missing.set()
            assert release.wait(5)
        return result

    monkeypatch.setattr(filesystem_type, "file", pause_after_missing_probe)
    outcomes: list[object] = []

    def publish() -> None:
        try:
            with reserved_paths._subsystem_authority_scope(owner):
                outcomes.append(
                    reserved_paths._publish_owner_bytes(
                        tmp_path,
                        target,
                        descriptor_id,
                        b"private control state",
                    )
                )
        except BaseException as error:  # noqa: BLE001 - thread outcome assertion
            outcomes.append(error)

    worker = threading.Thread(target=publish, daemon=True)
    worker.start()
    assert probed_missing.wait(5)
    displaced_parent = target.parent.with_name(f"{target.parent.name}-displaced")
    target.parent.rename(displaced_parent)
    target.parent.mkdir()
    release.set()
    worker.join(5)

    assert not worker.is_alive()
    assert len(outcomes) == 1
    assert isinstance(outcomes[0], RuntimeError)
    assert "parent changed" in str(outcomes[0])
    assert not target.exists()
    assert not (displaced_parent / target.name).exists()


def test_json_and_graph_private_owners_publish_through_their_named_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import graph_sync, review_state, voice_profiles

    observed: list[tuple[str, str]] = []

    def observe(
        vault_root: Path,
        path: Path,
        descriptor_id: str,
        data: bytes,
    ) -> object:
        del data
        assert Path(vault_root) == tmp_path
        assert reserved_paths.owner_authorized(descriptor_id)
        observed.append((descriptor_id, path.name))
        return SimpleNamespace()

    monkeypatch.setattr(
        reserved_paths,
        "_publish_owner_bytes",
        observe,
        raising=False,
    )

    review_state.ReviewStateStore(tmp_path)._write(
        {"version": review_state.SCHEMA_VERSION, "records": {}}
    )
    voice_profiles._write_store(
        voice_profiles.voice_profiles_path(tmp_path),
        {},
    )
    graph_sync._write_floor(
        tmp_path,
        graph_sync.GraphSyncGenerationFloor.create(1),
    )
    checkpoint = graph_sync.next_checkpoint(
        current=None,
        acknowledged_generation=0,
        mutation_id="1" * 24,
        paths=[],
        created_paths=[],
        force_full_scope=True,
    )
    graph_sync._write_checkpoint(tmp_path, checkpoint)
    receipt = graph_sync.GraphCommitReceipt.create(
        idempotency_key_digest="2" * 64,
        command_digest="3" * 64,
        attempt_id="4" * 24,
        commit_token="5" * 24,
        canonical_disposition="success",
        terminal_projection={"status": "committed"},
        commit_secret=b"6" * 32,
    )
    graph_sync.write_graph_commit_receipt(tmp_path, receipt)

    assert observed == [
        ("review-state", ".review-state.json"),
        ("voice-profile-store", ".voice_profiles.json"),
        ("graph-handoff", ".graph-sync-floor.json"),
        ("graph-handoff", ".graph-sync.json"),
        ("graph-receipts", f"{receipt.commit_token}.json"),
    ]


def test_graph_epoch_restore_uses_exact_handoff_owner_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import graph_sync

    published: list[tuple[str, str, bytes]] = []
    removed: list[tuple[str, str, bool]] = []

    monkeypatch.setattr(
        vault,
        "batch_atomic_write",
        lambda *_args, **_kwargs: pytest.fail(
            "graph handoff restore reached the generic batch writer"
        ),
    )

    def publish(
        vault_root: Path,
        path: Path,
        descriptor_id: str,
        data: bytes,
    ) -> object:
        assert Path(vault_root) == tmp_path
        assert reserved_paths.owner_authorized(descriptor_id)
        published.append((descriptor_id, path.name, data))
        return SimpleNamespace()

    def remove(
        vault_root: Path,
        path: Path,
        descriptor_id: str,
        *,
        missing_ok: bool = False,
    ) -> bool:
        assert Path(vault_root) == tmp_path
        assert reserved_paths.owner_authorized(descriptor_id)
        removed.append((descriptor_id, path.name, missing_ok))
        return False

    monkeypatch.setattr(reserved_paths, "_publish_owner_bytes", publish)
    monkeypatch.setattr(reserved_paths, "_remove_owner_file", remove)

    graph_sync._restore_epoch_artifacts(
        tmp_path,
        prior_floor_bytes=b"prior floor",
        prior_checkpoint_bytes=None,
    )

    assert published == [
        ("graph-handoff", ".graph-sync-floor.json", b"prior floor")
    ]
    assert removed == [
        ("graph-handoff", ".graph-sync.json", True)
    ]


def test_graph_sidecar_publication_uses_exact_graph_owner_move(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import epistemic_graph, graph_sync

    live = epistemic_graph.sidecar_path(tmp_path)
    live.parent.mkdir(parents=True)
    live.write_bytes(b"prior")
    temporary = graph_sync.temporary_sidecar_path(
        live,
        SimpleNamespace(checkpoint_sha256="a" * 64),
    )
    temporary.write_bytes(b"replacement")
    observed: list[tuple[str, str, str, str, bool]] = []

    def move(
        vault_root: Path,
        source: Path,
        source_descriptor_id: str,
        destination: Path,
        destination_descriptor_id: str,
        *,
        replace: bool,
    ) -> object:
        assert Path(vault_root) == tmp_path
        assert reserved_paths.owner_authorized(source_descriptor_id)
        assert reserved_paths.owner_authorized(destination_descriptor_id)
        observed.append(
            (
                source.name,
                source_descriptor_id,
                destination.name,
                destination_descriptor_id,
                replace,
            )
        )
        source.replace(destination)
        return SimpleNamespace()

    monkeypatch.setattr(reserved_paths, "_move_owner_file", move)

    graph_sync.replace_sidecar(temporary, live, vault_root=tmp_path)

    assert observed == [
        (
            temporary.name,
            "graph-rebuild",
            ".graph.sqlite",
            "graph-store",
            True,
        )
    ]
    assert live.read_bytes() == b"replacement"


def test_graph_and_lexical_reapers_use_their_exact_owner_removers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import epistemic_graph, graph_sync, lexstore

    live = epistemic_graph.sidecar_path(tmp_path)
    live.parent.mkdir(parents=True)
    graph_temporary = graph_sync.temporary_sidecar_path(
        live,
        SimpleNamespace(checkpoint_sha256="b" * 64),
    )
    graph_temporary.write_bytes(b"graph")
    lexical_temporary = live.with_name(
        ".lexical.sqlite.rebuild-0123456789abcdef0123456789abcdef.tmp"
    )
    lexical_temporary.write_bytes(b"lexical")
    os.utime(lexical_temporary, (1, 1))
    observed: list[tuple[str, str]] = []

    def remove_graph(vault_root: Path, path: Path, *, missing_ok: bool) -> bool:
        assert Path(vault_root) == tmp_path
        observed.append(("graph-rebuild", path.name))
        path.unlink(missing_ok=missing_ok)
        return True

    def remove_lexical(vault_root: Path, path: Path, *, missing_ok: bool) -> bool:
        assert Path(vault_root) == tmp_path
        observed.append(("lexical-rebuild", path.name))
        path.unlink(missing_ok=missing_ok)
        return True

    monkeypatch.setattr(
        epistemic_graph,
        "_remove_graph_rebuild_artifact",
        remove_graph,
        raising=False,
    )
    monkeypatch.setattr(
        lexstore,
        "_remove_lexical_rebuild_artifact",
        remove_lexical,
        raising=False,
    )

    removed = graph_sync.sweep_abandoned_temporaries(
        tmp_path,
        live,
        live_paths=set(),
    )

    assert set(removed) == {graph_temporary, lexical_temporary}
    assert set(observed) == {
        ("graph-rebuild", graph_temporary.name),
        ("lexical-rebuild", lexical_temporary.name),
    }


def test_json_and_graph_private_owners_read_through_their_named_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import graph_sync, review_state, voice_profiles

    observed: list[tuple[str, str, int]] = []

    def observe(
        vault_root: Path,
        path: Path,
        descriptor_id: str,
        *,
        limit: int,
    ) -> bytes:
        assert Path(vault_root) == tmp_path
        assert reserved_paths.owner_authorized(descriptor_id)
        observed.append((descriptor_id, path.name, limit))
        if descriptor_id == "review-state":
            return b'{"records":{},"version":1}'
        if descriptor_id == "voice-profile-store":
            return b"{}"
        return graph_sync.GraphSyncGenerationFloor.create(1).render().encode("utf-8")

    monkeypatch.setattr(
        reserved_paths,
        "_read_owner_bytes",
        observe,
        raising=False,
    )
    monkeypatch.setattr(Path, "exists", lambda _path: True)

    assert review_state.ReviewStateStore(tmp_path).load()["records"] == {}
    assert voice_profiles.load_store(voice_profiles.voice_profiles_path(tmp_path)) == {}
    assert graph_sync.read_floor(tmp_path) is not None

    assert [(descriptor, name) for descriptor, name, _limit in observed] == [
        ("review-state", ".review-state.json"),
        ("voice-profile-store", ".voice_profiles.json"),
        ("graph-handoff", ".graph-sync-floor.json"),
    ]


def test_owner_byte_read_requires_exact_named_authority(tmp_path: Path) -> None:
    target = tmp_path / "Knowledge Base" / ".voice_profiles.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"{}")

    with pytest.raises(RuntimeError, match="authority"):
        reserved_paths._read_owner_bytes(
            tmp_path,
            target,
            "voice-profile-store",
            limit=64,
        )

    with reserved_paths._subsystem_authority_scope("claims"):
        with pytest.raises(RuntimeError, match="authority"):
            reserved_paths._read_owner_bytes(
                tmp_path,
                target,
                "voice-profile-store",
                limit=64,
            )


def test_owner_move_and_remove_require_exact_named_authority(tmp_path: Path) -> None:
    kb = tmp_path / "Knowledge Base"
    kb.mkdir()
    token = "7" * 32
    staged = kb / f".lexical.sqlite.rebuild-{token}.tmp"
    live = kb / ".lexical.sqlite"
    staged.write_bytes(b"target generation")
    live.write_bytes(b"prior generation")

    with pytest.raises(RuntimeError, match="authority"):
        reserved_paths._move_owner_file(
            tmp_path,
            staged,
            "lexical-rebuild",
            live,
            "lexical-store",
            replace=True,
        )

    with reserved_paths._subsystem_authority_scope("lexstore"):
        installed = reserved_paths._move_owner_file(
            tmp_path,
            staged,
            "lexical-rebuild",
            live,
            "lexical-store",
            replace=True,
        )
        assert installed.kind == "file"
        assert reserved_paths._remove_owner_file(
            tmp_path,
            live,
            "lexical-store",
        )

    assert not staged.exists()
    assert not live.exists()


def test_lexical_publication_refuses_parent_exchange_after_destination_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import held_fs

    kb = tmp_path / "Knowledge Base"
    kb.mkdir()
    token = "8" * 32
    staged = kb / f".lexical.sqlite.rebuild-{token}.tmp"
    live = kb / ".lexical.sqlite"
    staged.write_bytes(b"target generation")
    live.write_bytes(b"prior generation")

    acquired = held_fs.acquire(tmp_path)
    assert acquired.ok
    filesystem_type = type(acquired.require())
    acquired.require().close()
    real_file = filesystem_type.file
    destination_probed = threading.Event()
    release = threading.Event()
    paused = False

    def pause_after_destination_probe(self, parent, leaf, **kwargs):  # noqa: ANN001
        nonlocal paused
        result = real_file(self, parent, leaf, **kwargs)
        if leaf == live.name and result.ok and not paused:
            paused = True
            destination_probed.set()
            assert release.wait(5)
        return result

    monkeypatch.setattr(filesystem_type, "file", pause_after_destination_probe)
    outcomes: list[object] = []

    def publish() -> None:
        try:
            with reserved_paths._subsystem_authority_scope("lexstore"):
                outcomes.append(
                    reserved_paths._move_owner_file(
                        tmp_path,
                        staged,
                        "lexical-rebuild",
                        live,
                        "lexical-store",
                        replace=True,
                    )
                )
        except BaseException as error:  # noqa: BLE001 - thread outcome assertion
            outcomes.append(error)

    worker = threading.Thread(target=publish, daemon=True)
    worker.start()
    assert destination_probed.wait(5)
    displaced = tmp_path / "Knowledge Base-displaced"
    exchange_blocked = False
    try:
        kb.rename(displaced)
    except PermissionError:
        exchange_blocked = True
    else:
        kb.mkdir()
    finally:
        release.set()
    worker.join(5)

    assert not worker.is_alive()
    assert len(outcomes) == 1
    if exchange_blocked:
        assert isinstance(outcomes[0], held_fs.StableIdentity)
        assert outcomes[0].kind == "file"
        assert live.read_bytes() == b"target generation"
        assert not staged.exists()
        assert not displaced.exists()
        return
    assert isinstance(outcomes[0], OSError)
    assert "parent changed" in str(outcomes[0])
    assert (displaced / staged.name).read_bytes() == b"target generation"
    assert (displaced / live.name).read_bytes() == b"prior generation"
    assert not (kb / staged.name).exists()
    assert not (kb / live.name).exists()


def test_lexical_quarantine_lifecycle_uses_exact_owner_file_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import lexstore

    store = lexstore.LexicalStore(tmp_path)
    store.path.parent.mkdir(parents=True)
    members = (store.path, *store._wal_shm_paths(store.path))
    for index, member in enumerate(members):
        member.write_bytes(f"generation-{index}".encode())

    moves: list[tuple[str, str, str, str]] = []
    removals: list[tuple[str, str]] = []

    def move(
        vault_root: Path,
        source: Path,
        source_descriptor_id: str,
        destination: Path,
        destination_descriptor_id: str,
        *,
        replace: bool,
    ) -> object:
        assert Path(vault_root) == tmp_path
        assert reserved_paths.owner_authorized(source_descriptor_id)
        assert reserved_paths.owner_authorized(destination_descriptor_id)
        assert replace is False
        moves.append(
            (
                source.name,
                source_descriptor_id,
                destination.name,
                destination_descriptor_id,
            )
        )
        source.replace(destination)
        return SimpleNamespace()

    def remove(
        vault_root: Path,
        path: Path,
        descriptor_id: str,
        *,
        missing_ok: bool = False,
    ) -> bool:
        assert Path(vault_root) == tmp_path
        assert reserved_paths.owner_authorized(descriptor_id)
        assert missing_ok
        removals.append((path.name, descriptor_id))
        path.unlink(missing_ok=True)
        return True

    monkeypatch.setattr(reserved_paths, "_move_owner_file", move)
    monkeypatch.setattr(reserved_paths, "_remove_owner_file", remove)

    quarantined = store._quarantine_live_set()
    assert quarantined is not None
    store._restore_quarantined_set(quarantined)
    quarantined = store._quarantine_live_set()
    assert quarantined is not None
    store._discard_quarantined_set(quarantined)

    assert len(moves) == 9
    assert all(
        {source_descriptor, destination_descriptor}
        == {"lexical-store", "lexical-quarantine"}
        for _source, source_descriptor, _destination, destination_descriptor in moves
    )
    assert len(removals) == 3
    assert all(descriptor == "lexical-quarantine" for _path, descriptor in removals)


def test_governance_store_acquires_only_its_named_subsystem_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import store

    real_sidecar_path = store.sidecar_path

    def guarded_sidecar_path(vault_root: Path) -> Path:
        assert reserved_paths.owner_authorized("governance-store")
        assert not reserved_paths.owner_authorized("governance-tree")
        return real_sidecar_path(vault_root)

    monkeypatch.setattr(store, "sidecar_path", guarded_sidecar_path)

    store.open_connection(tmp_path).close()

    assert reserved_paths._active_owner_authority() is None


def test_sqlite_index_connections_acquire_only_their_named_subsystem_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import (
        claims,
        clip_index,
        deferred_index,
        embedding_index,
        epistemic_graph,
        lexstore,
        media_jobs,
        memory_refs,
    )
    graph = epistemic_graph.EpistemicGraphIndex(
        tmp_path,
        mutation_coordinator=SimpleNamespace(),
    )
    cases = (
        ("embedding_index", embedding_index, embedding_index.EmbeddingIndex(tmp_path)._connect),
        ("clip_index", clip_index, clip_index.ClipIndex(tmp_path)._connect),
        ("lexstore", lexstore, lexstore.LexicalStore(tmp_path)._connect),
        ("claims", claims, claims.ClaimIndex(tmp_path)._connect),
        ("epistemic_graph", epistemic_graph, graph._connect),
        (
            "deferred_index",
            deferred_index,
            lambda: deferred_index._connect(tmp_path, create=True),
        ),
        (
            "media_jobs",
            media_jobs,
            media_jobs.MediaJobStore(tmp_path, create=False)._connect,
        ),
        ("memory_refs", memory_refs, memory_refs.ReferenceIndex(tmp_path)._connect),
    )

    class ConnectionObserved(Exception):
        pass

    for owner, module, connect in cases:
        with monkeypatch.context() as scoped:
            def observe_connect(
                *_args: object,
                expected_owner: str = owner,
                **_kwargs: object,
            ):
                assert reserved_paths._active_owner_authority() is not None
                owned = {
                    descriptor.id
                    for descriptor in reserved_paths.internal_state_registry()
                    if descriptor.owner == expected_owner
                    and descriptor.authority_enabled
                }
                assert owned
                assert all(reserved_paths.owner_authorized(item) for item in owned)
                assert not any(
                    reserved_paths.owner_authorized(descriptor.id)
                    for descriptor in reserved_paths.internal_state_registry()
                    if descriptor.id not in owned
                )
                raise ConnectionObserved

            scoped.setattr(module.sqlite3, "connect", observe_connect)
            with pytest.raises(ConnectionObserved):
                connect()
        assert reserved_paths._active_owner_authority() is None


def test_primary_sqlite_connections_retain_their_owner_target_through_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import (
        claims,
        clip_index,
        deferred_index,
        embedding_index,
        epistemic_graph,
        lexstore,
        media_jobs,
        memory_refs,
    )
    from exomem.governance import store as governance_store

    observed: list[tuple[str, str, bool]] = []
    published: list[str] = []
    retained: set[str] = set()

    @contextmanager
    def observe_scope(
        vault_root: Path,
        database: Path,
        descriptor_id: str,
        *,
        create: bool,
    ):
        assert reserved_paths.owner_authorized(descriptor_id)
        assert reserved_paths._identity_coordination_active(vault_root)
        observed.append((descriptor_id, Path(database).name, create))
        retained.add(descriptor_id)
        try:
            yield Path(database)
        finally:
            retained.remove(descriptor_id)

    def observe_publication(
        _vault_root: Path,
        _database: Path,
        descriptor_id: str,
        _connection: object,
    ) -> None:
        assert descriptor_id in retained
        published.append(descriptor_id)

    monkeypatch.setattr(
        reserved_paths,
        "_sqlite_owner_target_scope",
        observe_scope,
        raising=False,
    )
    monkeypatch.setattr(
        reserved_paths,
        "_publish_sqlite_owner_family",
        observe_publication,
    )

    graph = epistemic_graph.EpistemicGraphIndex(
        tmp_path,
        mutation_coordinator=SimpleNamespace(),
    )
    cases = (
        ("embeddings-store", lambda: embedding_index.EmbeddingIndex(tmp_path)._connect()),
        ("clip-store", lambda: clip_index.ClipIndex(tmp_path)._connect()),
        ("claims-store", lambda: claims.ClaimIndex(tmp_path)._connect()),
        ("graph-store", graph._connect),
        (
            "deferred-index-store",
            lambda: deferred_index._connect(tmp_path, create=True),
        ),
        ("refs-store", lambda: memory_refs.ReferenceIndex(tmp_path)._connect()),
        ("lexical-store", lambda: lexstore.LexicalStore(tmp_path)._connect_setup()),
        (
            "media-jobs-store",
            lambda: media_jobs.MediaJobStore(tmp_path, create=False)._connect(),
        ),
        ("governance-store", lambda: governance_store.open_connection(tmp_path)),
    )

    for _descriptor_id, connect in cases:
        connection = connect()
        connection.close()

    assert [descriptor_id for descriptor_id, _name, _create in observed] == [
        descriptor_id for descriptor_id, _connect in cases
    ]
    assert all(create for _descriptor_id, _name, create in observed)
    assert published == [descriptor_id for descriptor_id, _connect in cases]


def test_sqlite_owner_target_scope_rejects_symlink_and_hardlink_aliases(
    tmp_path: Path,
) -> None:
    scope_factory = getattr(reserved_paths, "_sqlite_owner_target_scope", None)
    assert scope_factory is not None

    kb = tmp_path / "Knowledge Base"
    kb.mkdir()
    ordinary = kb / "ordinary.sqlite"
    ordinary.write_bytes(b"ordinary")
    private = kb / ".embeddings.sqlite"

    try:
        private.symlink_to(ordinary.name)
    except (NotImplementedError, OSError):
        pytest.skip("symlinks are unavailable")
    with reserved_paths._subsystem_authority_scope("embedding_index"):
        with reserved_paths._identity_coordination_scope(tmp_path):
            with pytest.raises(RuntimeError, match="unsafe|ambiguous"):
                with scope_factory(
                    tmp_path,
                    private,
                    "embeddings-store",
                    create=True,
                ):
                    pytest.fail("unsafe SQLite alias reached the connection leaf")
    private.unlink()

    try:
        os.link(ordinary, private)
    except OSError:
        pytest.skip("hard links are unavailable")
    with reserved_paths._subsystem_authority_scope("embedding_index"):
        with reserved_paths._identity_coordination_scope(tmp_path):
            with pytest.raises(RuntimeError, match="unsafe|ambiguous"):
                with scope_factory(
                    tmp_path,
                    private,
                    "embeddings-store",
                    create=True,
                ):
                    pytest.fail("ambiguous SQLite alias reached the connection leaf")


def test_sqlite_owner_target_scope_retains_identity_without_delete_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import held_fs

    database = tmp_path / "Knowledge Base" / ".embeddings.sqlite"
    database.parent.mkdir()
    database.write_bytes(b"existing")
    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=tmp_path / "state")
    )
    monkeypatch.setattr(writer_lease, "active_manager", lambda: manager)
    acquired = held_fs.acquire(tmp_path)
    assert acquired.ok
    filesystem_type = type(acquired.require())
    acquired.require().close()
    real_parent = filesystem_type.parent
    real_file = filesystem_type.file
    parent_accesses: list[str] = []
    accesses: list[str] = []

    def observe_parent_access(self, relative, **kwargs):  # noqa: ANN001
        parent_accesses.append(kwargs.get("access", "read"))
        return real_parent(self, relative, **kwargs)

    def observe_access(self, parent, leaf, **kwargs):  # noqa: ANN001
        if leaf == database.name:
            accesses.append(kwargs.get("access", "read"))
        return real_file(self, parent, leaf, **kwargs)

    monkeypatch.setattr(filesystem_type, "parent", observe_parent_access)
    monkeypatch.setattr(filesystem_type, "file", observe_access)

    with reserved_paths._subsystem_authority_scope("embedding_index"):
        with reserved_paths._identity_coordination_scope(tmp_path):
            with reserved_paths._sqlite_owner_target_scope(
                tmp_path,
                database,
                "embeddings-store",
                create=True,
            ) as retained:
                assert retained == database

    assert parent_accesses == ["read"]
    assert accesses == ["read", "read"]


def test_readonly_sqlite_connections_retain_their_owner_target_through_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import deferred_index, media_jobs
    from exomem.governance import store as governance_store

    created = (
        governance_store.open_connection(tmp_path),
        deferred_index._connect(tmp_path, create=True),
    )
    for connection in created:
        connection.close()
    media_jobs.MediaJobStore(tmp_path)

    observed: list[tuple[str, bool]] = []

    @contextmanager
    def observe_scope(
        vault_root: Path,
        database: Path,
        descriptor_id: str,
        *,
        create: bool,
    ):
        assert reserved_paths.owner_authorized(descriptor_id)
        assert reserved_paths._identity_coordination_active(vault_root)
        observed.append((descriptor_id, create))
        yield Path(database)

    monkeypatch.setattr(
        reserved_paths,
        "_sqlite_owner_target_scope",
        observe_scope,
    )

    readonly = (
        governance_store.open_readonly_connection(tmp_path),
        deferred_index._connect_readonly(tmp_path),
        media_jobs.MediaJobStore(tmp_path, create=False)._connect(readonly=True),
    )
    for connection in readonly:
        assert connection is not None
        connection.close()

    assert observed == [
        ("governance-store", False),
        ("deferred-index-store", False),
        ("media-jobs-store", False),
    ]


def test_private_sqlite_read_probes_retain_their_named_owner_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import (
        claims,
        deferred_index,
        epistemic_graph,
        graph_sync,
        lexstore,
        media_jobs,
        memory_refs,
    )
    from exomem.governance import store as governance_store

    references = memory_refs.ReferenceIndex(tmp_path)
    references_connection = references._connect()
    references_connection.close()
    claims_index = claims.ClaimIndex(tmp_path)
    claims_connection = claims_index._connect()
    claims_connection.close()
    deferred_connection = deferred_index._connect(tmp_path, create=True)
    deferred_connection.close()
    graph = epistemic_graph.EpistemicGraphIndex(
        tmp_path,
        mutation_coordinator=SimpleNamespace(),
    )
    graph_connection = graph._connect()
    graph_connection.close()
    lexical = lexstore.LexicalStore(tmp_path)
    lexical_connection = lexical._connect_setup()
    lexical_connection.close()
    media = media_jobs.MediaJobStore(tmp_path)
    governance_connection = governance_store.open_connection(tmp_path)
    governance_connection.close()

    observed: list[str] = []

    @contextmanager
    def observe_scope(
        vault_root: Path,
        database: Path,
        descriptor_id: str,
        *,
        create: bool,
    ):
        assert not create
        assert reserved_paths.owner_authorized(descriptor_id)
        assert reserved_paths._identity_coordination_active(vault_root)
        observed.append(descriptor_id)
        yield Path(database)

    monkeypatch.setattr(
        reserved_paths,
        "_sqlite_owner_target_scope",
        observe_scope,
    )

    assert references.available() is False
    assert claims_index._recall_identity_current() is False
    assert deferred_index.semantic_isolation_signature(tmp_path) == "semantic:0"
    assert graph.reads_suspended() is False
    observational = lexical._connect_observational_main()
    observational.close()
    media_jobs._diagnostic_snapshot_rows(media.path)
    assert governance_store.guard_generation_probe(tmp_path)["state"] == "clear"
    assert graph_sync.acknowledgement_state(tmp_path)[0] == "absent"

    assert observed == [
        "refs-store",
        "claims-store",
        "deferred-index-store",
        "graph-store",
        "lexical-store",
        "media-jobs-store",
        "governance-store",
        "graph-store",
    ]


def test_reference_drift_reports_a_late_retained_target_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import memory_refs

    sidecar = tmp_path / "Knowledge Base" / ".refs.sqlite"
    sidecar.parent.mkdir()
    sidecar.touch()
    monkeypatch.setattr(memory_refs.ReferenceIndex, "available", lambda _self: True)
    monkeypatch.setattr(
        memory_refs.ReferenceIndex,
        "_connect_readonly",
        lambda _self: (_ for _ in ()).throw(RuntimeError("retained target changed")),
    )

    assert memory_refs.drift(tmp_path) == [
        {
            "path": "Knowledge Base/",
            "reason": "reference sidecar unreadable: retained target changed",
        }
    ]


def test_private_sqlite_modules_have_one_raw_owned_connection_seam() -> None:
    from exomem import (
        claims,
        clip_index,
        deferred_index,
        embedding_index,
        epistemic_graph,
        lexstore,
        media_jobs,
        memory_refs,
    )

    modules = (
        claims,
        clip_index,
        deferred_index,
        embedding_index,
        epistemic_graph,
        lexstore,
        media_jobs,
        memory_refs,
    )

    class ConnectVisitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.functions: list[str] = []
            self.sites: list[tuple[str | None, int]] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.functions.append(node.name)
            self.generic_visit(node)
            self.functions.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node: ast.Call) -> None:
            function = node.func
            if (
                isinstance(function, ast.Attribute)
                and function.attr == "connect"
                and isinstance(function.value, ast.Name)
                and function.value.id == "sqlite3"
            ):
                self.sites.append(
                    (self.functions[-1] if self.functions else None, node.lineno)
                )
            self.generic_visit(node)

    for module in modules:
        visitor = ConnectVisitor()
        visitor.visit(ast.parse(inspect.getsource(module)))
        assert visitor.sites == [("_sqlite_connect_owned", visitor.sites[0][1])], (
            module.__name__,
            visitor.sites,
        )


def test_private_sqlite_raw_connection_seams_acquire_exact_owner_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import (
        claims,
        clip_index,
        deferred_index,
        embedding_index,
        epistemic_graph,
        lexstore,
        media_jobs,
        memory_refs,
    )

    cases = (
        ("claims", claims),
        ("clip_index", clip_index),
        ("deferred_index", deferred_index),
        ("embedding_index", embedding_index),
        ("epistemic_graph", epistemic_graph),
        ("lexstore", lexstore),
        ("media_jobs", media_jobs),
        ("memory_refs", memory_refs),
    )

    class ConnectionObserved(Exception):
        pass

    for owner, module in cases:
        with monkeypatch.context() as scoped:
            def observe_connect(
                *_args: object,
                expected_owner: str = owner,
                **_kwargs: object,
            ):
                owned = {
                    descriptor.id
                    for descriptor in reserved_paths.internal_state_registry()
                    if descriptor.owner == expected_owner
                    and descriptor.authority_enabled
                }
                assert owned
                assert all(reserved_paths.owner_authorized(item) for item in owned)
                assert not any(
                    reserved_paths.owner_authorized(descriptor.id)
                    for descriptor in reserved_paths.internal_state_registry()
                    if descriptor.id not in owned
                )
                raise ConnectionObserved

            scoped.setattr(module.sqlite3, "connect", observe_connect)
            with pytest.raises(ConnectionObserved):
                module._sqlite_connect(":memory:")
        assert reserved_paths._active_owner_authority() is None


def test_owner_identity_is_published_before_wal_temp_index_or_receipt_reachability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import media_jobs

    observed: list[tuple[str, tuple[str, ...]]] = []

    def observe_publication(
        vault_root: Path,
        database: Path,
        descriptor_id: str,
        connection: object,
    ) -> None:
        del connection
        assert Path(vault_root) == tmp_path
        assert descriptor_id == "media-jobs-store"
        assert reserved_paths.owner_authorized(descriptor_id)
        assert reserved_paths._identity_coordination_active(tmp_path)
        family = tuple(
            candidate.name
            for candidate in (
                database,
                database.with_name(f"{database.name}-wal"),
                database.with_name(f"{database.name}-shm"),
            )
            if candidate.is_file()
        )
        assert family == (
            ".media-jobs.sqlite",
            ".media-jobs.sqlite-wal",
            ".media-jobs.sqlite-shm",
        )
        observed.append((descriptor_id, family))

    monkeypatch.setattr(
        reserved_paths,
        "_publish_sqlite_owner_family",
        observe_publication,
        raising=False,
    )

    media_jobs.MediaJobStore(tmp_path)

    assert observed == [
        (
            "media-jobs-store",
            (
                ".media-jobs.sqlite",
                ".media-jobs.sqlite-wal",
                ".media-jobs.sqlite-shm",
            ),
        )
    ]


def test_primary_sqlite_owners_publish_under_exact_authority_and_coordination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import (
        claims,
        clip_index,
        deferred_index,
        embedding_index,
        epistemic_graph,
        lexstore,
        memory_refs,
    )
    from exomem.governance import store as governance_store

    observed: list[tuple[str, str]] = []

    def observe_publication(
        vault_root: Path,
        database: Path,
        descriptor_id: str,
        connection: object,
    ) -> None:
        del connection
        assert reserved_paths.owner_authorized(descriptor_id)
        assert reserved_paths._identity_coordination_active(vault_root)
        assert database.parent == tmp_path / "Knowledge Base"
        observed.append((descriptor_id, database.name))

    monkeypatch.setattr(
        reserved_paths,
        "_publish_sqlite_owner_family",
        observe_publication,
    )

    graph = epistemic_graph.EpistemicGraphIndex(
        tmp_path,
        mutation_coordinator=SimpleNamespace(),
    )
    cases = (
        ("embeddings-store", lambda: embedding_index.EmbeddingIndex(tmp_path)._connect()),
        ("clip-store", lambda: clip_index.ClipIndex(tmp_path)._connect()),
        ("claims-store", lambda: claims.ClaimIndex(tmp_path)._connect()),
        ("graph-store", graph._connect),
        (
            "deferred-index-store",
            lambda: deferred_index._connect(tmp_path, create=True),
        ),
        ("refs-store", lambda: memory_refs.ReferenceIndex(tmp_path)._connect()),
        ("lexical-store", lambda: lexstore.LexicalStore(tmp_path)._connect_setup()),
        ("governance-store", lambda: governance_store.open_connection(tmp_path)),
    )

    for descriptor_id, connect in cases:
        connection = connect()
        connection.close()
        assert observed[-1][0] == descriptor_id

    assert [descriptor_id for descriptor_id, _name in observed] == [
        descriptor_id for descriptor_id, _connect in cases
    ]


def test_primary_sqlite_owner_publications_are_consumable_by_stable_identity(
    tmp_path: Path,
) -> None:
    from exomem import (
        claims,
        clip_index,
        deferred_index,
        embedding_index,
        epistemic_graph,
        lexstore,
        media_jobs,
        memory_refs,
    )
    from exomem.governance import store as governance_store

    graph = epistemic_graph.EpistemicGraphIndex(
        tmp_path,
        mutation_coordinator=SimpleNamespace(),
    )
    cases = (
        (
            "embeddings-store",
            tmp_path / "Knowledge Base" / ".embeddings.sqlite",
            lambda: embedding_index.EmbeddingIndex(tmp_path)._connect(),
        ),
        (
            "clip-store",
            tmp_path / "Knowledge Base" / ".clip.sqlite",
            lambda: clip_index.ClipIndex(tmp_path)._connect(),
        ),
        (
            "claims-store",
            tmp_path / "Knowledge Base" / ".claims.sqlite",
            lambda: claims.ClaimIndex(tmp_path)._connect(),
        ),
        (
            "graph-store",
            tmp_path / "Knowledge Base" / ".graph.sqlite",
            graph._connect,
        ),
        (
            "deferred-index-store",
            tmp_path / "Knowledge Base" / ".deferred-index.sqlite",
            lambda: deferred_index._connect(tmp_path, create=True),
        ),
        (
            "refs-store",
            tmp_path / "Knowledge Base" / ".refs.sqlite",
            lambda: memory_refs.ReferenceIndex(tmp_path)._connect(),
        ),
        (
            "lexical-store",
            tmp_path / "Knowledge Base" / ".lexical.sqlite",
            lambda: lexstore.LexicalStore(tmp_path)._connect_setup(),
        ),
        (
            "governance-store",
            tmp_path / "Knowledge Base" / ".governance.sqlite",
            lambda: governance_store.open_connection(tmp_path),
        ),
    )

    for descriptor_id, path, connect in cases:
        connection = connect()
        try:
            catalogue = reserved_paths._published_identity_catalogue(tmp_path)
            assert catalogue.descriptor_for(reserved_paths._lstat_identity(path)) == descriptor_id
        finally:
            connection.close()

    media_jobs.MediaJobStore(tmp_path)
    media_path = tmp_path / "Knowledge Base" / ".media-jobs.sqlite"
    catalogue = reserved_paths._published_identity_catalogue(tmp_path)
    assert catalogue.descriptor_for(reserved_paths._lstat_identity(media_path)) == (
        "media-jobs-store"
    )


def test_governance_leaf_refuses_without_dispatcher_owner_authority(
    tmp_path: Path,
) -> None:
    from exomem.governance.principal import owner_principal
    from exomem.governance.tool import GovernanceError, op_govern_memory

    with pytest.raises(GovernanceError) as error:
        op_govern_memory(
            tmp_path,
            operation="list",
            principal=owner_principal(),
        )

    assert error.value.code == "GOVERNANCE_AUTHORITY_REQUIRED"
    assert not (tmp_path / "Knowledge Base").exists()


def test_reserved_preflight_precedes_invalid_finite_selector_parsing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.cli_ops import OpError

    command = next(
        item for item in commands.PRODUCT_COMMANDS if item.name == "process_media"
    )
    monkeypatch.setattr(
        writer_lease,
        "get_manager",
        lambda: pytest.fail("reserved preflight must precede manager acquisition"),
    )

    with pytest.raises(OpError) as error:
        writer_lease.invoke_command(
            command,
            tmp_path,
            path="Knowledge Base/_Governance/private.mp4",
            operation="future-operation",
        )

    assert error.value.code == "RESERVED_PATH"


def test_reserved_preflight_leaves_nonreserved_invalid_paths_to_leaf_validation() -> None:
    command = next(
        item for item in commands.PRODUCT_COMMANDS if item.name == "process_media"
    )

    assert reserved_paths.classify_logical("../outside.m4a").disposition is (
        reserved_paths.PathDisposition.INVALID
    )
    assert reserved_paths.reserved_preflight(
        command,
        {"path": "../outside.m4a", "operation": "process"},
    ) is None


@pytest.mark.parametrize(
    "identifier",
    [
        "exomem://vault/Knowledge%20Base/_Governance/private.md",
        "exomem://source/Knowledge%20Base/_Consolidation/run",
    ],
)
def test_managed_alias_resolution_cannot_reintroduce_reserved_paths(
    tmp_path: Path,
    identifier: str,
) -> None:
    with pytest.raises(ValueError) as error:
        commands._resolve_memory_identifier(tmp_path, identifier)

    assert error.value.args == ("NOT_FOUND: memory identifier is unavailable",)


def test_canonical_reference_resolution_cannot_reintroduce_reserved_paths(
    tmp_path: Path,
) -> None:
    from exomem import memory_refs

    memory_id = "00000000-0000-4000-8000-000000000321"
    index = memory_refs.ReferenceIndex(tmp_path)
    connection = index._connect()
    try:
        with connection:
            connection.execute(
                "INSERT OR REPLACE INTO ref_meta(key, value) VALUES (?, ?)",
                ("schema_version", str(memory_refs.SCHEMA_VERSION)),
            )
            connection.execute(
                "INSERT INTO identities(path, exomem_id, raw_id, source_hash, status) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    "Knowledge Base/_Governance/private.md",
                    memory_id,
                    memory_id,
                    "a" * 64,
                    "valid",
                ),
            )
    finally:
        connection.close()

    with pytest.raises(ValueError) as error:
        commands._resolve_memory_identifier(tmp_path, memory_refs.memory_ref(memory_id))

    assert error.value.args == ("NOT_FOUND: memory identifier is unavailable",)


def test_every_command_and_finite_selector_has_total_path_role_metadata() -> None:
    from exomem import commands

    expected = {
        "fetch": {"id"},
        "find": {"relation_of"},
        "suggest_links": {"path"},
        "graph_context": {"path", "unit_ref"},
        "suggest_relations": {"path"},
        "overview": {"path"},
        "adopt": {"path", "manifest_path", "selected_paths"},
        "provenance_report": {"path"},
        "propose_compilation": {"sources"},
        "get": {"path"},
        "edit": {"path"},
        "observe_memory": {"path", "unit_ref"},
        "replace": {"old_path", "sources"},
        "note": {"sources"},
        "query_data": {"path"},
        "create_file": {"path"},
        "list_directory": {"path"},
        "move_file": {"old_path", "new_path"},
        "delete": {"path"},
        "append_to_file": {"path"},
        "recover_from_trash": {"trash_path", "restore_path"},
        "list_inbound_links": {"target"},
        "record_memory": {"collection", "manifest_path"},
        "plan_memory": {"collection", "manifest_path"},
        "get_video_frames": {"path"},
        "ask_memory": {"relation_of"},
        "read_memory": {"path", "unit_ref"},
        "browse_memory": {"path"},
        "remember": {"sources"},
        "edit_memory": {"path"},
        "replace_memory": {"old_path", "sources"},
        "compile_source": {"sources"},
        "preserve_artifacts": {"files"},
        "process_media": {"path"},
        "review_memory": {"path", "ref", "sources"},
        "review_item_context": {"ref"},
        "triage_memory": {"ref"},
        "connect_memory": {"path", "target", "unit_ref", "ref"},
        "adopt_vault": {"path", "manifest_path", "selected_paths"},
        "adoption_studio": {
            "path",
            "include",
            "exclude",
            "only_paths",
            "sources",
            "ref",
        },
        "govern_memory": {"selector_paths", "path", "paths"},
        "manage_memory_file": {
            "path",
            "old_path",
            "new_path",
            "trash_path",
            "restore_path",
        },
        "query_dataset": {"path"},
        "read_media": {"path"},
    }

    for command in (*commands.COMMANDS, *commands.PRODUCT_COMMANDS):
        roles = command.path_roles
        assert {role.argument for role in roles} == expected.get(command.name, set()), command.name
        assert all(role.role and role.value_kind for role in roles)
        assert all(role.argument in {param.name for param in command.params} for role in roles)


def test_every_generic_command_path_role_routes_every_private_family() -> None:
    representatives = {
        "governance-tree": "Knowledge Base/_Governance/rules/private.yaml",
        "consolidation-tree": "Knowledge Base/_Consolidation/runs/run.json",
        "governance-store": "Knowledge Base/.governance.sqlite-wal",
        "embeddings-store": "Knowledge Base/.embeddings.sqlite-shm",
        "clip-store": "Knowledge Base/.clip.sqlite-journal",
        "lexical-store": "Knowledge Base/.lexical.sqlite-wal",
        "graph-store": "Knowledge Base/.graph.sqlite-shm",
        "claims-store": "Knowledge Base/.claims.sqlite-journal",
        "references-store": "Knowledge Base/.references.sqlite-wal",
        "refs-store": "Knowledge Base/.refs.sqlite-shm",
        "freshness-store": "Knowledge Base/.freshness.sqlite-journal",
        "deferred-index-store": "Knowledge Base/.deferred-index.sqlite-wal",
        "media-jobs-store": "Knowledge Base/.media-jobs.sqlite-shm",
        "idempotency-store": "Knowledge Base/.idempotency.jsonl",
        "voice-profile-store": "Knowledge Base/.voice_profiles.json",
        "graph-handoff": "Knowledge Base/.graph-sync-floor.json",
        "graph-receipts": (
            "Knowledge Base/.graph-commit-receipts/" + "1" * 24 + ".json"
        ),
        "review-state": "Knowledge Base/..review-state.json.abc123_4.tmp",
        "lexical-rebuild": (
            "Knowledge Base/.lexical.sqlite.rebuild-" + "2" * 32 + ".tmp-wal"
        ),
        "lexical-quarantine": (
            "Knowledge Base/.lexical.sqlite-shm.quarantine-" + "3" * 32
        ),
        "graph-rebuild": (
            "Knowledge Base/.graph-rebuild-"
            + "4" * 64
            + "-"
            + "5" * 24
            + ".sqlite-shm"
        ),
        "graph-reset": "Knowledge Base/.graph-reset-" + "6" * 24 + "/.manifest.json",
        "authorization-projections": (
            "Knowledge Base/.authorization-projections/generation/rows.sqlite"
        ),
        "batch-workspace": (
            "Knowledge Base/Notes/.exomem-batch-"
            + "7" * 32
            + "/stage-0.tmp"
        ),
        "held-publication": (
            "Knowledge Base/Notes/.exomem-held-publish-" + "8" * 32
        ),
    }

    for command in (*commands.COMMANDS, *commands.PRODUCT_COMMANDS):
        for role in command.path_roles:
            if role.value_kind.startswith("external-") or role.value_kind == "ref":
                continue
            for descriptor_id, path in representatives.items():
                value: object = (
                    [path]
                    if role.value_kind.endswith("-list")
                    or role.value_kind == "path-list"
                    else path
                )
                hit = reserved_paths.reserved_preflight(
                    command,
                    {role.argument: value},
                )
                assert hit is not None, (command.name, role.argument, descriptor_id)
                assert hit.role == role
                assert hit.classification.descriptor_id == descriptor_id
