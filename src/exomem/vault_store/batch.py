"""A governed batch write on a store-mode vault (OpenSpec make-vault-store-canonical, task 2.2).

``commit_batch`` is the store branch of ``vault.batch_atomic_write``. It runs on the store thread,
which is the only writer of the store, and it writes no Markdown for a page: the pages of the
batch commit with their lexical, graph and identity rows in one store transaction
(``write.commit``). A file that the store holds as a binary, or does not hold at all (a Markdown
file outside the Knowledge Base folder, a machine-local file), is published to disk through the
file-mode machinery, and the store commit runs inside that batch's rollback window as its last
step: it records the binary or derives the rows of the file, and a failure of any part restores the
published files. Only what lives outside the store runs after the commit: the free-text census
note, the semantic drain's wake and the heat projection.
"""

from __future__ import annotations

import enum
import hashlib
import logging
import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import call_spans, mode, vault
from ..cli_ops import OpError
from ..collection_store.writer import CollectionWriter
from . import ownership, read
from . import write as store_write
from .write import (
    BinaryDelete,
    BinaryMove,
    BinaryRecord,
    Expect,
    Origin,
    PageDelete,
    PageMove,
    PagePut,
    StoreChange,
    StoreConflict,
)

log = logging.getLogger(__name__)

DEFERRED_GRAPH_UNSUPPORTED = "VAULT_STORE_DEFERRED_GRAPH_UNSUPPORTED"
VOCABULARY_AUTHORITY_UNSUPPORTED = "VAULT_STORE_VOCABULARY_AUTHORITY_UNSUPPORTED"

#: The three things that hold a page's expected state, in the order file mode raises their errors.
_HASH, _GUARD, _CREATE_ONLY = 0, 1, 2


@dataclass(frozen=True, slots=True)
class _Held:
    """What one page must still be when the commit runs, and the file-mode error otherwise."""

    rel: str
    path: Path
    expect: Expect
    kind: int


@dataclass(frozen=True, slots=True)
class Lifecycle:
    """The page moves and removals that delete, move and recover commit with their batch's writes.

    They join the batch's one store transaction, so a page never leaves its path without the rows
    and the other pages that go with it. ``expect`` holds what each named path must still be when
    the commit runs: a stale source refuses the whole commit with its ``StoreConflict``.

    ``binary_moves`` move binary records whose files the writer renames right after the commit
    (``binaries.relocated``). ``vacated_folders`` are the folders that a folder trash or recovery
    moves away as a whole (``StoreChange.vacated_folders``).
    """

    moves: tuple[PageMove, ...] = ()
    deletes: tuple[PageDelete, ...] = ()
    binary_deletes: tuple[BinaryDelete, ...] = ()
    binary_moves: tuple[BinaryMove, ...] = ()
    expect: Mapping[str, Expect] = field(default_factory=dict)
    vacated_folders: tuple[str, ...] = ()


class NoStoreChange(ValueError):
    """A batch names only files that the store neither holds nor derives rows for.

    A curation run's JSON file is one: the store has nothing to commit for it, and refuses an
    empty change. ``commit_batch`` raises this before it does anything, and
    ``vault.batch_atomic_write`` commits such a batch through the native locked writer, as in a
    file-mode vault. It is a ``ValueError`` as the store's own refusal of an empty change is.
    """


class _Holder(enum.Enum):
    """Who holds a batch file: the store (a page, a binary), its row refresh (Markdown) or no one."""

    PAGE = enum.auto()
    BINARY = enum.auto()
    REFRESH = enum.auto()
    FILE = enum.auto()


def _holder_of(rel: str | None) -> _Holder:
    """The holder of a vault-relative path (None: outside the vault). The one rule for what gives
    the store a change: ``commit_batch`` and its plan both read it."""
    if rel is None:
        return _Holder.FILE
    kind = ownership.classify(rel)
    if kind is ownership.Ownership.PAGE:
        return _Holder.PAGE
    if kind is ownership.Ownership.BINARY:
        return _Holder.BINARY
    # A Markdown suffix names the format, so it fixes which files carry rows.
    return _Holder.REFRESH if ownership.is_markdown(rel) else _Holder.FILE


def commit_batch(
    writer: CollectionWriter,
    writes: Iterable[vault.PlannedWrite],
    *,
    vault_root: Path,
    required_guards: Iterable[vault.PathGuard | vault.DirectoryCensusGuard] = (),
    completion_guards: Iterable[vault.PathGuard] = (),
    index_reports: list[Any] | None = None,
    post_commit_fanout: bool = True,
    commit_point: bool = True,
    defer_graph_completion: bool = False,
    vocabulary_auxiliaries: Any | None = None,
    validate_prepared_bindings: Callable[[], None] | None = None,
    prepare: Callable[[list[vault.PlannedWrite]], None] | None = None,
    lifecycle: Lifecycle | None = None,
    retained_original: vault.PathGuard | None = None,
    publish_originals: Callable[[], None] | None = None,
    file_commit_witness: vault._BatchFileWitness | None = None,
) -> list[Path]:
    """Commit one batch to the store of ``vault_root`` in place of the file-mode flip.

    It returns the batch's paths, as the file-mode writer does. A stale ``expected_hash``, a taken
    ``create_only`` path and a stale guard raise the error that file mode raises, so each caller's
    handling of them holds. The transaction derives each page's parent state from the page's own
    bytes, so the caller's ``semantic_states`` are not taken.

    ``lifecycle`` adds a delete, move or recover's page moves and removals to the same transaction.
    A stale expectation of it raises the ``StoreConflict`` itself, which the lifecycle writer maps
    to its own error, since file mode has no such guard to name.
    """
    root = Path(vault_root)
    caller_writes = list(writes)
    if (
        lifecycle is None
        and caller_writes
        and all(_holder_of(_relative(root, write.path)) is _Holder.FILE for write in caller_writes)
    ):
        raise NoStoreChange("a batch of files that the store does not hold has no store change")
    if defer_graph_completion:
        # The handoff completes a graph checkpoint in the external state root, which a store commit
        # never writes: its graph rows are complete at the commit, so there is nothing to defer.
        raise OpError(
            DEFERRED_GRAPH_UNSUPPORTED,
            "a store-mode vault's graph rows commit with their pages, so there is no graph "
            "completion to defer",
        )
    _require_v1_authority(root)
    from .. import curation_witness, semantic_drain, vocabulary_gate, working_set_heat
    from ..governance import artifact_census

    if prepare is not None:
        with vocabulary_gate.preparation_context(root):
            _stop_at_preparation(
                root,
                caller_writes,
                required_guards,
                completion_guards,
                vocabulary_auxiliaries,
                prepare,
            )
    if not caller_writes and lifecycle is None:
        return []
    from .. import trash_metadata

    caller_writes = trash_metadata.prepare_binding_changes(root, caller_writes)
    # A lifecycle commit with no page to write carries no curation witness: nothing to witness.
    augmented, witness = (
        curation_witness.augment_batch(
            caller_writes, vault_root=root, planned_write=vault.PlannedWrite
        )
        if caller_writes
        else ([], None)
    )
    heat = working_set_heat.begin_commit(root, (write.path for write in caller_writes))
    try:
        plan = _commit(
            writer,
            root,
            augmented,
            required_guards,
            completion_guards,
            vocabulary_auxiliaries,
            validate_prepared_bindings,
            commit_point,
            vault._owner_writes(publish_originals, curation_witness.publication_hook(witness)),
            lifecycle,
            retained_original,
            file_commit_witness,
        )
    except BaseException:
        working_set_heat.abandon_commit(heat)
        raise
    # The commit is durable and write.commit's transaction is closed, so wake the semantic drain
    # for the audit rows now, not at its idle poll. It runs for a caller that defers its fan-out
    # too, which `_after_commit` does not reach. A hint creates no worker.
    semantic_drain.signal(root)
    with call_spans.span("commit_tail.census"):
        # Before the write acknowledges, so the free-text census names the pages that no file holds.
        artifact_census.note_created(root, [write.path for _rel, write in plan.pages])
    observed = working_set_heat.observe_commit(heat)
    curation_witness.mark_consumed(witness)
    working_set_heat.persist_commit(observed)
    if post_commit_fanout:
        _after_commit(plan, index_reports)
        written = [*(rel for rel, _write in plan.pages), *plan.refresh, *_lifecycle_paths(lifecycle)]
        rels = list(dict.fromkeys(written))
        if rels:
            # After the request's own drain, which the commit queued first: the acknowledgement says
            # whether the rows were derived when the write returned, over every path that the
            # commit wrote, moved or removed, so that it speaks for each report the writer made.
            from .. import writer_lease

            writer_lease.defer_until_terminal_persisted(lambda: _derived_report(root, rels))
    return [write.path for write in caller_writes]


def _require_v1_authority(root: Path) -> None:
    """Refuse a vault whose vocabulary authority is active: it decides a batch from staged images.

    A store commit stages no images, so it cannot carry the authority's decision. Refusing is the
    fail-closed answer, and it stays until the authority reads a store batch.
    """
    from .. import vocabulary_authority

    if vocabulary_authority.VocabularyAuthority(root.resolve()).runtime_status().mode != "v1":
        raise OpError(
            VOCABULARY_AUTHORITY_UNSUPPORTED,
            "this vault's vocabulary authority is active, and a store-mode write cannot yet carry "
            "its decision",
        )


class _Plan:
    """A batch sorted by holder: the pages that the store commits and the files that it does not."""

    def __init__(
        self,
        root: Path,
        writes: list[vault.PlannedWrite],
        required_guards: Iterable[vault.PathGuard | vault.DirectoryCensusGuard],
        completion_guards: Iterable[vault.PathGuard],
        lifecycle: Lifecycle | None = None,
    ) -> None:
        self.root = root
        self.lifecycle = lifecycle or Lifecycle()
        self.pages: list[tuple[str, vault.PlannedWrite]] = []
        self.files: list[vault.PlannedWrite] = []
        self.binaries: list[tuple[str, vault.PlannedWrite]] = []
        self.refresh: list[str] = []
        for write in writes:
            rel = _relative(root, write.path)
            holder = _holder_of(rel)
            if holder is _Holder.PAGE:
                self.pages.append((rel, write))
                continue
            self.files.append(write)
            if holder is _Holder.BINARY:
                self.binaries.append((rel, write))
            elif holder is _Holder.REFRESH:
                self.refresh.append(rel)
        self.disk_guards: list[vault.PathGuard] = []
        self.disk_censuses: list[vault.DirectoryCensusGuard] = []
        self.disk_completion: list[vault.PathGuard] = []
        self.held: list[_Held] = []
        for rel, write in self.pages:
            self._hold_write(rel, write)
        for guard in required_guards:
            self._hold_guard(guard, self.disk_guards)
        for guard in completion_guards:
            self._hold_guard(guard, self.disk_completion)
        self.held.sort(key=lambda held: held.kind)

    def _hold_write(self, rel: str, write: vault.PlannedWrite) -> None:
        prepared = isinstance(write.content, vault.PreparedBinaryContent)
        if prepared and (
            not write.create_only or write.expected_hash != vault.MISSING_CONTENT_HASH
        ):
            raise vault.PathGuardError(
                "PATH_GUARD_INVALID", "binary batch content requires an exact missing destination"
            )
        if write.expected_hash is not None and not prepared:
            expect = (
                Expect(absent=True)
                if write.expected_hash == vault.MISSING_CONTENT_HASH
                else Expect(sha256=write.expected_hash)
            )
            self.held.append(_Held(rel, write.path, expect, _HASH))
        if write.guard is not None:
            if write.guard.target != rel:
                raise vault.PathGuardError(
                    "PATH_GUARD_TARGET", "write path does not match guard target"
                )
            self.held.append(_Held(rel, write.path, _guard_expect(write.guard), _GUARD))
        if write.create_only:
            self.held.append(_Held(rel, write.path, Expect(absent=True), _CREATE_ONLY))

    def _hold_guard(
        self, guard: vault.PathGuard | vault.DirectoryCensusGuard, disk: list[vault.PathGuard]
    ) -> None:
        if isinstance(guard, vault.DirectoryCensusGuard):
            # A census of a folder that the store owns lists the projection, which trails the store,
            # and the store thread serializes writers; the held expectation of each exact path binds
            # a page, so that census adds nothing. Any other folder holds files that this batch
            # writes on disk, so its census still binds.
            if not ownership.owns_folder(guard.target):
                self.disk_censuses.append(guard)
            return
        if not isinstance(guard, vault.PathGuard):
            raise vault.PathGuardError("PATH_GUARD_INVALID", "unsupported required guard")
        if ownership.classify(guard.target) is ownership.Ownership.PAGE:
            path = self.root / guard.target
            self.held.append(_Held(guard.target, path, _guard_expect(guard), _GUARD))
        else:
            disk.append(guard)

    def expectations(self) -> dict[str, Expect]:
        """One expectation for each held path, for the commit to bind inside its transaction."""
        merged: dict[str, Expect] = {}
        for held in self.held:
            have = merged.get(held.rel)
            # Absence is the narrowest claim, then a hash; a bare "page exists" gives way to either.
            # Two claims that cannot both hold were already refused by the check before the commit.
            if have is None or held.expect.absent or have == Expect():
                merged[held.rel] = held.expect
        return {**merged, **self.lifecycle.expect}


def _relative(root: Path, path: Path) -> str | None:
    """The vault-relative POSIX path of ``path``, or None for a file outside the vault."""
    try:
        return Path(os.path.abspath(path)).relative_to(Path(os.path.abspath(root))).as_posix()
    except ValueError:
        return None


def _guard_expect(guard: vault.PathGuard) -> Expect:
    """What a guard of a stored page says the store holds: nothing, bytes of one hash, or a page.

    A guard that was captured from the disk says what the projection held then, which the store may
    be ahead of; a mismatch refuses the write, and the caller reads the page again from the store.
    """
    if guard.expects_absence:
        return Expect(absent=True)
    if guard.binds_bytes:
        return Expect(sha256=guard.expected_content_hash)
    return Expect()  # "stable" and "generation" bind a file's identity: the page must exist


def _check_held(plan: _Plan) -> None:
    """Refuse, before any file is published, a page that is not what the batch expects.

    Each failure raises the error that file mode raises for the same stale write: callers retry a
    taken name (``CreateOnlyConflict``), report a changed page (``ContentHashMismatchError``) and
    read again after a stale guard (``PathGuardError``); the store's own ``StoreConflict`` reaches
    none of them.
    """
    with read.snapshot(plan.root) as view:
        # A lifecycle source is checked as the commit checks it; its conflict reaches the writer.
        store_write.check(view.connection, plan.lifecycle.expect)
        for held in plan.held:
            try:
                store_write.check(view.connection, {held.rel: held.expect})
            except StoreConflict as conflict:
                stored = view.page_meta(held.rel)
                actual = None if stored is None else stored.sha256
                if held.kind == _HASH:
                    raise vault.ContentHashMismatchError(
                        held.path, held.expect.sha256 or vault.MISSING_CONTENT_HASH, actual
                    ) from conflict
                if held.kind == _CREATE_ONLY:
                    raise vault.CreateOnlyConflict(held.rel) from conflict
                # The codes that ``PathGuard`` gives a stored page, which callers branch on.
                changed = held.expect.sha256 and actual
                code = "PATH_GUARD_CONTENT" if changed else "PATH_GUARD_CHANGED"
                raise vault.PathGuardError(code, "guarded content changed") from conflict


def _validate(
    root: Path, writes: list[vault.PlannedWrite], vocabulary_auxiliaries: Any | None, *, final: bool
) -> None:
    """The access gates that a file-mode batch passes before staging and again before its writes."""
    if final:
        vault._validate_batch_write_access(
            root, writes, proposed=writes, auxiliary_manifest=vocabulary_auxiliaries
        )
    else:
        vault._validate_batch_write_access(
            root, writes, auxiliary_manifest=vocabulary_auxiliaries, defer_registry=True
        )


def _stop_at_preparation(
    root: Path,
    writes: list[vault.PlannedWrite],
    required_guards: Iterable[vault.PathGuard | vault.DirectoryCensusGuard],
    completion_guards: Iterable[vault.PathGuard],
    vocabulary_auxiliaries: Any | None,
    prepare: Callable[[list[vault.PlannedWrite]], None],
) -> None:
    """Native preparation: judge the batch as a commit would, run the preparer, write nothing."""
    vault._require_distinct_destinations(writes)
    _validate(root, writes, vocabulary_auxiliaries, final=False)
    _check_held(_Plan(root, writes, required_guards, completion_guards))
    prepare(writes)
    raise ValueError("native preparation must stop before publication")


def _commit(
    writer: CollectionWriter,
    root: Path,
    writes: list[vault.PlannedWrite],
    required_guards: Iterable[vault.PathGuard | vault.DirectoryCensusGuard],
    completion_guards: Iterable[vault.PathGuard],
    vocabulary_auxiliaries: Any | None,
    validate_prepared_bindings: Callable[[], None] | None,
    commit_point: bool,
    before_publication: Callable[[], None] | None,
    lifecycle: Lifecycle | None = None,
    retained_original: vault.PathGuard | None = None,
    file_commit_witness: vault._BatchFileWitness | None = None,
) -> _Plan:
    from ..writer_lease import (
        log_active_mutation_phase,
        mark_active_mutation_committed,
        validate_active_write_fence,
    )

    vault._require_distinct_destinations(writes)
    _validate(root, writes, vocabulary_auxiliaries, final=False)
    plan = _Plan(root, writes, required_guards, completion_guards, lifecycle)
    if retained_original is not None:
        from .. import vocabulary_auxiliaries as auxiliaries

        manifests = (
            vocabulary_auxiliaries.entries
            if isinstance(vocabulary_auxiliaries, auxiliaries.DerivedAuxiliaryManifest) else ()
        )
        retained = [
            write for rel, write in plan.binaries
            if rel == retained_original.target and isinstance(write.content, vault.PreparedBinaryContent)
            and retained_original.expected_content_hash == write.content.sha256
            and retained_original.expected_content_size == write.content.size
        ]
        original_bound = any(
            entry.role == auxiliaries.SOURCE_ORIGINAL and entry.path == retained_original.target
            and entry.after_sha256 == retained_original.expected_content_hash
            for entry in manifests
        )
        is_import = any(entry.role == auxiliaries.SOURCE_IMPORT_RECEIPT for entry in manifests)
        if len(retained) != 1 or not original_bound or not is_import or retained_original.leaf_policy != "content":
            raise ValueError("retained original differs from the sealed Source import")
        plan.files.remove(retained[0])
        plan.disk_completion.append(retained_original)
    # Before any file is published, so a stale expectation leaves no file behind. The commit checks
    # again inside its transaction, and that check is the one that binds.
    _check_held(plan)
    if validate_prepared_bindings is not None:
        validate_prepared_bindings()
    validate_active_write_fence()
    log_active_mutation_phase("canonical_commit_started", affected_count=len(writes))
    if file_commit_witness is None and before_publication is not None:
        # The owner's own durable write; the commit does not undo it.
        before_publication()

    def commit_to_store() -> None:
        # Everything that can still fail runs here: the file-mode batch calls this as its last step
        # before its commit point, so a failure restores the published files.
        if validate_prepared_bindings is not None:
            validate_prepared_bindings()
        _validate(root, writes, vocabulary_auxiliaries, final=True)
        change = StoreChange(
            origin=Origin.GOVERNED,
            reason=_reason(),
            puts=tuple(PagePut(rel, _page_bytes(write), media_allocation=write.media_allocation)
                       for rel, write in plan.pages),
            deletes=plan.lifecycle.deletes,
            moves=plan.lifecycle.moves,
            binaries=tuple(_record(rel, write) for rel, write in plan.binaries),
            binary_deletes=plan.lifecycle.binary_deletes,
            binary_moves=plan.lifecycle.binary_moves,
            vacated_folders=plan.lifecycle.vacated_folders,
            refresh=tuple(plan.refresh),
            expect=plan.expectations(),
        )
        try:
            store_write.commit(writer, change)
        except StoreConflict as conflict:
            if conflict.path not in change.expect or conflict.path in plan.lifecycle.expect:
                raise
            # The store changed after the check above ran: the batch is stale as a guard says it is.
            raise vault.PathGuardError(
                "PATH_GUARD_CHANGED", "a stored page changed before the commit"
            ) from conflict

    if plan.files:
        with vault._BATCH_COMMIT_LOCK:
            vault._batch_atomic_write_locked(
                plan.files,
                vault_root=root,
                required_guards=[*plan.disk_guards, *plan.disk_censuses],
                completion_guards=plan.disk_completion,
                post_commit_fanout=False,
                commit_point=False,
                # The access gates above judged the whole batch with its manifest, which a part of
                # the batch cannot be judged against alone.
                _vocabulary_auxiliaries=None,
                # A store commit derives its graph rows itself; the epoch is file-mode graph sync.
                _graph_epoch=False,
                _before_commit_point=commit_to_store,
                _before_first_install=before_publication if file_commit_witness is not None else None,
                _file_commit_witness=file_commit_witness,
            )
    else:
        vault.recheck_path_guards(root, (*plan.disk_guards, *plan.disk_completion))
        for census in plan.disk_censuses:
            census.recheck(root)
        if file_commit_witness is None or before_publication is None:
            commit_to_store()
        else:
            try:
                file_commit_witness.begin()
                before_publication()
                commit_to_store()
            except Exception:
                file_commit_witness.retire_rolled_back()
                raise
            file_commit_witness.seal()
    log_active_mutation_phase("canonical_files_committed", affected_count=len(writes))
    call_spans.mark("canonical_files_committed")
    if commit_point:
        mark_active_mutation_committed()
    return plan


def _reason() -> str:
    from ..writer_lease import active_mutation_trace

    trace = active_mutation_trace()
    return "governed write" if trace is None else f"governed write: {trace[1]}"


def _page_bytes(write: vault.PlannedWrite) -> bytes:
    content = write.content
    if isinstance(content, str):
        return content.encode("utf-8")
    # A preserved Markdown artifact arrives as a stream with its exact identity.
    position = content.stream.tell()
    try:
        raw = content.stream.read(content.size + 1)
    finally:
        content.stream.seek(position)
    if len(raw) != content.size or hashlib.sha256(raw).hexdigest() != content.sha256:
        raise vault.PathGuardError(
            "PATH_GUARD_INVALID", "binary batch content does not match its identity"
        )
    return raw


def _record(rel: str, write: vault.PlannedWrite) -> BinaryRecord:
    content = write.content
    if isinstance(content, vault.PreparedBinaryContent):
        return BinaryRecord(rel, content.sha256, content.size)
    raw = content.encode("utf-8")
    return BinaryRecord(rel, hashlib.sha256(raw).hexdigest(), len(raw))


def _row_components(root: Path, rels: Iterable[str]) -> list[Any]:
    """The outcomes of the identity, lexical and graph rows of ``rels``, from the derivation queue.

    The commit queues them and the writing request derives them after it releases the vault boundary
    (``derivation``), so a report made at the commit names them deferred and one made after that drain
    names what it left. A queue that cannot be read leaves them deferred: never a completion unseen.
    """
    from .. import index_sync
    from ..collection_store.preview import bound_writer
    from . import derivation

    writer = bound_writer(Path(root).resolve())
    state = ("deferred", "store_derivation_unread")
    if writer is not None:
        try:
            state = derivation.outcome(writer.connection, list(dict.fromkeys(rels)))
        except Exception:  # noqa: BLE001 - an unread queue is reported as such, and the write stands
            log.debug("the derivation queue could not be read for a report", exc_info=True)
    return [
        index_sync.IndexComponentOutcome(name, *state)
        for name in ("memory_refs", "lexstore", "epistemic_graph")
    ]


def row_report(operation: str, paths: Iterable[str], *, root: Path) -> Any:
    """The fan-out report of a lifecycle commit: where its lexical, graph and identity rows stand.

    It names no embeddings outcome: the vector sidecar follows the store's audit rows after the
    commit, and a report must not say that work is done which no code has done yet.
    """
    from .. import index_sync

    named = list(paths)
    requested, truncated = index_sync._bounded_paths(named)
    return index_sync.IndexSyncReport(
        operation, requested, requested, tuple(_row_components(root, named)), truncated
    )


def _lifecycle_paths(lifecycle: Lifecycle | None) -> list[str]:
    """Every path that a lifecycle commit moves a page or binary from or to, or removes."""
    if lifecycle is None:
        return []
    return [
        *(path for move in (*lifecycle.moves, *lifecycle.binary_moves) for path in (move.src, move.dst)),
        *(delete.rel for delete in (*lifecycle.deletes, *lifecycle.binary_deletes)),
    ]


def _derived_report(root: Path, rels: list[str]) -> list[Any]:
    """The report that a governed write acknowledges with, once its request derived its rows."""
    from .. import index_sync

    requested, truncated = index_sync._bounded_paths(rels)
    return [index_sync.IndexSyncReport(
        "upsert", requested, requested, tuple(_row_components(root, rels)), truncated
    )]


def _after_commit(plan: _Plan, index_reports: list[Any] | None) -> None:
    """What lives outside the store, run after the commit: the embeddings report.

    The commit is durable, so this step only reports; it never fails the write.
    """
    from .. import index_sync
    from . import vectors

    rels = [*(rel for rel, _write in plan.pages), *plan.refresh]
    components = _row_components(plan.root, rels)
    if mode.service_profile_enabled() and rels:
        if os.environ.get("EXOMEM_DISABLE_EMBEDDINGS"):
            components.append(
                index_sync.IndexComponentOutcome("embeddings", "accepted", "embeddings_disabled")
            )
        else:
            # No receipt: the drain follows the store's audit rows and `commit_batch` woke it, and a
            # receipt that the store turn ignores would only pile up in the deferred index.
            owed = any(vectors.embeddable(rel) for rel in rels)
            components.append(
                index_sync.IndexComponentOutcome(
                    "embeddings",
                    "deferred" if owed else "accepted",
                    "store_audit" if owed else "no_eligible_paths",
                )
            )
    if index_reports is not None:
        requested, truncated = index_sync._bounded_paths(rels)
        report = index_sync.IndexSyncReport(
            "upsert", requested, requested, tuple(components), truncated
        )
        index_reports.append(report)
