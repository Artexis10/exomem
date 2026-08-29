"""Machine-local state placement pins (relocate-machine-local-state).

The defect class: machine-local derived state living inside the vault gets
hashed, held, and replaced by the user's file-sync agent (Syncthing, Dropbox,
OneDrive), which has already cost a day-long outage. The fix is placement:
every machine-local family resolves under a per-user, per-vault state root
outside the vault, through ONE resolver seam (`exomem.state_paths`).

Two self-enforcing pins live here (design.md "Risks"):

- the constructor walk: every machine-local family in `reserved_paths`'
  registry maps to a pinned constructor (or a documented follows-its-target
  exemption), so a new family added without routing through the seam goes red;
- the seam pins: `EXOMEM_STATE_ROOT` relocates every consumer at once, and a
  consumer composing its own root — even one reading the env var itself —
  dies on the monkeypatched-seam walk.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def _constructor_map():
    """descriptor id -> the path constructors this suite pins for it.

    Sibling scratch families (lexical rebuild/quarantine, graph-rebuild) are
    composed with ``with_name`` beside their parent store, so pinning the
    store's constructor pins the scratch location too.
    """
    from exomem import (
        claims,
        deferred_index,
        due_state,
        epistemic_graph,
        graph_sync,
        index_paths,
        lexstore,
        media_jobs,
        memory_refs,
        review_state,
        voice_profiles,
    )
    from exomem.governance import (
        projection_measurement_store,
        projection_store,
        projections,
    )

    namespace = projections.ProjectionNamespaceKey(
        policy_fingerprint="a" * 64,
        projector_schema_version=1,
        catalog_generation=1,
    )
    measurement = projection_measurement_store.MeasurementFamilyKey(
        namespace_key=namespace,
        lane="vector",
        extractor_version="extractor-v1",
        model_version="model-v1",
    )

    return {
        "deferred-index-store": (deferred_index.store_path,),
        "embeddings-store": (index_paths.sidecar_path,),
        "lexical-store": (lexstore.lexical_path,),
        "lexical-rebuild": (lexstore.lexical_path,),
        "lexical-quarantine": (lexstore.lexical_path,),
        "graph-store": (epistemic_graph.sidecar_path,),
        "graph-rebuild": (epistemic_graph.sidecar_path,),
        "graph-reset": (
            lambda root: graph_sync._reset_directory(root, "a" * 24),
        ),
        "claims-store": (claims.sidecar_path,),
        "graph-handoff": (
            graph_sync.checkpoint_path,
            graph_sync.floor_path,
            graph_sync.recovery_marker_path,
        ),
        "graph-receipts": (
            lambda root: graph_sync.graph_commit_receipt_path(root, "a" * 24),
        ),
        "clip-store": (index_paths.clip_sidecar_path,),
        "refs-store": (memory_refs.sidecar_path,),
        "media-jobs-store": (media_jobs.job_store_path, media_jobs.worker_lock_path),
        "governance-store": (index_paths.governance_sidecar_path,),
        "due-state": (due_state.state_path,),
        "review-state": (review_state.state_path,),
        "voice-profile-store": (voice_profiles.voice_profiles_path,),
        "authorization-projections": (
            lambda root: projection_store.variant_store_path(root, namespace),
            lambda root: projection_measurement_store.measurement_store_path(
                root, measurement
            ),
        ),
    }


#: Families whose members are created beside their *operation's target*, not
#: composed from the vault root. A batch workspace stages the note it installs
#: and the install is an atomic same-volume rename, so it must live beside the
#: note; a held-publication temp is a leaf under the already-retained parent of
#: whatever is being published, so it follows state to the external root
#: automatically and follows content into the vault necessarily.
_FOLLOWS_ITS_TARGET = {
    "batch-workspace": "stages a note install; atomic rename requires the note's volume",
    "held-publication": "temp leaf under the retained parent of the published file",
}


# Independent contract inventory.  This is intentionally not generated from
# reserved_paths: a missing descriptor and a missing constructor map must not
# be able to agree green.  Legacy families with no current constructor remain
# here because their names stay reserved and their bytes still migrate.
_SOURCE_DESCRIBED_EXTERNAL_FAMILIES = {
    "authorization-projections",
    "claims-store",
    "clip-store",
    "deferred-index-store",
    "due-state",
    "embeddings-store",
    "freshness-store",
    "governance-store",
    "graph-coordination",
    "graph-handoff",
    "graph-rebuild",
    "graph-receipts",
    "graph-reset",
    "graph-store",
    "idempotency-store",
    "lexical-quarantine",
    "lexical-rebuild",
    "lexical-store",
    "media-jobs-store",
    "references-store",
    "refs-store",
    "review-state",
    "voice-profile-store",
}

_SOURCE_DESCRIBED_TARGET_ADJACENT_FAMILIES = {
    "batch-workspace",
    "held-publication",
}

_SOURCE_DESCRIBED_VAULT_CANONICAL_FAMILIES = {
    "consolidation-tree",
    "governance-tree",
}


def _external_root() -> Path:
    # The autouse fixture injects EXOMEM_STATE_ROOT at a tmpdir for every test.
    return Path(os.environ["EXOMEM_STATE_ROOT"])


def test_every_machine_local_constructor_resolves_under_the_external_root(
    tmp_path: Path,
) -> None:
    """Task 1.1: no machine-local family resolves inside the vault."""
    vault = tmp_path / "vault"
    root = _external_root()
    for descriptor_id, constructors in _constructor_map().items():
        for constructor in constructors:
            path = Path(constructor(vault))
            assert root in path.parents, (
                f"{descriptor_id}: {path} resolves outside the external state root "
                f"{root} — machine-local state must not live under the vault"
            )
            assert vault not in path.parents, (
                f"{descriptor_id}: {path} still resolves inside the vault"
            )


def test_source_inventory_and_registry_inventory_agree_independently() -> None:
    """Task 1.1: source-described families and registry placement agree.

    The expected sets above are a hand-maintained inventory of source
    constructors plus approved legacy names, independent of the registry.  A
    new path in either inventory without the other therefore fails closed.
    """
    from exomem import reserved_paths

    external = {
        descriptor.id
        for descriptor in reserved_paths.internal_state_registry()
        if descriptor.placement is reserved_paths.StatePlacement.EXTERNAL_STATE
    }
    target_adjacent = {
        descriptor.id
        for descriptor in reserved_paths.internal_state_registry()
        if descriptor.placement is reserved_paths.StatePlacement.TARGET_ADJACENT
    }
    vault_canonical = {
        descriptor.id
        for descriptor in reserved_paths.internal_state_registry()
        if descriptor.placement is reserved_paths.StatePlacement.VAULT_CANONICAL
    }

    assert external == _SOURCE_DESCRIBED_EXTERNAL_FAMILIES
    assert target_adjacent == _SOURCE_DESCRIBED_TARGET_ADJACENT_FAMILIES
    assert vault_canonical == _SOURCE_DESCRIBED_VAULT_CANONICAL_FAMILIES
    assert set(_constructor_map()) <= external
    assert set(_FOLLOWS_ITS_TARGET) == target_adjacent


def test_owner_anchor_refuses_external_descriptor_under_the_vault(tmp_path: Path) -> None:
    from exomem import reserved_paths

    vault = tmp_path / "vault"
    with pytest.raises(RuntimeError, match="placement"):
        reserved_paths._owner_anchor(
            vault,
            vault / "Knowledge Base" / ".embeddings.sqlite",
            operation="placement regression",
        )


def test_owner_anchor_refuses_vault_descriptor_under_external_state(tmp_path: Path) -> None:
    from exomem import reserved_paths, state_paths

    vault = tmp_path / "vault"
    external = state_paths.vault_state_dir(vault)
    with pytest.raises(RuntimeError, match="placement"):
        reserved_paths._owner_anchor(
            vault,
            external / "_governance" / "policy.json",
            operation="placement regression",
        )


def test_every_descriptor_has_exactly_one_explicit_placement() -> None:
    from exomem import reserved_paths

    registry = reserved_paths.internal_state_registry()
    assert registry
    assert all(
        type(descriptor.placement) is reserved_paths.StatePlacement
        for descriptor in registry
    )
    assert all(not hasattr(descriptor, "machine_local") for descriptor in registry)
    partition = (
        _SOURCE_DESCRIBED_EXTERNAL_FAMILIES
        | _SOURCE_DESCRIBED_TARGET_ADJACENT_FAMILIES
        | _SOURCE_DESCRIBED_VAULT_CANONICAL_FAMILIES
    )
    assert {descriptor.id for descriptor in registry} == partition
    assert sum(
        descriptor.id in family
        for descriptor in registry
        for family in (
            _SOURCE_DESCRIBED_EXTERNAL_FAMILIES,
            _SOURCE_DESCRIBED_TARGET_ADJACENT_FAMILIES,
            _SOURCE_DESCRIBED_VAULT_CANONICAL_FAMILIES,
        )
    ) == len(registry), (
        "every registered family must appear in exactly one placement partition"
    )


def test_the_seam_resolves_env_override_then_platform_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Task 1.2: the resolver order is EXOMEM_STATE_ROOT -> platform state dir."""
    from exomem import state_paths

    override = tmp_path / "override-root"
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(override))
    assert state_paths.state_store_root() == override

    monkeypatch.delenv("EXOMEM_STATE_ROOT")
    if os.name == "nt":
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
        assert (
            state_paths.state_store_root()
            == tmp_path / "localappdata" / "exomem" / "state"
        )
    else:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
        assert (
            state_paths.state_store_root()
            == tmp_path / "xdg-state" / "exomem" / "state"
        )
        monkeypatch.delenv("XDG_STATE_HOME")
        assert (
            state_paths.state_store_root()
            == Path.home() / ".local" / "state" / "exomem" / "state"
        )
    # With the env override absent, the two spellings agree — the guard
    # fixture watches platform_default_state_root, so drift between them
    # would blind the guard.
    assert state_paths.state_store_root() == state_paths.platform_default_state_root()


@pytest.mark.parametrize("override", ("relative/state", "~/state", "   "))
def test_relative_state_root_override_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    override: str,
) -> None:
    from exomem import state_paths

    monkeypatch.setenv("EXOMEM_STATE_ROOT", override)
    with pytest.raises(ValueError, match="absolute"):
        state_paths.state_store_root()


def test_private_root_setup_failure_is_fatal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from exomem import state_paths

    monkeypatch.setattr(state_paths, "_is_windows", lambda: True)

    def fail_closed(_directory: Path) -> None:
        raise OSError("simulated private-root failure")

    monkeypatch.setattr(
        state_paths,
        "_prepare_windows_private_state_root",
        fail_closed,
    )
    expected = state_paths.vault_state_dir(tmp_path / "vault")

    with pytest.raises(OSError, match="private-root failure"):
        state_paths.ensure_vault_state_dir(tmp_path / "vault")

    assert not expected.exists(), "failure must not leave a plain directory behind"


def test_state_root_nested_inside_vault_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from exomem import state_paths

    vault = tmp_path / "vault"
    nested_root = vault / ".local-state"
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(nested_root))

    with pytest.raises(ValueError, match="outside the vault"):
        state_paths.ensure_vault_state_dir(vault)

    assert not nested_root.exists()


def test_nested_state_root_refuses_forged_manifest_before_cache_or_owner_io(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A valid-looking manifest/cache entry cannot waive physical placement."""

    from exomem import due_state, state_migration, state_paths

    vault = tmp_path / "vault"
    vault.mkdir()
    nested_root = vault / ".machine-local"
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(nested_root))
    state_dir = nested_root / state_paths.vault_state_key(vault)
    state_dir.mkdir(parents=True)
    manifest = state_migration._new_manifest(vault, state_migration._descriptor_ids())
    manifest["families"] = {
        descriptor_id: {"status": "complete"}
        for descriptor_id in manifest["descriptors"]
    }
    manifest["state"] = "complete"
    (state_dir / state_migration.MANIFEST_NAME).write_text(
        __import__("json").dumps(manifest),
        encoding="utf-8",
    )
    key = state_migration._cache_key(vault, state_dir)
    state_migration._RESOLUTION_CACHE[key] = state_migration.StateResolution(
        state_dir,
        migrated=True,
        dual_state=False,
    )

    with pytest.raises(ValueError, match="outside the vault"):
        state_migration.migration_status(vault)
    with pytest.raises(ValueError, match="outside the vault"):
        state_migration.require_vault_state_ready(vault)
    with pytest.raises(ValueError, match="outside the vault"):
        due_state.state_path(vault)

    assert state_migration._RESOLUTION_CACHE[key].state_dir == state_dir


def test_distinct_vaults_get_distinct_stable_state_dirs(tmp_path: Path) -> None:
    """Spec scenario: state roots are keyed by stable vault identity."""
    from exomem import state_paths

    first = state_paths.vault_state_dir(tmp_path / "vault-a")
    second = state_paths.vault_state_dir(tmp_path / "vault-b")
    assert first != second
    assert first.parent == second.parent == state_paths.state_store_root()
    # Same vault, same key — across repeated resolution and spelling noise.
    assert state_paths.vault_state_dir(tmp_path / "vault-a") == first
    assert state_paths.vault_state_dir(tmp_path / "vault-a" / ".." / "vault-a") == first
    # A moved vault maps to a new key (regenerates rather than aliasing).
    assert state_paths.vault_state_dir(tmp_path / "moved" / "vault-a") != first


def test_setting_the_env_root_relocates_every_consumer_at_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Spec scenario, and half the mutation-proof: a constructor that composes
    its own platform root (instead of consulting the seam) does not follow the
    env override and dies here."""
    vault = tmp_path / "vault"
    first_root = tmp_path / "root-one"
    second_root = tmp_path / "root-two"

    for root in (first_root, second_root):
        monkeypatch.setenv("EXOMEM_STATE_ROOT", str(root))
        for descriptor_id, constructors in _constructor_map().items():
            for constructor in constructors:
                path = Path(constructor(vault))
                assert root in path.parents, (
                    f"{descriptor_id}: EXOMEM_STATE_ROOT={root} did not relocate "
                    f"{path} — this consumer does not follow the resolver seam"
                )


def test_every_constructor_selects_its_root_through_the_seam(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The other half of the mutation-proof, pinned at the call sites.

    The seam function itself is replaced with a spy that redirects to a
    sentinel directory. A consumer that composes its own root — including one
    that reads EXOMEM_STATE_ROOT directly instead of calling the seam — does
    not land under the sentinel, and a spy that was never called proves
    nothing, so non-vacuity is asserted too (the `_follower_wait_seconds`
    pinning discipline).
    """
    from exomem import state_paths

    vault = tmp_path / "vault"
    sentinel = tmp_path / "sentinel-root"
    observed: list[Path] = []
    real = state_paths.vault_state_dir

    def spy(vault_root: Path) -> Path:
        observed.append(Path(vault_root))
        return sentinel / real(vault_root).name

    monkeypatch.setattr(state_paths, "vault_state_dir", spy)

    checked = 0
    for descriptor_id, constructors in _constructor_map().items():
        for constructor in constructors:
            path = Path(constructor(vault))
            checked += 1
            assert sentinel in path.parents, (
                f"{descriptor_id}: {path} was not derived through "
                f"state_paths.vault_state_dir — the constructor composes its own root"
            )
    assert checked > 0 and len(observed) >= checked, (
        "the seam spy was not consulted once per constructed path "
        f"(constructed {checked}, observed {len(observed)})"
    )


def test_a_fresh_vault_builds_state_in_the_external_root(tmp_path: Path) -> None:
    """Task 1.4: adoption on a new machine — no external root, no in-vault
    state — builds fresh state outside the vault from vault content."""
    from exomem import due_state
    from exomem.kbdir import kb_dirname

    vault = tmp_path / "vault"
    (vault / kb_dirname()).mkdir(parents=True)
    payload = {"version": due_state.SCHEMA_VERSION, "categories": {}}

    due_state.save(vault, payload)

    state_file = due_state.state_path(vault)
    assert _external_root() in state_file.parents
    assert state_file.is_file(), "the fresh build did not land in the external root"
    assert due_state.load(vault) == payload
    kb_entries = {entry.name for entry in (vault / kb_dirname()).iterdir()}
    assert not any(name.startswith(".due-state") for name in kb_entries), (
        f"fresh state leaked into the vault: {sorted(kb_entries)}"
    )


def test_batch_workspace_remains_target_adjacent(
    tmp_path: Path,
) -> None:
    from exomem import reserved_paths
    from exomem import vault as vault_module

    target = tmp_path / "vault" / "Knowledge Base" / "Notes" / "note.md"
    target.parent.mkdir(parents=True)
    workspace = vault_module._BatchWorkspace.create(target.parent)
    try:
        assert workspace.path.parent == target.parent
        assert _external_root() not in workspace.path.parents
        classification = reserved_paths.classify_logical(
            workspace.path.relative_to(tmp_path / "vault" / "Knowledge Base").as_posix()
        )
        assert classification.descriptor_id == "batch-workspace"
        descriptor = next(
            item
            for item in reserved_paths.internal_state_registry()
            if item.id == "batch-workspace"
        )
        assert descriptor.placement is reserved_paths.StatePlacement.TARGET_ADJACENT
    finally:
        assert workspace.cleanup()


def test_tui_runtime_requires_ready_state_before_background_warm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from exomem import state_migration, warmup
    from exomem.tui.backend import ExomemBackend

    vault = tmp_path / "vault"
    events: list[tuple[str, Path]] = []
    backend = ExomemBackend(str(vault))
    backend._vault = vault

    monkeypatch.setattr(backend, "_apply_lean_fallback", lambda: None)
    monkeypatch.setattr(
        state_migration,
        "require_vault_state_ready",
        lambda root: events.append(("ready", Path(root))),
        raising=False,
    )
    monkeypatch.setattr(
        warmup,
        "start_background",
        lambda root: events.append(("warm", Path(root))),
    )

    backend.start_runtime()

    assert events == [("ready", vault), ("warm", vault)]


def test_stateful_status_cli_requires_ready_state_before_collecting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    from exomem import __main__, resource_status, state_migration

    vault = tmp_path / "vault"
    events: list[tuple[str, Path]] = []
    monkeypatch.setattr(
        state_migration,
        "require_vault_state_ready",
        lambda root: events.append(("ready", Path(root))),
        raising=False,
    )

    def collect(root: Path) -> dict[str, object]:
        events.append(("collect", Path(root)))
        return {
            "mode": "normal",
            "source": "default",
            "config_path": "unused",
            "models": {},
            "media": {},
            "deferred_work": {},
            "cuda": {},
        }

    monkeypatch.setattr(resource_status, "collect", collect)

    assert __main__._status_main(["--vault", str(vault), "--json"]) == 0
    assert events == [("ready", vault), ("collect", vault)]
    assert capsys.readouterr().out


def test_maintain_state_migration_requires_explicit_offline_authority(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from exomem import __main__, state_migration

    vault = tmp_path / "vault"
    events: list[object] = []
    authority = object()
    monkeypatch.setattr(
        state_migration,
        "assert_offline_migration_authority",
        lambda *, source: events.append(("authority", source)) or authority,
        raising=False,
    )
    monkeypatch.setattr(
        state_migration,
        "migrate_vault_state_offline",
        lambda root, *, authority, adopt=None: (
            events.append(("migrate", Path(root), authority, adopt))
            or state_migration.StateResolution(Path(root) / "state", True, False)
        ),
        raising=False,
    )

    with pytest.raises(SystemExit):
        __main__._simple_maintain_main(
            ["--vault", str(vault), "--migrate-state"]
        )
    assert events == []

    assert (
        __main__._simple_maintain_main(
            ["--vault", str(vault), "--migrate-state", "--offline", "--json"]
        )
        == 0
    )
    assert events[0][0] == "authority"
    assert events[1] == ("migrate", vault, authority, None)


def test_hosted_offline_migration_holds_lifetime_lock_around_migration_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from exomem import __main__, hosted_restore, hosted_runtime, state_migration

    events: list[object] = []
    authority = object()

    class Config:
        vault_root = tmp_path / "vault"
        state_root = tmp_path / "hosted-state"
        log_root = tmp_path / "logs"
        requires_dynamic_security = False

        def apply_process_environment(self) -> None:
            events.append("environment")

    class LifetimeLock:
        def __enter__(self):
            events.append("lifetime-enter")

        def __exit__(self, *_args):
            events.append("lifetime-exit")

    monkeypatch.setattr(hosted_runtime, "hosted_mode_enabled", lambda: True)
    monkeypatch.setattr(
        hosted_runtime.HostedCellConfig,
        "from_env",
        classmethod(lambda _cls, *, require_provisioned: Config()),
    )
    monkeypatch.setattr(
        hosted_restore,
        "acquire_hosted_lifetime_lock",
        lambda root, *, binding: (
            events.append(("lifetime-lock", Path(root), binding)) or LifetimeLock()
        ),
    )
    monkeypatch.setattr(
        state_migration,
        "assert_offline_migration_authority",
        lambda *, source: events.append(("authority", source)) or authority,
    )
    monkeypatch.setattr(
        state_migration,
        "migrate_vault_state_offline",
        lambda root, *, authority, adopt=None: (
            events.append(("migration-lock", Path(root), authority, adopt))
            or state_migration.StateResolution(tmp_path / "ready", True, False)
        ),
    )

    result = __main__._run_offline_state_migration(vault=None, adopt=None)

    assert result.state_dir == tmp_path / "ready"
    assert events == [
        "environment",
        ("lifetime-lock", Config.state_root, None),
        "lifetime-enter",
        ("authority", "hosted target-image offline migration job"),
        ("migration-lock", Config.vault_root, authority, None),
        "lifetime-exit",
    ]


def test_shipped_guidance_does_not_name_relocated_state_as_in_vault() -> None:
    root = Path(__file__).resolve().parents[1]
    shipped = (
        root / "src/exomem/_scaffold/_Schema/SKILL.md",
        root / "plugins/claude-code/skills/exomem/SKILL.md",
        root / "src/exomem/product_invoke.py",
        root / "src/exomem/__main__.py",
        root / "src/exomem/vault.py",
        root / "src/exomem/reserved_paths.py",
        root / "docs/epistemic-inbox.md",
    )
    forbidden = (
        "<vault>/Knowledge Base/.embeddings.sqlite",
        "Knowledge Base/.review-state.json",
        "Knowledge Base/.due-state.json",
        "first start over a vault carrying in-vault state migrates",
        "first time over an old vault, migrates",
        "Resolve and migrate the vault",
        "Closed authority for Exomem's private in-vault state paths",
    )

    violations = {
        str(path.relative_to(root)): [token for token in forbidden if token in path.read_text(encoding="utf-8")]
        for path in shipped
    }
    assert not {path: tokens for path, tokens in violations.items() if tokens}
