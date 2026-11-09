"""A governed batch write on a store-mode vault (OpenSpec make-vault-store-canonical, task 2.2).

``commit_batch`` is the store branch of ``vault.batch_atomic_write``. It runs on the store thread,
which is the only writer of the store, and it writes no Markdown for a page: the pages of the
batch commit with their lexical, graph and identity rows in one store transaction
(``write.commit``). A file that the store holds as a binary, or does not hold at all (a Markdown
file outside the Knowledge Base folder, a machine-local file), is published to disk through the
file-mode machinery, and the store commit runs inside that batch's rollback window as its last
step: it records the binary or derives the rows of the file, and a failure of any part restores the
published files. Only what lives outside the store runs after the commit: the free-text census
note, the semantic drain's handoff and the heat projection.
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import call_spans, mode, vault
from ..cli_ops import OpError
from ..collection_store.writer import CollectionWriter
from . import ownership, read
from . import write as store_write
from .write import BinaryRecord, Expect, Origin, PagePut, StoreChange, StoreConflict

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
) -> list[Path]:
    """Commit one batch to the store of ``vault_root`` in place of the file-mode flip.

    It returns the batch's paths, as the file-mode writer does. A stale ``expected_hash``, a taken
    ``create_only`` path and a stale guard raise the error that file mode raises, so each caller's
    handling of them holds. The transaction derives each page's parent state from the page's own
    bytes, so the caller's ``semantic_states`` are not taken.
    """
    root = Path(vault_root)
    if defer_graph_completion:
        # The handoff completes a graph checkpoint in the external state root, which a store commit
        # never writes: its graph rows are complete at the commit, so there is nothing to defer.
        raise OpError(
            DEFERRED_GRAPH_UNSUPPORTED,
            "a store-mode vault's graph rows commit with their pages, so there is no graph "
            "completion to defer",
        )
    _require_v1_authority(root)
    from .. import curation_witness, vocabulary_gate, working_set_heat
    from ..governance import artifact_census

    caller_writes = list(writes)
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
    if not caller_writes:
        return []
    augmented, witness = curation_witness.augment_batch(
        caller_writes, vault_root=root, planned_write=vault.PlannedWrite
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
            curation_witness.publication_hook(witness),
        )
    except BaseException:
        working_set_heat.abandon_commit(heat)
        raise
    with call_spans.span("commit_tail.census"):
        # Before the write acknowledges, so the free-text census names the pages that no file holds.
        artifact_census.note_created(root, [write.path for _rel, write in plan.pages])
    observed = working_set_heat.observe_commit(heat)
    curation_witness.mark_consumed(witness)
    working_set_heat.persist_commit(observed)
    if post_commit_fanout:
        _after_commit(root, plan, index_reports)
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
    ) -> None:
        self.root = root
        self.pages: list[tuple[str, vault.PlannedWrite]] = []
        self.files: list[vault.PlannedWrite] = []
        self.binaries: list[tuple[str, vault.PlannedWrite]] = []
        self.refresh: list[str] = []
        for write in writes:
            rel = _relative(root, write.path)
            kind = None if rel is None else ownership.classify(rel)
            if rel is not None and kind is ownership.Ownership.PAGE:
                self.pages.append((rel, write))
                continue
            self.files.append(write)
            if rel is not None and kind is ownership.Ownership.BINARY:
                self.binaries.append((rel, write))
            # A Markdown suffix names the format, so it fixes which files carry rows.
            elif rel is not None and rel.lower().endswith(".md"):
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
        return merged


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
    if guard.leaf_policy == "absent" or (
        guard.leaf_policy == "store" and guard.expected_content_hash is None
    ):
        return Expect(absent=True)
    if guard.leaf_policy in {"store", "content"}:
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
) -> _Plan:
    from ..writer_lease import (
        log_active_mutation_phase,
        mark_active_mutation_committed,
        validate_active_write_fence,
    )

    vault._require_distinct_destinations(writes)
    _validate(root, writes, vocabulary_auxiliaries, final=False)
    plan = _Plan(root, writes, required_guards, completion_guards)
    # Before any file is published, so a stale expectation leaves no file behind. The commit checks
    # again inside its transaction, and that check is the one that binds.
    _check_held(plan)
    if validate_prepared_bindings is not None:
        validate_prepared_bindings()
    validate_active_write_fence()
    log_active_mutation_phase("canonical_commit_started", affected_count=len(writes))
    if before_publication is not None:
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
            puts=tuple(PagePut(rel, _page_bytes(write)) for rel, write in plan.pages),
            binaries=tuple(_record(rel, write) for rel, write in plan.binaries),
            refresh=tuple(plan.refresh),
            expect=plan.expectations(),
        )
        try:
            store_write.commit(writer, change)
        except StoreConflict as conflict:
            if conflict.path not in change.expect:
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
            )
    else:
        vault.recheck_path_guards(root, (*plan.disk_guards, *plan.disk_completion))
        for census in plan.disk_censuses:
            census.recheck(root)
        commit_to_store()
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


def _after_commit(root: Path, plan: _Plan, index_reports: list[Any] | None) -> None:
    """What lives outside the store, run after the commit: the drain's handoff and the report.

    The step is best effort, as the file-mode fan-out's is: the commit is durable, and a handoff
    that fails leaves its own retry state (the drain's receipts) and never fails the write.
    """
    from .. import index_sync, semantic_drain

    rels = [*(rel for rel, _write in plan.pages), *plan.refresh]
    components = [
        index_sync.IndexComponentOutcome(name, "completed", "store_commit")
        for name in ("memory_refs", "lexstore", "epistemic_graph")
    ]
    if mode.service_profile_enabled() and rels:
        if os.environ.get("EXOMEM_DISABLE_EMBEDDINGS"):
            components.append(
                index_sync.IndexComponentOutcome("embeddings", "accepted", "embeddings_disabled")
            )
        else:
            try:
                queued = semantic_drain.request(root, [root / rel for rel in rels], edited=True)
            except Exception:  # noqa: BLE001 - derived work retries from its own receipts
                log.warning("semantic handoff failed after a store commit", exc_info=True)
                components.append(
                    index_sync.IndexComponentOutcome(
                        "embeddings", "degraded", "durable_defer_failed"
                    )
                )
            else:
                components.append(
                    index_sync.IndexComponentOutcome(
                        "embeddings",
                        "deferred" if queued else "accepted",
                        "deferred_durable" if queued else "no_eligible_paths",
                    )
                )
    if index_reports is not None:
        requested, truncated = index_sync._bounded_paths(rels)
        report = index_sync.IndexSyncReport(
            "upsert", requested, requested, tuple(components), truncated
        )
        index_reports.append(report)
