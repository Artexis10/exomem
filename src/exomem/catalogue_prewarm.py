"""Build and adopt a file-mode catalogue without starting the cell runtime."""

from __future__ import annotations

import argparse
import contextlib
import logging
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

from . import freshness, lexstore, reserved_paths, state_paths
from .state_migration import vault_store_recorded
from .vault import VaultLockError, _InterprocessFileLock

log = logging.getLogger(__name__)


@contextlib.contextmanager
def _build_lock(store: lexstore.LexicalStore) -> Iterator[None]:
    """Keep one stable inode across contenders; never wait for another build."""
    directory = state_paths.ensure_vault_state_dir(store.vault_root)
    path = directory / reserved_paths.LEXICAL_STAGED_LOCK
    lock = None
    try:
        with reserved_paths._subsystem_authority_scope("lexstore"):
            with reserved_paths._identity_coordination_scope(
                store.vault_root, descriptor_ids=("lexical-staged-lock",)
            ):
                with reserved_paths._sqlite_owner_target_scope(
                    store.vault_root, path, "lexical-staged-lock", create=True
                ) as retained:
                    lock = _InterprocessFileLock(retained, deadline=time.monotonic())
                    lock.__enter__()
        yield
    finally:
        if lock is not None:
            lock.__exit__(None, None, None)


def _catalog_valid(
    store: lexstore.LexicalStore, path: Path, owner: str, identity: str
) -> bool:
    """Inspect a folded staged copy or the live main without SQLite housekeeping."""
    if not path.is_file():
        return False
    # A staged copy is a single file; immutable reads must never hide its WAL.
    if owner == "lexical-staged" and any(
        Path(f"{path}{suffix}").exists() for suffix in reserved_paths._SQLITE_SUFFIXES[1:]
    ):
        return False
    conn = None
    try:
        with reserved_paths._subsystem_authority_scope("lexstore"):
            with reserved_paths._identity_coordination_scope(
                store.vault_root, descriptor_ids=(owner,), identity_may_change=False
            ):
                with reserved_paths._sqlite_owner_target_scope(
                    store.vault_root, path, owner, create=False
                ) as retained:
                    conn = sqlite3.connect(f"{retained.as_uri()}?mode=ro&immutable=1", uri=True)
                    # The live self-gate needs only identity; staged adoption requires complete validation.
                    if owner == "lexical-store":
                        return store._meta_catalog_identity(conn) == identity
                    return (
                        store._schema_is_current(conn)
                        and store._meta_catalog_identity(conn) == identity
                        and all(
                            lexstore.checkpoint_state(store._meta_checkpoint(conn, scope)) is not None
                            for scope in freshness.SCOPES
                        )
                        and conn.execute("PRAGMA quick_check").fetchone() == ("ok",)
                    )
    except (sqlite3.Error, OSError, RuntimeError):
        return False
    finally:
        if conn is not None:
            conn.close()


def _staged_copies(store: lexstore.LexicalStore) -> list[Path]:
    copies = set()
    for member in store.path.parent.glob(".lexical-staged-*"):
        if reserved_paths.state_target_descriptor_id(store.vault_root, member) != "lexical-staged":
            continue
        name = member.name
        for suffix in reserved_paths._SQLITE_SUFFIXES[1:]:
            name = name.removesuffix(suffix)
        copies.add(member.with_name(name))
    return sorted(copies)


def _discard_staged(store: lexstore.LexicalStore, path: Path) -> None:
    with reserved_paths._subsystem_authority_scope("lexstore"):
        for suffix in reserved_paths._SQLITE_SUFFIXES:
            reserved_paths._remove_owner_file(
                store.vault_root, Path(f"{path}{suffix}"), "lexical-staged", missing_ok=True
            )


def build(vault_root: Path) -> str:
    """Leave only a complete staged copy; all successful self-gates are inert."""
    # The manifest also covers a store transition; skipping it avoids SQLite reader WAL housekeeping.
    if vault_store_recorded(vault_root):
        return "store-mode-or-transition"
    store = lexstore.LexicalStore(vault_root)
    try:
        with _build_lock(store):
            identity = lexstore.catalog_semantic_identity(vault_root)
            keep = None
            for path in _staged_copies(store):
                if keep is None and _catalog_valid(store, path, "lexical-staged", identity):
                    keep = path
                else:
                    _discard_staged(store, path)
            store._sweep_orphan_rebuild_temps()
            if keep is not None:
                return "already-staged"
            if _catalog_valid(store, store.path, "lexical-store", identity):
                return "already-live"
            detached = store.build_detached_catalog()
            if detached is None:
                return "unavailable"
            try:
                # Discovery uses the prefix; validation always trusts the stored full identity.
                staged = store.path.with_name(f".lexical-staged-{identity[:16]}.sqlite")
                with reserved_paths._subsystem_authority_scope("lexstore"):
                    reserved_paths._move_owner_file(
                        vault_root, detached.path, "lexical-rebuild", staged, "lexical-staged",
                        replace=False,
                    )
                return "staged"
            finally:
                store.discard_detached_catalog(detached)
    except VaultLockError:
        return "busy"


def adopt_at_boot(vault_root: Path) -> bool:
    """Adopt before runtime workers open the live catalogue, then remove leftovers."""
    if vault_store_recorded(vault_root):
        return False
    store = lexstore.get_store(vault_root)
    adopted = False
    try:
        with _build_lock(store):
            try:
                identity = lexstore.catalog_semantic_identity(vault_root)
                for path in _staged_copies(store):
                    if not _catalog_valid(store, path, "lexical-staged", identity):
                        continue
                    with store._publication_lock():
                        # Recheck under the same barrier that installs the whole live SQLite set.
                        if _catalog_valid(store, path, "lexical-staged", identity):
                            adopted = store._replace_live_catalog(path, source_owner="lexical-staged")
                    if adopted:
                        break
            except (VaultLockError, sqlite3.Error, OSError, RuntimeError):
                log.info("Staged catalogue adoption declined; the cell will reconcile or rebuild.")
            finally:
                for path in _staged_copies(store):
                    _discard_staged(store, path)
                # Cellctl ends all build pods before the serving pod stops; released locks suffice here.
                store._sweep_orphan_rebuild_temps(boot=True)
    except (VaultLockError, OSError, RuntimeError):
        log.info("Staged catalogue cleanup deferred; the cell keeps its ordinary boot path.")
    return adopted


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", type=Path, required=True)
    args = parser.parse_args()
    outcome = build(args.vault.resolve())
    print(f"Catalogue build: {outcome}.")
    return 1 if outcome == "unavailable" else 0


if __name__ == "__main__":
    raise SystemExit(main())
