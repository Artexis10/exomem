"""Move a file-mode vault into store mode (OpenSpec make-vault-store-canonical D8, tasks 1.1, 1.3 and 1.4).

Migration runs offline, as the owner. It stages the vault's exact bytes and the sidecars' current rows in a private
database, proves that those rows equal what the owners derive from the files, and only then installs everything in one
writer transaction. A refusal or a kill leaves file mode as it was.
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import tempfile
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import closing, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .. import (
    __version__,
    cloud_cell,
    epistemic_graph,
    lexstore,
    memory_refs,
    state_migration,
    state_paths,
    vault,
    writer_lease,
)
from ..cli_ops import OpError
from ..collection_store import admission, authority, capability, connection
from . import availability, marker, mode, ownership, rederive, verify
from .write import Origin, frontmatter_json

#: A migration's private directory beside the live store; a run that died leaves one, and the next migrate or rollback removes it.
STAGING_PREFIX = ".exomem-vault-store-migration-"
_COPY_BATCH = 5000


@dataclass(frozen=True, slots=True)
class FileFact:
    """What one owned file holds: its bytes' hash and size, and the mtime it had when read."""

    sha256: str
    size: int
    mtime_ns: int


@dataclass(slots=True)
class Staged:
    """What staging loaded, and the objects the owners' schema created in the staged database."""

    pages: int = 0
    binaries: int = 0
    skipped: list[ownership.Skipped] = field(default_factory=list)
    #: Each object the lexical, then the graph, then the references, owner created: name -> type, in creation order.
    objects: dict[str, str] = field(default_factory=dict)
    lexical_tables: list[str] = field(default_factory=list)
    graph_tables: list[str] = field(default_factory=list)
    refs_tables: list[str] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)


def snapshot(
    vault_root: Path, on_skip=None
) -> Iterator[tuple[ownership.OwnedFile, FileFact, bytes | None]]:
    """Read each file the store owns once: its facts, and its bytes when it is a page."""
    for owned in ownership.iter_owned(vault_root, on_skip=on_skip):
        path = Path(vault_root) / owned.rel
        if owned.ownership is ownership.Ownership.PAGE:
            body = vault.read_disk_bytes_without_pinning(path)
            digest = hashlib.sha256(body).hexdigest()
        else:
            body = None
            with open(path, "rb") as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
        yield (
            owned,
            FileFact(
                digest, owned.stat.st_size if body is None else len(body), owned.stat.st_mtime_ns
            ),
            body,
        )


def schema_objects(conn: sqlite3.Connection) -> dict[str, str]:
    """Every named object in the main schema: name -> table, virtual, shadow, index or trigger."""
    kinds = dict(conn.execute("SELECT name, type FROM pragma_table_list WHERE schema = 'main'"))
    return {
        name: kinds.get(name, kind) if kind == "table" else kind
        for name, kind in conn.execute(
            "SELECT name, type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY rowid"
        )
    }


def create_owner_schema(
    conn: sqlite3.Connection,
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """Create the lexical, graph, then references tables with their owners' DDL; return what each created."""
    before = schema_objects(conn)
    lexstore.create_catalogue_schema(conn)
    middle = schema_objects(conn)
    # The store publishes nothing to independent readers, so it takes no capture tables or triggers.
    epistemic_graph.create_row_schema(conn, publication=False)
    graph_done = schema_objects(conn)
    memory_refs.create_identity_schema(conn)
    after = schema_objects(conn)
    return (
        {n: t for n, t in middle.items() if n not in before},
        {n: t for n, t in graph_done.items() if n not in middle},
        {n: t for n, t in after.items() if n not in graph_done},
    )


def stage(vault_root: Path, conn: sqlite3.Connection, *, revision: int = 1) -> Staged:
    """Load the exact bytes of every owned file and copy the sidecars' rows into ``conn``, a staged store."""
    staged = Staged()
    started = time.perf_counter()
    lexical, graph, refs = create_owner_schema(conn)
    staged.objects = {**lexical, **graph, **refs}
    staged.lexical_tables = [n for n, t in lexical.items() if t in ("table", "virtual")]
    staged.graph_tables = [n for n, t in graph.items() if t in ("table", "virtual")]
    staged.refs_tables = [n for n, t in refs.items() if t in ("table", "virtual")]
    _load_files(vault_root, conn, staged, revision)
    staged.timings["load_files_s"] = time.perf_counter() - started
    started = time.perf_counter()
    _copy_sidecars(vault_root, conn, staged)
    staged.timings["copy_sidecars_s"] = time.perf_counter() - started
    return staged


def _load_files(vault_root: Path, conn: sqlite3.Connection, staged: Staged, revision: int) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        for owned, fact, body in snapshot(vault_root, staged.skipped.append):
            if body is not None:
                conn.execute(
                    "INSERT INTO store_pages(path, body, sha256, size, frontmatter_json, revision, mtime_ns) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        owned.rel,
                        body,
                        fact.sha256,
                        fact.size,
                        frontmatter_json(body),
                        revision,
                        fact.mtime_ns,
                    ),
                )
                staged.pages += 1
            else:
                conn.execute(
                    "INSERT INTO store_binaries(path, sha256, size, revision) VALUES (?, ?, ?, ?)",
                    (owned.rel, fact.sha256, fact.size, revision),
                )
                staged.binaries += 1
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _copy_sidecars(vault_root: Path, conn: sqlite3.Connection, staged: Staged) -> None:
    """Copy each sidecar table, rows and row ids as they are, into the staged database."""
    sources = (
        (lexstore.lexical_path(vault_root), staged.lexical_tables),
        (epistemic_graph.sidecar_path(vault_root), staged.graph_tables),
        (memory_refs.sidecar_path(vault_root), staged.refs_tables),
    )

    def copy() -> None:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for path, tables in sources:
                # A read-only connection of its own: ATTACH would parse the graph sidecar's publication
                # triggers against this schema.
                with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as source:
                    for table in tables:
                        _copy_table(conn, source, table)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    _copy_rows_without_triggers(conn, staged, copy)


def _copy_rows_without_triggers(conn: sqlite3.Connection, staged: Staged, copy) -> None:
    """Run ``copy`` with the owners' triggers dropped, then restore them with the owners' DDL and prove they match.

    A trigger would rewrite the revision and stamp rows that the copy carries.
    """
    for name, kind in staged.objects.items():
        if kind == "trigger":
            conn.execute(f'DROP TRIGGER "{name}"')
    copy()
    create_owner_schema(conn)
    if {n: t for n, t in schema_objects(conn).items() if n in staged.objects} != staged.objects:
        raise OpError(
            "VAULT_STORE_SCHEMA_DRIFT",
            "the owners' schema did not recreate the objects it first created",
        )


def _copy_table(conn: sqlite3.Connection, source: sqlite3.Connection, table: str) -> None:
    columns = ", ".join(f'"{row[1]}"' for row in conn.execute(f'PRAGMA main.table_info("{table}")'))
    marks = ", ".join("?" * (columns.count(",") + 2))
    # The owners' DDL seeds a few stamp rows; the sidecar's own replace them.
    conn.execute(f'DELETE FROM main."{table}"')
    rows = source.execute(f'SELECT rowid, {columns} FROM "{table}"')
    while batch := rows.fetchmany(_COPY_BATCH):
        conn.executemany(f'INSERT INTO main."{table}"(rowid, {columns}) VALUES ({marks})', batch)


def verify_staged(
    vault_root: Path, conn: sqlite3.Connection, work: Path
) -> tuple[verify.Comparison, list[str]]:
    """Derive the rows afresh from the files and compare them, then project the stored pages and compare bytes."""
    expected = work / "expected.sqlite"
    verify.derive_expected(vault_root, expected)
    comparison = verify.compare_tables(conn, expected)
    expected.unlink()
    return comparison, verify.projection_differences(conn, work / "projection")


def preflight(vault_root: Path) -> dict:
    """Refuse a vault that cannot enter store mode; otherwise count what a migration would take. It takes no lease.

    Each refusal costs the owner one command to fix: the release gate, the lease topology, the collection store,
    the mode and the two sidecars are the facts that a migration started on the wrong footing would fail on late.
    """
    admission.require_owner(vault_root, "store-mode migrate")
    if not capability.vault_store_enabled():
        raise OpError(
            "VAULT_STORE_NOT_RELEASED",
            f"this release has not enabled {capability.VAULT_STORE_V1}, so no vault enters store mode",
            "Use a release that enables store mode.",
        )
    if cloud_cell.cloud_mode_enabled():
        raise OpError(
            "VAULT_STORE_CLOUD_CELL", "a Cloud cell does not enter store mode with this command"
        )
    availability.require_supported_lease(writer_lease.LeaseConfig.from_env())
    if not connection.store_path(vault_root).is_file():
        raise OpError(
            "VAULT_STORE_NEEDS_COLLECTION_STORE",
            "this vault has no collection store, and store mode never creates one",
            "Run `exomem collections migrate` or create a summary collection first.",
        )
    if mode.store_mode_active(vault_root):
        raise OpError("VAULT_STORE_ALREADY_ACTIVE", "this vault is already in store mode")
    _require_sidecars(vault_root)
    skipped: list[ownership.Skipped] = []
    owned = Counter(
        file.ownership for file in ownership.iter_owned(vault_root, on_skip=skipped.append)
    )
    return {
        "pages": owned[ownership.Ownership.PAGE],
        "binaries": owned[ownership.Ownership.BINARY],
        "skipped": _skipped_report(skipped),
    }


def _skipped_report(skipped: list[ownership.Skipped]) -> dict[str, int]:
    return dict(Counter(item.reason.value for item in skipped))


def _require_sidecars(vault_root: Path) -> None:
    sidecars = (
        ("lexical", lexstore.lexical_path(vault_root), "meta", lexstore.SCHEMA_VERSION),
        (
            "graph",
            epistemic_graph.sidecar_path(vault_root),
            "graph_meta",
            epistemic_graph.SCHEMA_VERSION,
        ),
        ("references", memory_refs.sidecar_path(vault_root), "ref_meta", memory_refs.SCHEMA_VERSION),
    )
    for label, path, table, wanted in sidecars:
        found = _stamped_version(path, table)
        if found != str(wanted):
            raise OpError(
                "VAULT_STORE_SIDECAR_NOT_READY",
                f"the {label} index is {'missing' if found is None else f'at version {found}'}, not at version {wanted}",
                "Start the service once so that it builds the indexes, stop it, then run this command again.",
            )


def _stamped_version(path: Path, table: str) -> str | None:
    if not path.is_file():
        return None
    try:
        with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as sidecar:
            row = sidecar.execute(
                f"SELECT value FROM {table} WHERE key = 'schema_version'"
            ).fetchone()
    except sqlite3.Error:
        return None
    return None if row is None else str(row[0])


def dry_run(vault_root: Path) -> dict:
    """What a migration would do, from the preflight alone: no lease, no staging and nothing written."""
    return {"status": "dry-run", **preflight(Path(vault_root).resolve())}


def apply_route(vault_root: Path, *, why: str) -> dict:
    """Move the vault into store mode as the offline owner, or refuse and leave file mode as it was."""
    if not isinstance(why, str) or not why.strip():
        raise ValueError("a store-mode migration needs a reason")
    root = Path(vault_root).resolve()
    preflight(root)
    result = admission.store_mode_route(
        root,
        "store-mode migrate",
        lambda session, token, writer: _cutover(root, session, token, writer, why=why),
    )
    # What the replica says once the hand-back ran: "store" means that a file-mode start on another
    # state root now refuses.
    return {**result, "vault_marker": marker.read_replica(root).state}


def _cutover(root: Path, session, token, writer, *, why: str) -> dict:
    """Stage, verify, then install under the lease; the report carries each phase's seconds."""
    state_dir = state_paths.ensure_vault_state_dir(root)
    remove_stale_staging(root)
    if authority.pending_create(writer.connection) is not None:
        raise OpError(
            "VAULT_STORE_COLLECTION_OPERATION_PENDING",
            "a collection store operation is still pending; finish it before entering store mode",
        )
    revision = _next_revision(writer.connection)
    # An activation that died before its commit left the id and no mode row; this run records the id again and goes on.
    resumed = state_migration.VAULT_STORE_COMPATIBILITY_ID in (
        state_migration.recorded_descriptor_ids(root) or ()
    )
    timings: dict[str, float] = {}
    with tempfile.TemporaryDirectory(prefix=STAGING_PREFIX, dir=state_dir) as scratch:
        work = Path(scratch)
        with connection.staging_store(work / "staged.sqlite") as conn:
            staged = stage(root, conn, revision=revision)
            timings.update(staged.timings)
            _refuse_partial_walk(staged)
            started = time.perf_counter()
            comparison, unprojected = verify_staged(root, conn, work)
            timings["verify_s"] = time.perf_counter() - started
        if comparison.differences or unprojected:
            raise OpError(
                "VAULT_STORE_VERIFICATION_FAILED",
                "the staged rows do not equal what the files derive; nothing was installed. "
                + "; ".join(difference.describe() for difference in comparison.differences)
                + (
                    f"; {len(unprojected)} page(s) do not project to their bytes, e.g. {', '.join(unprojected[:5])}"
                    if unprojected
                    else ""
                ),
                details={
                    "tables": [difference.describe() for difference in comparison.differences],
                    "unprojected": unprojected[: verify.KEYS_REPORTED],
                },
            )
        started = time.perf_counter()
        _install(
            root, session, token, writer, work / "staged.sqlite", staged, revision=revision, why=why
        )
        timings["install_s"] = time.perf_counter() - started
    return {
        "status": "migrated",
        "resumed_interrupted_activation": resumed,
        "revision": revision,
        "pages": staged.pages,
        "binaries": staged.binaries,
        "skipped": _skipped_report(staged.skipped),
        "verified_tables": len(comparison.tables),
        "timings": {name: round(seconds, 2) for name, seconds in timings.items()},
    }


def remove_stale_staging(root: Path) -> None:
    """Remove the private directories that a run killed in the middle left in the vault's state directory."""
    for leftover in state_paths.ensure_vault_state_dir(root).glob(f"{STAGING_PREFIX}*"):
        shutil.rmtree(leftover, ignore_errors=True)


def _next_revision(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT revision FROM store_mode WHERE id = 1").fetchone()
    return 1 if row is None else int(row[0]) + 1


def _refuse_partial_walk(staged: Staged) -> None:
    unreadable = [
        item.rel for item in staged.skipped if item.reason is ownership.SkipReason.UNREADABLE
    ]
    if unreadable:
        raise OpError(
            "VAULT_STORE_WALK_INCOMPLETE",
            f"{len(unreadable)} path(s) could not be read, so the store would miss files, e.g. {', '.join(unreadable[:5])}",
            "Make them readable, then run this command again.",
        )


def _install(
    root: Path,
    session,
    token,
    writer,
    staged_path: Path,
    staged: Staged,
    *,
    revision: int,
    why: str,
) -> None:
    """Install the staged rows in one writer transaction; the vault state records the mode id before it commits.

    A refusal or a kill before the commit leaves the store without its mode row, which is file mode, whatever
    the id says. A failure that leaves no store-mode row also drops the id, so the owner sees a clean refusal.
    """
    authority_check = lambda: session.require(token)  # noqa: E731 - the one closure both id calls share
    state_migration.record_vault_store_compatibility(
        root, authority_check=authority_check, active=True
    )
    try:
        with closing(sqlite3.connect(f"{staged_path.as_uri()}?mode=ro", uri=True)) as source:
            with writer.handle.transaction() as conn:
                _install_rows(root, conn, source, staged, revision=revision, why=why)
    except BaseException:
        if not _store_row_says_store(writer.connection):
            with suppress(Exception):
                state_migration.record_vault_store_compatibility(
                    root, authority_check=authority_check, active=False
                )
        raise


def _install_rows(
    root: Path,
    conn: sqlite3.Connection,
    source: sqlite3.Connection,
    staged: Staged,
    *,
    revision: int,
    why: str,
) -> None:
    present = sorted(set(schema_objects(conn)) & set(staged.objects))
    if present:
        raise OpError(
            "VAULT_STORE_OBJECTS_EXIST",
            f"the store already holds {len(present)} of the tables store mode creates, e.g. {', '.join(present[:5])}",
        )
    lexical, graph, refs = create_owner_schema(conn)
    if {**lexical, **graph, **refs} != staged.objects:
        raise OpError(
            "VAULT_STORE_SCHEMA_DRIFT",
            "the owners' schema differs from the one that was staged and proved",
        )

    def copy() -> None:
        for table in (
            *staged.lexical_tables,
            *staged.graph_tables,
            *staged.refs_tables,
            "store_pages",
            "store_binaries",
        ):
            _copy_table(conn, source, table)

    _copy_rows_without_triggers(conn, staged, copy)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    pages = _recheck_files(root, source)
    conn.executemany(
        "INSERT INTO store_manifest(path, sha256, size, mtime_ns, revision) VALUES (?, ?, ?, ?, ?)",
        [(rel, fact.sha256, fact.size, fact.mtime_ns, revision) for rel, fact in pages],
    )
    # The page's mtime is the manifest's: both come from the recheck, which is the last read before the commit.
    conn.execute(
        "UPDATE store_pages SET mtime_ns = "
        "(SELECT mtime_ns FROM store_manifest WHERE store_manifest.path = store_pages.path)"
    )
    conn.executemany(
        "INSERT INTO store_mode_objects(name, type) VALUES (?, ?)", list(staged.objects.items())
    )
    conn.execute(
        "INSERT INTO store_mode(id, mode, revision, changed_at, release) VALUES (1, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET mode = excluded.mode, revision = excluded.revision, "
        "changed_at = excluded.changed_at, release = excluded.release",
        (mode.STORE_MODE, revision, now, __version__),
    )
    conn.execute(
        "INSERT INTO store_audit(revision, path, operation, origin, sha256, reason, at) "
        "VALUES (?, NULL, 'migrate', ?, NULL, ?, ?)",
        (revision, Origin.MIGRATION.value, why, now),
    )
    # The vault-side marker commits with the mode row, so no replica says store for a file vault.
    marker.record(conn)
    rederive.record_derived_from_files(conn, revision)


def _recheck_files(root: Path, source: sqlite3.Connection) -> list[tuple[str, FileFact]]:
    """Read every owned file again and refuse when any differs from the staged bytes; return the pages' facts."""
    stored = dict(
        source.execute(
            "SELECT path, sha256 FROM store_pages UNION ALL SELECT path, sha256 FROM store_binaries"
        )
    )
    changed, seen, pages = [], set(), []
    for owned, fact, _ in snapshot(root):
        seen.add(owned.rel)
        if stored.get(owned.rel) != fact.sha256:
            changed.append(owned.rel)
        elif owned.ownership is ownership.Ownership.PAGE:
            pages.append((owned.rel, fact))
    changed.extend(sorted(set(stored) - seen))
    if changed:
        raise OpError(
            "VAULT_STORE_FILES_CHANGED",
            f"{len(changed)} file(s) changed after they were read, so nothing was installed, e.g. "
            f"{', '.join(changed[: verify.KEYS_REPORTED])}",
            "Run this command again once nothing writes to the vault.",
        )
    return pages


def _store_row_says_store(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute("SELECT mode FROM store_mode WHERE id = 1").fetchone()
    except sqlite3.Error:
        return False
    return row is not None and row[0] == mode.STORE_MODE
