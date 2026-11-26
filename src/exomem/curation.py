"""Governed, agent-authored curation plans and durable run evidence.

The active agent owns every semantic choice.  This module accepts only a
closed typed plan, seals deterministic bindings, and stores enough canonical
evidence to reconstruct progress without a machine-local coordinator.
"""

from __future__ import annotations

import datetime as dt
import functools
import hashlib
import json
import logging
import os
import re
import threading
import unicodedata
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Final, NoReturn

from . import capabilities, held_fs, trash_metadata, vault
from .collection_profiles import PLANNING_PROFILE
from .kbdir import managed_root, managed_split
from .vault import (
    BatchWriteError,
    ContentHashMismatchError,
    CreateOnlyConflict,
    DefiniteNoEffect,
    PathGuard,
    PathGuardError,
    PlannedWrite,
    batch_atomic_write,
    in_append_only_tree,
    read_guarded_bytes,
    read_guarded_text,
)
from .vault import content_hash as text_content_hash

log = logging.getLogger(__name__)

# nosemgrep: ep-word-set -- maintain_memory defines these closed curation action tokens.
CURATION_ACTIONS: Final[tuple[str, ...]] = (
    "work-item",
    "propose",
    "preview",
    "status",
    "apply",
    "resume",
    "propose-compensation",
    "apply-compensation",
    # Reverts one recent promotion in one call (`promotion_revert.py`).
    "revert",
    # Derives and seals the plan that relocates a set of pages (`relocate.py`).
    "relocate",
)
READ_ONLY_ACTIONS: Final[frozenset[str]] = frozenset({"work-item", "preview", "status"})
#: The actions whose caller repeats the identical call until the answer stops asking for it. A
#: repeat of one runs the next steps, so the writer lease must not replay the last reply for it.
# nosemgrep: ep-word-set -- members of the closed curation action enum above, decided per action's code.
CONTINUING_ACTIONS: Final[frozenset[str]] = frozenset({"resume", "relocate"})
#: The actions that hold the vault mutation boundary themselves, one canonical seam at a time
#: (`guards_own_boundary`). `relocate` prepares read-only and seals through `seal_proposal`, as
#: `propose` does. `revert` is absent: it still relies on the command's boundary.
# nosemgrep: ep-word-set -- members of the closed curation action enum above, decided per action's code.
_SELF_GUARDED_ACTIONS: Final[frozenset[str]] = frozenset(
    {"propose", "propose-compensation", "relocate", "apply", "resume", "apply-compensation"}
)
# nosemgrep: ep-word-set -- the closed step kinds this module implements, each with its own handler.
STEP_KINDS: Final[tuple[str, ...]] = (
    "create-note",
    "create-entity",
    "accept-relation",
    "edit",
    "supersede",
    "move",
    "repoint-links",
    "delete",
    "recover",
    "fold-recap-revisions",
    "fold-duplicate",
)
#: The step that rewrites the referrers of a plan's moves once, as its last step.
REPOINT_STEP_KIND: Final[str] = "repoint-links"
#: The private inverses of the relocation steps and of a pointer correction. Like the fold
#: inverses, only a compensation plan that `propose_compensation` derives may carry them: a public
#: kind that writes caller-given bytes would bypass every leaf.
UNMOVE_STEP_KIND: Final[str] = "unmove"
UNREPOINT_STEP_KIND: Final[str] = "unrepoint-links"
REVERT_POINTER_STEP_KIND: Final[str] = "revert-pointer"
# nosemgrep: ep-word-set -- the three private inverse step kinds this module defines, a closed set.
_RELOCATION_INVERSE_KINDS: Final[frozenset[str]] = frozenset(
    {UNMOVE_STEP_KIND, UNREPOINT_STEP_KIND, REVERT_POINTER_STEP_KIND}
)
#: The steps that may bind a page that an earlier step of their plan writes, at that step's exact
#: sealed after hash (`_prepare_manifest`): each runs only after the step it follows.
_CHAINED_STEP_KINDS: Final[frozenset[str]] = frozenset({REPOINT_STEP_KIND, *_RELOCATION_INVERSE_KINDS})
#: The recap fold (add-first-class-duplicate-handling A7): an episode's superseded recap revisions
#: move into compiled history. It is public, and with its undo it is the only step that retires a
#: Source: `move`, `delete` and `recover` keep refusing, because the fold trashes a file only after
#: it re-reads identical bytes held elsewhere (`fold_leaf`), so it needs no relocation lineage.
FOLD_RECAP_STEP_KIND: Final[str] = "fold-recap-revisions"
#: The fold's inverse. It is internal: `STEP_KINDS` lacks it, and only a compensation plan that
#: `propose_compensation` derives from a fold's sealed manifest may carry it.
UNFOLD_RECAP_STEP_KIND: Final[str] = "unfold-recap-revisions"
#: The duplicate fold (add-first-class-duplicate-handling C2): a copy of a Source or Evidence page folds
#: into the canonical page that holds identical bytes. It is public. `move`, `delete` and `supersede`
#: keep refusing Sources and Evidence, and this fold alone retires a copy, after it re-proves the identity.
FOLD_DUPLICATE_STEP_KIND: Final[str] = "fold-duplicate"
#: The duplicate fold's inverse. It is internal, like the recap fold's: only a compensation plan may carry it.
UNFOLD_DUPLICATE_STEP_KIND: Final[str] = "unfold-duplicate"
# nosemgrep: ep-word-set -- the four fold step kinds this module defines, a closed set.
FOLD_STEP_KINDS: Final[frozenset[str]] = frozenset(
    {FOLD_RECAP_STEP_KIND, UNFOLD_RECAP_STEP_KIND, FOLD_DUPLICATE_STEP_KIND, UNFOLD_DUPLICATE_STEP_KIND}
)
#: The step kinds of the duplicate fold. They share one chained plan state (`duplicate_fold.Projection`).
# nosemgrep: ep-word-set -- the two duplicate-fold step kinds this module defines, a closed set.
_DUPLICATE_FOLD_KINDS: Final[frozenset[str]] = frozenset({FOLD_DUPLICATE_STEP_KIND, UNFOLD_DUPLICATE_STEP_KIND})
#: The protected trees a fold may name: the Sources tree its revisions live in, and the Evidence tree.
#: The trash is named too.
_FOLD_TREES: Final[frozenset[str]] = frozenset(name.casefold() for name in vault.APPEND_ONLY_KB_SUBPATHS)
#: The episode Records leaf (close-memory-loop 3.5). It is not a general
#: curation kind: only a plan the episode `records` route seals may carry it
#: (`propose(..., allow_records=True)`, alone in its plan), so no curation door
#: can seal one. A plan the episode sealed is an ordinary curation run, which
#: `maintain_memory` apply/resume can run through the same Records writer and
#: checks `record_memory` applies. It delegates to that writer, the leaf that
#: may touch the protected Records tree, and its witness holds only while that
#: writer's own receipt -- its audit transition -- corroborates it.
RECORDS_STEP_KIND: Final[str] = "append-record"
RECORDS_STEP_KINDS: Final[frozenset[str]] = frozenset({RECORDS_STEP_KIND, "update-record"})
# These are closed native operation tokens, never business vocabulary.
PRESERVATION_STEP_KINDS: Final[frozenset[str]] = frozenset({"capture-source", "preserve-evidence"})
# nosemgrep: ep-word-set -- the closed curation step kinds of the Planning leaves and the native
# Planning action each one runs.
PLANNING_STEP_ACTIONS: Final[Mapping[str, str]] = MappingProxyType(
    {"add-plan": "add", "update-plan": "update", "triage-plan": "triage"}
)
PLANNING_STEP_KINDS: Final[frozenset[str]] = frozenset(PLANNING_STEP_ACTIONS)
COLLECTION_STEP_KINDS = RECORDS_STEP_KINDS | PLANNING_STEP_KINDS
#: Native kinds whose atomic witness alone proves the commit: their missing receipt is repaired from
#: history even after the output changed or disappeared, which current coverage reports on its own.
_NATIVE_STEP_KINDS = PRESERVATION_STEP_KINDS | PLANNING_STEP_KINDS
OBSERVE_UNIT_STEP_KIND: Final[str] = "observe-unit"
#: The revert leaf of `recent_promotions` (promote-referents-without-nudges 2.6):
#: remove the bullets of one typed edge, keyed by subject page, predicate and
#: target. It is internal. No public plan admits it (`STEP_KINDS` lacks it), no
#: tool operation names it, and it takes no hash, so an unrelated concurrent edit
#: of the page survives. Only a plan the promotion revert seals may carry it.
REMOVE_RELATION_STEP_KIND: Final[str] = "remove-relation"
#: The receipt outcomes that record a committed leaf; a `failed` receipt records none.
# nosemgrep: ep-word-set -- the closed receipt outcomes this module writes and reconstructs.
COMMITTED_OUTCOMES: Final[frozenset[str]] = frozenset({"committed", "recovered-committed"})
#: Curation's own recovery-proof reasons for a stopped episode attempt, beside the native
#: `DefiniteNoEffect.DESTINATION_EXISTS`: a store-sealed step whose store holds no transaction, and a
#: native preservation step whose publication record never left intent. Curation owns them, so they
#: live here and not in `vault`.
RECOVERY_STORE_TRANSACTION_ABSENT: Final[str] = "store_transaction_absent"
RECOVERY_PUBLICATION_NOT_STARTED: Final[str] = "publication_not_started"
# The compatibility flag `allow_records` admits only the private leaves: the episode leaves here,
# and `REMOVE_RELATION_STEP_KIND` beside them (it may share a plan with its siblings).
_EPISODE_STEP_KINDS = COLLECTION_STEP_KINDS | PRESERVATION_STEP_KINDS | {OBSERVE_UNIT_STEP_KIND}
#: The step kinds whose leaf holds the vault mutation boundary itself and, under it, rechecks the
#: sealed pre-step state of what it writes (a hash, or a new page's absence), so `_execute_next` runs
#: their leaf outside every hold of its own. A move and its inverse recheck the source's sealed hash
#: in `_dispatch_step`'s before-rename stage. A pointer `edit` and any episode-bound step still run
#: whole under curation's hold.
# nosemgrep: ep-word-set -- closed protocol set over this module's kinds; each member's leaf does that recheck.
SELF_GUARDED_STEP_KINDS: Final[frozenset[str]] = frozenset(
    {"create-note", "edit", OBSERVE_UNIT_STEP_KIND, *COLLECTION_STEP_KINDS, "move", UNMOVE_STEP_KIND}
)
MAX_STEPS: Final[int] = 64
MAX_PLAN_BYTES: Final[int] = 256 * 1024
#: The most bytes one stored run artifact may hold. The seal refuses a plan above it and every read
#: of a run artifact refuses one above it, so a plan that seals always loads again.
MAX_SEALED_BYTES: Final[int] = 4 * MAX_PLAN_BYTES
#: The step cap of a plan that the server derives (`derived_by`). An agent-written plan keeps
#: `MAX_STEPS`; a derived plan's size is bounded by `MAX_SEALED_BYTES`.
MAX_DERIVED_STEPS: Final[int] = 2048
#: The sealed `derived_by` values: the server action that derived a plan's steps.
# nosemgrep: ep-word-set -- the closed set of curation actions that derive a plan, not vocabulary.
DERIVED_BY: Final[frozenset[str]] = frozenset({"relocate"})
#: The rows of each group that a derived plan's summary shows; its counts cover every row.
SUMMARY_SAMPLE: Final[int] = 20
MAX_TITLE_CHARS: Final[int] = 500
MAX_STEP_ID_CHARS: Final[int] = 80
MAX_WHY_CHARS: Final[int] = 500
MAX_WORK_ITEM_PAGES: Final[int] = 12
MAX_WORK_ITEM_CHARS: Final[int] = 24_000

_STEP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HEX24 = re.compile(r"^[0-9a-f]{24}$")
_PLAN_FIELDS = frozenset({"version", "title", "steps", "entity_candidate"})
_STEP_FIELDS = frozenset({"step_id", "kind", "args"})
_SEALED_FIELDS = frozenset({"binding_manifest", "registry_ids", "plan_type", "forward_plan_id", "episode_invocation_version", "derived_by", "approval_summary"})
_ENTITY_CANDIDATE_FIELDS = frozenset(
    {
        "review_ref",
        "review_fingerprint",
        "candidate_state",
        "identity",
        "signal_version",
        "first_disconnected_context_batch",
        "remaining_disconnected_count",
        "batch_fingerprint",
        "target_refs",
        "grammar_identity",
        "registry_identity",
        # Presentation-only fields returned by a hydration recheck.  They are
        # validated here and removed from the immutable plan binding below.
        "executable",
        "closure_only",
        "deferred_remaining_count",
    }
)
_ENTITY_CONTEXT_BINDING_FIELDS = frozenset({"path", "context_hash"})
_ENTITY_GRAMMAR_FIELDS = frozenset({"version", "predicate_table_digest"})

_CREATE_NOTE_FIELDS = frozenset(
    {
        "content",
        "title",
        "slug",
        "note_type",
        "project",
        "projects",
        "sources",
        "tags",
        "status",
        "severity",
        "pattern_type",
        "domain",
        "started",
        "duration",
        "hypothesis",
        "n",
        "concluded",
        "medium",
        "recorded",
        "published",
        "host",
        "editor",
        "bridge_of",
        "bridge_scope",
        "bridge_review",
        "project_category",
        "relation_disposition",
        "relation_review_reason",
        "vocabulary_decision",
    }
)
_CREATE_ENTITY_FIELDS = frozenset(
    {
        "entity_type",
        "name",
        "slug",
        "summary",
        "why_in_kb",
        "tags",
        "connections",
        "affiliation",
        "relationship",
        "domain",
        "language",
        "repo",
        "license",
        "used_in",
        "decided",
        "project",
        "decision_status",
        "identity_decision",
        "facets",
        "relations",
        "aliases",
        "registry_scope",
    }
)
_ACCEPT_RELATION_FIELDS = frozenset({"ref", "expected_hash", "why", "expected_fingerprint"})
# nosemgrep: ep-word-set -- remove_relation defines these closed request fields.
_REMOVE_RELATION_FIELDS = frozenset({"subject", "predicate", "target", "why"})
_APPEND_RECORD_FIELDS = frozenset(
    {"collection", "item", "item_key", "body", "why", "expected_container_hash"}
)
_UPDATE_RECORD_FIELDS = frozenset(
    {"collection", "item_key", "changes", "why", "expected_container_hash", "expected_item_version"}
)
_EDIT_FIELDS = frozenset({"path", "why", "operation"})
# nosemgrep: ep-word-set -- the observe-unit leaf's request fields, fixed by its schema.
_OBSERVE_UNIT_FIELDS = frozenset({"path", "why", "observation"})
_SUPERSEDE_FIELDS = frozenset(
    {
        "old_path",
        "content",
        "title",
        "slug",
        "note_type",
        "reason",
        "project",
        "projects",
        "sources",
        "tags",
        "status",
        "severity",
        "pattern_type",
        "domain",
        "started",
        "duration",
        "hypothesis",
        "n",
        "concluded",
        "medium",
        "recorded",
        "published",
        "host",
        "editor",
        "bridge_of",
        "bridge_scope",
        "bridge_review",
        "project_category",
        "relation_disposition",
        "relation_review_reason",
        "vocabulary_decision",
    }
)
# A move moves bytes and lineage; the plan's `repoint-links` step rewrites links.
# `project_folders` names the folder of a project key that its `fields` patch registers.
_MOVE_FIELDS = frozenset(
    {"old_path", "new_path", "fields", "project_folders", "allow_curated", "promotion_reason"}
)
_DELETE_FIELDS = frozenset(
    {
        "path",
        "confirm",
        "recursive",
        "force_orphan",
        "force_superseded",
        "allow_curated",
        "expected_dead_inbound",
    }
)
_RECOVER_FIELDS = frozenset({"trash_path", "restore_path", "allow_curated"})
# Native leaf fields are a closed wire protocol; original bytes never enter the plan JSON.
_CAPTURE_SOURCE_FIELDS = frozenset({"original", "content", "title", "source_type", "slug", "url", "tags", "why_captured", "domain", "projects", "raw_protection"})
_PRESERVE_EVIDENCE_FIELDS = frozenset({"original", "scope", "category", "filename", "content_type", "description", "text", "text_origin", "raw_protection"})
_ADD_PLAN_FIELDS = frozenset({"collection", "item", "plan_id", "expected_container_hash", "body", "why"})
_UPDATE_PLAN_FIELDS = frozenset({"collection", "plan_id", "changes", "body", "expected_container_hash", "expected_item_version", "why"})
_TRIAGE_PLAN_FIELDS = frozenset({"collection", "plan_id", "transition", "expected_container_hash", "expected_item_version", "why"})
# nosemgrep: ep-word-set -- the fold step's argument names, fixed by its schema.
_FOLD_RECAP_FIELDS = frozenset({"head", "revisions"})
# nosemgrep: ep-word-set -- the duplicate fold step's argument names, fixed by its schema.
_FOLD_DUPLICATE_FIELDS = frozenset({"canonical", "copy", "why"})
# nosemgrep: ep-word-set -- the unfold steps' one argument name, fixed by their schemas.
_UNFOLD_FIELDS = frozenset({"fold"})
_ARG_FIELDS = {
    # The referring pages one of several repoint steps rewrites (`repoint_links.prepare`).
    REPOINT_STEP_KIND: frozenset({"referrers"}),
    # Each inverse carries the sealed material of the step it reverses, under one name.
    UNMOVE_STEP_KIND: frozenset({"move"}),
    UNREPOINT_STEP_KIND: frozenset({"repoint"}),
    REVERT_POINTER_STEP_KIND: frozenset({"pointer"}),
    FOLD_RECAP_STEP_KIND: _FOLD_RECAP_FIELDS,
    UNFOLD_RECAP_STEP_KIND: frozenset({"fold"}),
    FOLD_DUPLICATE_STEP_KIND: _FOLD_DUPLICATE_FIELDS,
    UNFOLD_DUPLICATE_STEP_KIND: _UNFOLD_FIELDS,
    "capture-source": _CAPTURE_SOURCE_FIELDS,
    "preserve-evidence": _PRESERVE_EVIDENCE_FIELDS,
    "add-plan": _ADD_PLAN_FIELDS, "update-plan": _UPDATE_PLAN_FIELDS, "triage-plan": _TRIAGE_PLAN_FIELDS,
    "create-note": _CREATE_NOTE_FIELDS,
    "create-entity": _CREATE_ENTITY_FIELDS,
    "accept-relation": _ACCEPT_RELATION_FIELDS,
    "edit": _EDIT_FIELDS,
    OBSERVE_UNIT_STEP_KIND: _OBSERVE_UNIT_FIELDS,
    # The private undo protocol adds one optional binding, with no new required field.
    REMOVE_RELATION_STEP_KIND: _REMOVE_RELATION_FIELDS | {"_promotion"},
    "supersede": _SUPERSEDE_FIELDS,
    "move": _MOVE_FIELDS,
    "delete": _DELETE_FIELDS,
    "recover": _RECOVER_FIELDS,
    RECORDS_STEP_KIND: _APPEND_RECORD_FIELDS,
    "update-record": _UPDATE_RECORD_FIELDS,
}
_REQUIRED_FIELDS = {
    REPOINT_STEP_KIND: frozenset(),
    UNMOVE_STEP_KIND: frozenset({"move"}),
    UNREPOINT_STEP_KIND: frozenset({"repoint"}),
    REVERT_POINTER_STEP_KIND: frozenset({"pointer"}),
    FOLD_RECAP_STEP_KIND: _FOLD_RECAP_FIELDS,
    UNFOLD_RECAP_STEP_KIND: frozenset({"fold"}),
    FOLD_DUPLICATE_STEP_KIND: _FOLD_DUPLICATE_FIELDS,
    UNFOLD_DUPLICATE_STEP_KIND: _UNFOLD_FIELDS,
    "capture-source": frozenset({"original", "title", "source_type"}),
    "preserve-evidence": frozenset({"original", "scope", "category", "filename"}),
    "add-plan": frozenset({"collection", "item", "expected_container_hash", "why"}),
    "update-plan": _UPDATE_PLAN_FIELDS - {"body"}, "triage-plan": _TRIAGE_PLAN_FIELDS,
    "create-note": frozenset({"content", "title"}),
    "create-entity": frozenset({"entity_type", "name", "summary"}),
    "accept-relation": _ACCEPT_RELATION_FIELDS,
    "edit": _EDIT_FIELDS,
    OBSERVE_UNIT_STEP_KIND: _OBSERVE_UNIT_FIELDS,
    REMOVE_RELATION_STEP_KIND: _REMOVE_RELATION_FIELDS,
    "supersede": frozenset({"old_path", "content", "title"}),
    "move": frozenset({"old_path", "new_path"}),
    "delete": frozenset({"path", "confirm"}),
    "recover": frozenset({"trash_path"}),
    RECORDS_STEP_KIND: frozenset({"collection", "item", "why", "expected_container_hash"}),
    "update-record": _UPDATE_RECORD_FIELDS,
}


@dataclass
class CurationError(ValueError):
    code: str
    reason: str
    #: The plan step that preparation refused, when preparation names one (`_prepare_manifest`).
    step_id: str | None = None
    #: The plan's pages a refusal names when it is narrower than its step: a repoint step's
    #: refusal names the moved pages, by their old paths, whose referrer it refused.
    pages: tuple[str, ...] = ()

    def __str__(self) -> str:
        return f"{self.code}: {self.reason}"


def _error(code: str, reason: str) -> CurationError:
    return CurationError(code, reason)


def _write_refusal(error: Exception, what: str) -> CurationError:
    """The refusal for an artifact write that did not land: a collision only where a create-only
    target already exists, else the write's own code and the cause. The cause is the batch writer's
    message, the system's error text or an exception class name; a plain `ValueError` brings its
    own fixed text, and the batch access check's `WRITE_REFUSED` text names the artifact's
    vault-relative path."""
    if isinstance(error, CreateOnlyConflict):
        return _error("CURATION_PLAN_COLLISION", f"{what} already exists")
    if isinstance(error, BatchWriteError):
        cause = error.message
    elif isinstance(error, OSError):
        cause = os.strerror(error.errno) if error.errno else type(error).__name__
    elif type(error) is ValueError:
        cause = str(error)  # the store's and batch's own fixed texts
    else:
        cause = type(error).__name__
    return _error("CURATION_STORE_WRITE_FAILED", f"{what} was not written: {cause}")


#: How long a caller refused by `_run_claim` waits before it retries: one step's short work.
_RUN_BUSY_RETRY_AFTER_MS: Final[int] = 1000


@dataclass
class CurationRunBusy(CurationError):
    """Another call is executing the same run, or recorded this call's step as committed first.

    Refused at the run claim, this call wrote nothing. Refused at the settle, the receipt that the
    other call recorded accounts for the step, and this call's own leaf may have committed.
    """

    #: Whether this call's leaf committed: true when this call saw its batch commit the witness,
    #: null when it cannot tell. Not named `committed`: the dispatcher would store the refusal as
    #: the call's terminal, and a retry with the same key would replay it instead of continuing.
    leaf_committed: bool | None = False

    def as_public_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.reason,
            "remediation": "Retry after the other call finishes this run's step; the retry continues from the run's receipts.",
            "ok": False,
            "error_code": self.code,
            "status": "retryable",
            "committed": self.leaf_committed,
            "retry_after_ms": _RUN_BUSY_RETRY_AFTER_MS,
        }


_RUN_CLAIMS_LOCK = threading.Lock()
#: (vault root, run id) -> [owning thread, depth]. A forward run and its compensation share a run id.
_RUN_CLAIMS: dict[tuple[Path, str], list[int]] = {}


@contextmanager
def _run_claim(vault_root: Path, run_id: str) -> Iterator[None]:
    """Claim one run for this thread, or refuse at once with `CurationRunBusy`.

    Curation no longer holds the vault boundary across a step, so this claim keeps two callers in
    this process from executing one run's step at once. It never waits: a caller that holds the
    vault boundary, such as an episode resume, cannot deadlock on it. The thread that holds the
    claim joins it again.
    """
    key = (Path(vault_root).resolve(), run_id)
    thread = threading.get_ident()
    with _RUN_CLAIMS_LOCK:
        claim = _RUN_CLAIMS.get(key)
        if claim is not None and claim[0] != thread:
            raise CurationRunBusy("CURATION_RUN_BUSY", "another call is executing this curation run")
        if claim is None:
            claim = _RUN_CLAIMS[key] = [thread, 0]
        claim[1] += 1
    try:
        yield
    finally:
        with _RUN_CLAIMS_LOCK:
            claim[1] -= 1
            if claim[1] == 0:
                del _RUN_CLAIMS[key]


@contextmanager
def _step_boundary(vault_root: Path, operation: str) -> Iterator[None]:
    """Hold the vault mutation boundary for one canonical seam, or join this thread's hold.

    A nested guard re-enters the lock but advances the commit generation again, so a seam
    reached under an outer hold takes none.
    """
    from . import writer_lease

    manager = writer_lease.active_manager()
    if manager.holds_mutation_boundary(vault_root):
        yield
        return
    with manager.mutation_guard(
        vault_root,
        request_id=writer_lease.active_mutation_request_id(),
        operation=operation,
        holder_kind="command",
    ):
        yield


def guards_own_boundary(kwargs: Mapping[str, Any]) -> bool:
    """Whether these `maintain_memory` arguments run a curation action that holds the boundary itself."""
    return kwargs.get("mode") == "curation" and kwargs.get("curation_action") in _SELF_GUARDED_ACTIONS


RECORDS_STORE_UNSUPPORTED: Final[str] = "CURATION_RECORDS_STORE_UNSUPPORTED"


def require_item_files(manifest: Any) -> None:
    """A Records leaf's witness names one item file, so only a one-file-per-item collection takes one."""
    if manifest.storage.strategy != "markdown-items" or manifest.view_mode != "items":
        raise _error(
            "CURATION_RECORDS_STORAGE_UNSUPPORTED", "a Records leaf writes to a markdown-items collection"
        )


def store_request_id(run_identity: str, operation_identity: str) -> str:
    """The request id a store step's transaction carries: the operation, within the run that applies it.

    Two runs sealed from one unchanged state on different days share a plan and so an operation
    identity. Naming the run keeps one run from finding the other's transaction and calling its own
    step committed; the later run then meets the moved container hash and refuses as stale.
    """
    return f"{run_identity}/{operation_identity}"


def _is_governed_trash(path: str) -> bool:
    """Whether `path` lies in the managed root's trash folder, compared without regard to case."""
    return trash_metadata.is_inside_trash(path, key=str.casefold)


def _records_store_unsupported(kind: str) -> CurationError:
    """A Records leaf on a collection the store owns runs only on the store thread of the service
    that serves it; anywhere else it refuses, and names the Records action that makes the same
    change there."""
    action = "append" if kind == RECORDS_STEP_KIND else "update"
    return _error(
        RECORDS_STORE_UNSUPPORTED,
        "a Records leaf writes a collection-store collection only through the service that serves "
        "this vault: send the request there, or make the change with "
        f"{capabilities.active_tools_text('record_memory', 'write')} {action}",
    )


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError) as error:
        raise _error("INVALID_CURATION_PLAN", "plan must be finite canonical JSON") from error


def canonical_json_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def plan_id(plan: Mapping[str, Any]) -> str:
    return _digest(plan)


def run_id(plan: Mapping[str, Any], *, today: dt.date | None = None) -> str:
    day = today or dt.date.today()
    return f"cur-{day:%Y%m%d}-{plan_id(plan)[:12]}"


def operation_id(plan_identity: str, ordinal: int, step_id: str) -> str:
    if not _HEX64.fullmatch(str(plan_identity)) or type(ordinal) is not int or ordinal < 0:
        raise _error("INVALID_OPERATION_ID", "operation identity inputs are invalid")
    encoded = canonical_json_bytes(
        ["exomem-curation-operation-v1", plan_identity, ordinal, step_id]
    )
    return hashlib.sha256(encoded).hexdigest()


def _require_relocation_history_capability(_vault_root: Path) -> NoReturn:
    """Refuse relocation until held-fs proves durable namespace lineage.

    Stable file identity and post-crash placement hashes cannot distinguish a
    completed rename from a crash followed by an external inverse rename.  The
    current held-fs ABI has no epoch-scoped monotonic namespace generation for
    both retained parents, so curation must not publish or authorize a rename
    transition on this base.
    """
    raise _error(
        "CURATION_RENAME_HISTORY_UNPROVABLE",
        "curation relocation requires durable retained-parent namespace lineage",
    )


def _require_plan_relocation_history(
    vault_root: Path, plan: Mapping[str, Any]
) -> None:
    # A move records its intent before its rename and recovers without renaming; `delete` and
    # `recover` still have no such history.
    # nosemgrep: ep-word-set -- the two step kinds this module defines without relocation history.
    if any(step.get("kind") in {"delete", "recover"} for step in plan["steps"]):
        _require_relocation_history_capability(vault_root)


def plan_fingerprint(
    plan: Mapping[str, Any],
    binding_manifest: Iterable[Mapping[str, Any]],
    registry_ids: Mapping[str, str],
) -> str:
    return _digest(
        {
            "plan": plan,
            "binding_manifest": list(binding_manifest),
            "registry_ids": dict(registry_ids),
        }
    )


def _unknown_fields(value: Mapping[str, Any], allowed: frozenset[str], where: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        # Naming the allowed fields lets an agent correct its plan from one refusal
        # instead of probing the shape field by field.
        raise _error(
            "CURATION_UNKNOWN_FIELD",
            f"{where} has unknown fields: {unknown}; allowed: {sorted(allowed)}",
        )


def _require_string(value: Any, field: str, *, max_chars: int = MAX_PLAN_BYTES) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _error("INVALID_CURATION_PLAN", f"{field} must be a non-empty string")
    if "\n" in value or "\r" in value:
        if field.endswith("why"):
            raise _error("INVALID_CURATION_PLAN", f"{field} must be one line")
    if len(value) > max_chars:
        raise _error("CURATION_PLAN_TOO_LARGE", f"{field} exceeds its size cap")
    return value


#: Casefolded top-level Knowledge Base trees another typed owner governs.
#: Curation cannot target them and tag maintenance never rewrites them.
PROTECTED_TREES: Final = frozenset(
    {
        "planning",
        "records",
        "workflow-contract",
        "workflow-contracts",
        "_schema",
        "_governance",
        "_adoption",
        "sources",
        "evidence",
    }
)


def _folded_segment(part: str) -> str:
    """One path segment as a filesystem may match it: NFKC, casefolded, and
    without the trailing dots and spaces Windows drops."""
    return unicodedata.normalize("NFKC", part).casefold().strip().rstrip(". ")


def _confined_path(path: Any, field: str) -> str:
    """`path` as a vault-relative POSIX path that names no parent, empty or current segment."""
    raw = _require_string(path, field).replace("\\", "/").strip().lstrip("/")
    if (
        raw.endswith("/")
        or "\x00" in raw
        or any(part in {"", ".", ".."} for part in raw.split("/"))
    ):
        raise _error("INVALID_CURATION_PATH", f"{field} is not a confined vault path")
    return raw


def normalize_target_path(
    path: Any, *, field: str = "path", allow_trash: bool = False, allow_records: bool = False, allow_fold: bool = False, allow_append_only: bool = False, _planning_tree: bool = False, _planning_identity: tuple[str, str] | None = None, _native_paths: frozenset[str] | None = None
) -> str:
    """A confined governed path. `allow_records` is the Records leaf's alone:
    it admits the Records tree, spelled exactly as the Records layer is, and
    nothing else, so that leaf can neither leave the tree nor open it to any
    other kind. Every other kind compares folded segments, so a spelling the
    filesystem folds onto a protected tree is that tree. `allow_fold` is the fold
    steps' alone: it admits the Sources tree and the trash, and no other protected tree.
    `allow_append_only` is a pointer correction's alone: it admits the append-only trees, whose
    pages then change only through the governed pointer write."""
    if _planning_identity is not None:
        from . import structured_collections

        if path != structured_collections.plan_ref(*_planning_identity):
            raise _error("CURATION_TARGET_PROTECTED", "Planning store identity changed")
        return path
    raw = _confined_path(path, field)
    relative = managed_split(raw)
    if relative is None:
        raise _error("CURATION_TARGET_PROTECTED", f"{field} must target governed knowledge")
    if _native_paths is not None:
        if raw not in _native_paths:
            raise _error("CURATION_TARGET_PROTECTED", "path is outside the sealed native core")
        return raw
    parts = relative.split("/")
    first = _folded_segment(parts[0])
    compact = "/".join(_folded_segment(part) for part in parts).replace("_", "-")
    if _planning_tree:
        # Native Planning dispatch fixes this owner layer; public curation never supplies it.
        if parts[0] != PLANNING_PROFILE.placement_layer or len(parts) < 2:
            raise _error("CURATION_TARGET_PROTECTED", f"{field} must stay inside its native owner tree")
        return raw
    if allow_records:
        # The exact layer `structured_collections._require_profile_layer` places
        # Records collections under; no folded spelling stands in for it.
        if parts[0] != "Records" or len(parts) < 2:
            raise _error("CURATION_TARGET_PROTECTED", f"{field} must stay inside Records")
        return raw
    if (
        first in PROTECTED_TREES and not ((allow_fold or allow_append_only) and first in _FOLD_TREES)
    ) or compact.startswith(
        "workflow-contract/"
    ):
        raise _error("CURATION_TARGET_PROTECTED", f"{field} belongs to another typed owner")
    if first == trash_metadata.TRASH_SUBPATH and not (allow_trash or allow_fold):
        raise _error("CURATION_TARGET_PROTECTED", f"{field} targets trash internals")
    return raw


def _validate_args(kind: str, raw: Any, ordinal: int) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise _error("INVALID_CURATION_PLAN", f"steps[{ordinal}].args must be an object")
    args = dict(raw)
    _unknown_fields(args, _ARG_FIELDS[kind], f"steps[{ordinal}].args")
    missing = sorted(name for name in _REQUIRED_FIELDS[kind] if name not in args)
    if missing:
        raise _error("INVALID_CURATION_PLAN", f"steps[{ordinal}].args misses {missing}")

    if kind in FOLD_STEP_KINDS:
        from . import duplicate_fold, episode_fold

        args = {
            FOLD_RECAP_STEP_KIND: episode_fold.validate_args,
            UNFOLD_RECAP_STEP_KIND: episode_fold.validate_unfold_args,
            FOLD_DUPLICATE_STEP_KIND: duplicate_fold.validate_args,
            UNFOLD_DUPLICATE_STEP_KIND: duplicate_fold.validate_unfold_args,
        }[kind](args, ordinal)
    elif kind in PRESERVATION_STEP_KINDS:
        from . import provenance

        args["original"] = provenance._origin_payload(  # noqa: SLF001
            {"inputs": {"original": args["original"]}, "assessments": [], "bindings": []}, authoring=True
        )["inputs"]["original"]
        if args["original"].get("representation") != "original":
            raise _error("INVALID_CURATION_PLAN", "native binary effect requires an explicit original")
        for field in _REQUIRED_FIELDS[kind] - {"original"}:
            _require_string(args[field], field)
    elif kind in PLANNING_STEP_KINDS:
        _require_string(args["collection"], "collection", max_chars=2048)
        _require_string(args["why"], "why", max_chars=MAX_WHY_CHARS)
        value_field = {"add-plan": "item", "update-plan": "changes", "triage-plan": "transition"}[kind]
        if not isinstance(args[value_field], Mapping):
            raise _error("INVALID_CURATION_PLAN", "Planning payload must be an object")
        if not _HEX64.fullmatch(str(args["expected_container_hash"])):
            raise _error("INVALID_CURATION_PLAN", "Planning requires the reviewed container hash")
        if kind != "add-plan":
            _require_string(args["plan_id"], "plan_id", max_chars=512)
            if not _HEX64.fullmatch(str(args["expected_item_version"])):
                raise _error("INVALID_CURATION_PLAN", "Planning requires the reviewed item version")
    elif kind == "create-note":
        _require_string(
            args.get("title"), f"steps[{ordinal}].args.title", max_chars=MAX_TITLE_CHARS
        )
        _require_string(args.get("content"), f"steps[{ordinal}].args.content")
    elif kind == "create-entity":
        for field in ("entity_type", "name", "summary"):
            _require_string(args.get(field), f"steps[{ordinal}].args.{field}")
        if "registry_scope" in args:
            _require_string(args["registry_scope"], f"steps[{ordinal}].args.registry_scope")
    elif kind == "accept-relation":
        for field in _ACCEPT_RELATION_FIELDS:
            _require_string(args.get(field), f"steps[{ordinal}].args.{field}")
        if not _HEX64.fullmatch(args["expected_hash"]) or not _HEX24.fullmatch(
            args["expected_fingerprint"]
        ):
            raise _error("INVALID_CURATION_PLAN", "relation bindings are invalid")
    elif kind == "edit":
        raw_operation = args.get("operation")
        # A frontmatter patch may name an append-only page; proposal admits only a pointer field.
        args["path"] = normalize_target_path(
            args.get("path"),
            field="path",
            allow_append_only=isinstance(raw_operation, Mapping)
            and raw_operation.get("kind") == "patch_frontmatter",
        )
        _require_string(args.get("why"), f"steps[{ordinal}].args.why", max_chars=MAX_WHY_CHARS)
        if not isinstance(args.get("operation"), Mapping):
            raise _error("INVALID_CURATION_PLAN", "edit operation must be a closed object")
        from .edit_operations import EDIT_OPERATION_ADAPTER

        try:
            operation = EDIT_OPERATION_ADAPTER.validate_python(dict(args["operation"]))
        except Exception as error:
            raise _error("INVALID_CURATION_PLAN", "edit operation schema is invalid") from error
        normalized_operation = operation.model_dump(mode="python", exclude_none=True)
        expected_hash = normalized_operation.get("expected_hash")
        if not isinstance(expected_hash, str) or not _HEX64.fullmatch(expected_hash):
            raise _error("INVALID_CURATION_PLAN", "edit requires the reviewed expected_hash")
        args["operation"] = normalized_operation
    elif kind == REMOVE_RELATION_STEP_KIND:
        if "_promotion" in args:
            from . import promotion_proof

            args["_promotion"] = promotion_proof.metadata(args["_promotion"])
        args["subject"] = normalize_target_path(args.get("subject"), field="subject")
        args["target"] = normalize_target_path(args.get("target"), field="target")
        _require_string(args.get("predicate"), f"steps[{ordinal}].args.predicate", max_chars=200)
        _require_string(args.get("why"), f"steps[{ordinal}].args.why", max_chars=MAX_WHY_CHARS)
    elif kind == OBSERVE_UNIT_STEP_KIND:
        args["path"] = normalize_target_path(args.get("path"), field="path")
        _require_string(args.get("why"), f"steps[{ordinal}].args.why", max_chars=MAX_WHY_CHARS)
        observation = args.get("observation")
        if not isinstance(observation, Mapping):
            raise _error("INVALID_CURATION_PLAN", "observation must be an object")
        from inspect import signature

        from .observe_memory import observe_memory

        # This projection uses the writer's actual argument schema, without another engine.
        fields = set(signature(observe_memory).parameters) - {
            "vault_root", "path", "operation", "reason", "today", "transition_token",
        }
        _unknown_fields(observation, fields, "observation")
        for field in ("unit_ref", "expected_fingerprint", "expected_hash", "category", "content"):
            _require_string(observation.get(field), field)
        if not isinstance(observation.get("target_fingerprints", {}), Mapping):
            raise _error("INVALID_CURATION_PLAN", "target_fingerprints must be an object")
        args["observation"] = {**observation, "target_fingerprints": dict(observation.get("target_fingerprints", {}))}
    elif kind == "supersede":
        args["old_path"] = normalize_target_path(args.get("old_path"), field="old_path")
        _require_string(args.get("content"), f"steps[{ordinal}].args.content")
        _require_string(
            args.get("title"), f"steps[{ordinal}].args.title", max_chars=MAX_TITLE_CHARS
        )
    elif kind in RECORDS_STEP_KINDS:
        _require_string(args.get("collection"), f"steps[{ordinal}].args.collection", max_chars=2048)
        value_field = "item" if kind == RECORDS_STEP_KIND else "changes"
        if not isinstance(args.get(value_field), Mapping):
            raise _error("INVALID_CURATION_PLAN", f"{kind} {value_field} must be an object")
        args[value_field] = json.loads(canonical_json(args[value_field]))
        if "item_key" in args:
            _require_string(args["item_key"], f"steps[{ordinal}].args.item_key", max_chars=512)
        if "body" in args and not isinstance(args["body"], str):
            raise _error("INVALID_CURATION_PLAN", "append-record body must be a string")
        _require_string(args.get("why"), f"steps[{ordinal}].args.why", max_chars=MAX_WHY_CHARS)
        if not _HEX64.fullmatch(str(args.get("expected_container_hash") or "")):
            raise _error(
                "INVALID_CURATION_PLAN", f"{kind} requires the reviewed expected_container_hash"
            )
        if kind == "update-record" and not _HEX64.fullmatch(
            str(args.get("expected_item_version") or "")
        ):
            raise _error(
                "INVALID_CURATION_PLAN", "update-record requires the reviewed expected_item_version"
            )
    elif kind == REPOINT_STEP_KIND and "referrers" in args:
        referrers = args["referrers"]
        if not isinstance(referrers, list) or not referrers:
            raise _error("INVALID_CURATION_PLAN", "repoint-links referrers must list pages")
        # Shape only: the step applies the unsplit step's own read and write admission to each page.
        args["referrers"] = sorted({_confined_path(page, "referrers") for page in referrers})
        # A name that differs only in case is the same page on a case-blind volume.
        if len({vault.vault_name_key(page) for page in args["referrers"]}) != len(referrers):
            raise _error("INVALID_CURATION_PLAN", "repoint-links referrers repeat a page")
    elif kind == "move":
        args["old_path"] = normalize_target_path(args.get("old_path"), field="old_path")
        args["new_path"] = normalize_target_path(args.get("new_path"), field="new_path")
        if "fields" in args:
            fields = args["fields"]
            if not isinstance(fields, Mapping) or not fields or not all(
                isinstance(name, str) and name for name in fields
            ):
                raise _error(
                    "INVALID_CURATION_PLAN", "move fields must be a non-empty object of fields"
                )
            args["fields"] = json.loads(canonical_json(fields))
        if "project_folders" in args:
            folders = args["project_folders"]
            if (
                "fields" not in args
                or not isinstance(folders, Mapping)
                or not folders
                or not all(
                    isinstance(key, str) and isinstance(folder, str) and folder.strip()
                    for key, folder in folders.items()
                )
            ):
                raise _error(
                    "INVALID_CURATION_PLAN",
                    "move project_folders must name a folder for each project key its fields patch registers",
                )
            from . import project_keys

            for folder in folders.values():
                try:
                    project_keys.require_portable_folder(folder)
                except project_keys.InvalidProjectFolderError as error:
                    raise _error("INVALID_CURATION_PLAN", str(error)) from error
            args["project_folders"] = json.loads(canonical_json(folders))
    elif kind in _RELOCATION_INVERSE_KINDS:
        (material,) = args.values()
        if not isinstance(material, Mapping):
            raise _error("INVALID_CURATION_PLAN", f"{kind} carries its sealed material as an object")
    elif kind == "delete":
        args["path"] = normalize_target_path(args.get("path"), field="path")
        if args.get("confirm") is not True:
            raise _error("INVALID_CURATION_PLAN", "delete requires canonical confirm=true")
    elif kind == "recover":
        trash_path = normalize_target_path(
            args.get("trash_path"), field="trash_path", allow_trash=True
        )
        if not _is_governed_trash(trash_path):
            raise _error(
                "INVALID_CURATION_PLAN", "recover requires an exact governed trash identity"
            )
        args["trash_path"] = trash_path
        if args.get("restore_path") is not None:
            args["restore_path"] = normalize_target_path(args["restore_path"], field="restore_path")
    return json.loads(canonical_json(args))


def _validate_entity_candidate_binding(raw: Any) -> dict[str, Any]:
    """Validate the exact deterministic review binding an agent round-trips."""
    if not isinstance(raw, Mapping):
        raise _error("INVALID_CURATION_PLAN", "entity_candidate must be an object")
    value = dict(raw)
    _unknown_fields(value, _ENTITY_CANDIDATE_FIELDS, "entity_candidate")
    required = _ENTITY_CANDIDATE_FIELDS - {
        "executable",
        "closure_only",
        "deferred_remaining_count",
    }
    missing = sorted(required - set(value))
    if missing:
        raise _error("INVALID_CURATION_PLAN", f"entity_candidate misses {missing}")
    review_ref = _require_string(value.get("review_ref"), "entity_candidate.review_ref")
    if not re.fullmatch(r"exomem://review/[0-9a-f]{24}", review_ref.casefold()):
        raise _error("INVALID_CURATION_PLAN", "entity_candidate.review_ref is invalid")
    review_fingerprint = _require_string(
        value.get("review_fingerprint"), "entity_candidate.review_fingerprint"
    )
    if not _HEX24.fullmatch(review_fingerprint):
        raise _error("INVALID_CURATION_PLAN", "entity candidate review fingerprint is invalid")
    state = value.get("candidate_state")
    if state not in {"promotion", "hydration", "ambiguous"}:
        raise _error("INVALID_CURATION_PLAN", "entity candidate state is invalid")
    identity = _require_string(value.get("identity"), "entity_candidate.identity")
    signal = _require_string(value.get("signal_version"), "entity_candidate.signal_version")
    if not re.fullmatch(r"(?:[0-9a-f]{16}|[0-9a-f]{64})", signal):
        raise _error("INVALID_CURATION_PLAN", "entity candidate signal version is invalid")
    contexts = value.get("first_disconnected_context_batch")
    if not isinstance(contexts, list) or len(contexts) > 8:
        raise _error("INVALID_CURATION_PLAN", "entity candidate context batch is invalid")
    normalized_contexts: list[dict[str, str]] = []
    for index, raw_context in enumerate(contexts):
        if not isinstance(raw_context, Mapping):
            raise _error("INVALID_CURATION_PLAN", "entity candidate context is invalid")
        context = dict(raw_context)
        _unknown_fields(
            context,
            _ENTITY_CONTEXT_BINDING_FIELDS,
            f"entity_candidate.first_disconnected_context_batch[{index}]",
        )
        if set(context) != _ENTITY_CONTEXT_BINDING_FIELDS:
            raise _error("INVALID_CURATION_PLAN", "entity candidate context binding is incomplete")
        path = _require_string(
            context["path"],
            f"entity_candidate.first_disconnected_context_batch[{index}].path",
        ).replace("\\", "/").lstrip("/")
        context_hash = _require_string(
            context["context_hash"],
            f"entity_candidate.first_disconnected_context_batch[{index}].context_hash",
        )
        if not _HEX64.fullmatch(context_hash):
            raise _error("INVALID_CURATION_PLAN", "entity candidate context hash is invalid")
        normalized_contexts.append({"path": path, "context_hash": context_hash})
    if normalized_contexts != sorted(
        normalized_contexts, key=lambda item: (item["path"], item["context_hash"])
    ):
        raise _error("INVALID_CURATION_PLAN", "entity candidate context batch is not canonical")
    remaining = value.get("remaining_disconnected_count")
    if type(remaining) is not int or remaining < 0:
        raise _error("INVALID_CURATION_PLAN", "entity candidate remaining count is invalid")
    batch_fingerprint = value.get("batch_fingerprint")
    if batch_fingerprint is not None and (
        not isinstance(batch_fingerprint, str) or not _HEX64.fullmatch(batch_fingerprint)
    ):
        raise _error("INVALID_CURATION_PLAN", "entity candidate batch fingerprint is invalid")
    targets = value.get("target_refs")
    if not isinstance(targets, list) or not all(
        isinstance(target, str) and target.strip() for target in targets
    ):
        raise _error("INVALID_CURATION_PLAN", "entity candidate targets are invalid")
    normalized_targets = sorted(set(targets))
    if normalized_targets != targets or len(targets) > 8:
        raise _error("INVALID_CURATION_PLAN", "entity candidate targets are not canonical")
    grammar = value.get("grammar_identity")
    if not isinstance(grammar, Mapping):
        raise _error("INVALID_CURATION_PLAN", "entity candidate grammar identity is invalid")
    grammar_value = dict(grammar)
    _unknown_fields(grammar_value, _ENTITY_GRAMMAR_FIELDS, "entity_candidate.grammar_identity")
    if set(grammar_value) != _ENTITY_GRAMMAR_FIELDS:
        raise _error("INVALID_CURATION_PLAN", "entity candidate grammar identity is incomplete")
    grammar_version = _require_string(
        grammar_value.get("version"), "entity_candidate.grammar_identity.version"
    )
    predicate_digest = grammar_value.get("predicate_table_digest")
    if predicate_digest is not None and (
        not isinstance(predicate_digest, str) or not _HEX64.fullmatch(predicate_digest)
    ):
        raise _error("INVALID_CURATION_PLAN", "entity candidate predicate digest is invalid")
    registry_identity = _require_string(
        value.get("registry_identity"), "entity_candidate.registry_identity"
    )
    if not _HEX64.fullmatch(registry_identity):
        raise _error("INVALID_CURATION_PLAN", "entity candidate registry identity is invalid")
    if value.get("closure_only") is True or value.get("executable") is False:
        raise _error(
            "CURATION_ENTITY_BATCH_DEFERRED",
            "a closure-only hydration recheck cannot bind a curation plan",
        )
    return {
        "review_ref": review_ref,
        "review_fingerprint": review_fingerprint,
        "candidate_state": state,
        "identity": identity,
        "signal_version": signal,
        "first_disconnected_context_batch": normalized_contexts,
        "remaining_disconnected_count": remaining,
        "batch_fingerprint": batch_fingerprint,
        "target_refs": normalized_targets,
        "grammar_identity": {
            "version": grammar_version,
            "predicate_table_digest": predicate_digest,
        },
        "registry_identity": registry_identity,
    }


def validate_forward_plan(
    raw: Any, *, allow_records: bool = False, allow_fold_inverse: bool = False, derived: bool = False
) -> dict[str, Any]:
    """A closed forward plan. The compatibility flag `allow_records` admits
    the private leaves (the episode leaves and the promotion revert's
    `remove-relation`); only their sealers and sealed plans pass it.
    `allow_fold_inverse` admits the two unfold steps, which only a
    compensation derivation and a plan it sealed may carry. `derived` is a
    server derivation's (`DERIVED_BY`) alone: its step cap is `MAX_DERIVED_STEPS`
    and its size cap `MAX_SEALED_BYTES`, so an agent cannot write a plan past `MAX_STEPS`."""
    max_steps, max_bytes = (MAX_DERIVED_STEPS, MAX_SEALED_BYTES) if derived else (MAX_STEPS, MAX_PLAN_BYTES)
    kinds = (
        *STEP_KINDS,
        *((*_EPISODE_STEP_KINDS, REMOVE_RELATION_STEP_KIND) if allow_records else ()),
        *(
            (UNFOLD_RECAP_STEP_KIND, UNFOLD_DUPLICATE_STEP_KIND, *sorted(_RELOCATION_INVERSE_KINDS))
            if allow_fold_inverse
            else ()
        ),
    )
    if not isinstance(raw, Mapping):
        raise _error("INVALID_CURATION_PLAN", "plan must be an object")
    value = dict(raw)
    _unknown_fields(value, _PLAN_FIELDS, "plan")
    if type(value.get("version")) is not int or value["version"] != 1:
        raise _error("INVALID_CURATION_PLAN", "plan version must be the integer 1")
    title = _require_string(value.get("title"), "plan.title", max_chars=MAX_TITLE_CHARS)
    steps = value.get("steps")
    if not isinstance(steps, list) or not steps:
        raise _error("INVALID_CURATION_PLAN", "plan.steps must be a non-empty list")
    if len(steps) > max_steps:
        raise _error("CURATION_PLAN_TOO_LARGE", f"plan has more than {max_steps} steps")
    normalized_steps: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ordinal, raw_step in enumerate(steps):
        if not isinstance(raw_step, Mapping):
            raise _error("INVALID_CURATION_PLAN", f"steps[{ordinal}] must be an object")
        step = dict(raw_step)
        _unknown_fields(step, _STEP_FIELDS, f"steps[{ordinal}]")
        step_identity = _require_string(
            step.get("step_id"), f"steps[{ordinal}].step_id", max_chars=MAX_STEP_ID_CHARS
        )
        if not _STEP_ID.fullmatch(step_identity):
            raise _error("INVALID_CURATION_PLAN", f"steps[{ordinal}].step_id is invalid")
        if step_identity in seen:
            raise _error("DUPLICATE_STEP_ID", f"step id {step_identity!r} is duplicated")
        seen.add(step_identity)
        kind = step.get("kind")
        if kind not in kinds:
            raise _error("INVALID_STEP_KIND", f"step kind must be one of {list(STEP_KINDS)}")
        normalized_steps.append(
            {
                "step_id": step_identity,
                "kind": kind,
                "args": _validate_args(kind, step.get("args"), ordinal),
            }
        )
    _require_repoint_placement(normalized_steps)
    normalized = {"version": 1, "title": title, "steps": normalized_steps}
    if any(step["kind"] in _EPISODE_STEP_KINDS for step in normalized_steps) and (
        len(normalized_steps) != 1 or "entity_candidate" in value
    ):
        raise _error("CURATION_RECORDS_LEAF_ALONE", "an episode leaf is sealed alone in its plan")
    if "entity_candidate" in value:
        normalized["entity_candidate"] = _validate_entity_candidate_binding(
            value["entity_candidate"]
        )
    if len(canonical_json_bytes(normalized)) > max_bytes:
        raise _error("CURATION_PLAN_TOO_LARGE", f"canonical plan exceeds {max_bytes} bytes")
    return normalized


def _require_repoint_placement(steps: Sequence[Mapping[str, Any]]) -> None:
    """Refuse `repoint-links` steps that are not the plan's last steps or follow no `move` step.

    Several repoint steps split the rewrite by referring page: each names its `referrers`, and no
    page is named twice, so each referrer is rewritten by exactly one step.
    """
    kinds = [step["kind"] for step in steps]
    count = kinds.count(REPOINT_STEP_KIND)
    if not count:
        return
    if any(kind != REPOINT_STEP_KIND for kind in kinds[-count:]) or "move" not in kinds[:-count]:
        raise _error(
            "CURATION_STEP_INVALID",
            "repoint-links steps must be the plan's last steps and follow a move step",
        )
    if count == 1:
        return
    named: set[str] = set()
    for step in steps[-count:]:
        referrers = step["args"].get("referrers")
        if referrers is None:
            raise _error(
                "CURATION_STEP_INVALID", "each of several repoint-links steps names its referrers"
            )
        keys = {vault.vault_name_key(page) for page in referrers}
        if keys & named:
            raise _error(
                "CURATION_STEP_INVALID", "two repoint-links steps name the same referrer"
            )
        named |= keys


def _validate_sealed_plan(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise _error("CURATION_PLAN_CORRUPT", "stored plan is not an object")
    base = {key: value for key, value in raw.items() if key in _PLAN_FIELDS}
    derived_by = raw.get("derived_by")
    if "derived_by" in raw and derived_by not in DERIVED_BY:
        raise _error("CURATION_PLAN_CORRUPT", "stored plan derivation is invalid")
    if "approval_summary" in raw and (derived_by is None or not isinstance(raw["approval_summary"], Mapping)):
        raise _error("CURATION_PLAN_CORRUPT", "stored approval summary is invalid")
    # A stored plan was admitted by `propose`, which alone decides whether a
    # Records leaf may be sealed.
    normalized = validate_forward_plan(
        base, allow_records=True, allow_fold_inverse=True, derived=derived_by is not None
    )
    _unknown_fields(dict(raw), _PLAN_FIELDS | _SEALED_FIELDS, "stored plan")
    manifest = raw.get("binding_manifest", [])
    registries = raw.get("registry_ids", {})
    if not isinstance(manifest, list) or not all(isinstance(item, Mapping) for item in manifest):
        raise _error("CURATION_PLAN_CORRUPT", "stored binding manifest is invalid")
    if not isinstance(registries, Mapping) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in registries.items()
    ):
        raise _error("CURATION_PLAN_CORRUPT", "stored registry identities are invalid")
    episode_bound = any(isinstance(prepared := item.get("prepared"), Mapping) and isinstance(prepared.get("episode"), Mapping) for item in manifest)
    if ("episode_invocation_version" in raw) != episode_bound or episode_bound and (
        type(raw["episode_invocation_version"]) is not int or raw["episode_invocation_version"] != 1
    ):
        raise _error("CURATION_PLAN_CORRUPT", "episode invocation reader marker is invalid")
    return {
        **normalized,
        **({"episode_invocation_version": 1} if episode_bound else {}),
        "binding_manifest": json.loads(canonical_json(manifest)),
        "registry_ids": dict(registries),
        **({"plan_type": raw["plan_type"]} if isinstance(raw.get("plan_type"), str) else {}),
        **(
            {"forward_plan_id": raw["forward_plan_id"]}
            if isinstance(raw.get("forward_plan_id"), str)
            else {}
        ),
        **({"derived_by": derived_by} if derived_by is not None else {}),
        **({"approval_summary": dict(raw["approval_summary"])} if "approval_summary" in raw else {}),
    }


def _optional_model_call(*_args: Any, **_kwargs: Any) -> None:
    """Non-capability sentinel: curation never calls a reasoning model."""
    return None


def _step_instances(vault_root: Path, plan: Mapping[str, Any]) -> list[str]:
    """The private instances the plan's create-entity steps select, as `link` selects them."""
    from . import link as link_module
    from .vocabulary.registry import RegistryError

    found: set[str] = set()
    for step in plan.get("steps") or ():
        if step.get("kind") != "create-entity":
            continue
        args = step["args"]
        try:
            scope = link_module.entity_instance(
                vault_root,
                project=args.get("project"),
                tags=args.get("tags"),
                registry_scope=args.get("registry_scope"),
            )
        except RegistryError:
            # The step's own preparation refuses it; no instance is bound for it here.
            continue
        if scope is not None:
            found.add(scope)
    return sorted(found)


def _instance_identity(vault_root: Path, scope: str) -> str:
    from . import entity_types, relation_registry
    from .vocabulary.registry import RegistryError

    try:
        entities = entity_types.load_entity_types(vault_root, registry_scope=scope)
        relations = relation_registry.load_registry(vault_root, registry_scope=scope)
    except RegistryError:
        # A caller who cannot admit the instance gets one answer, never its definitions.
        return _digest({"scope": scope, "available": False})
    return _digest(
        {
            "entities": [entities.core_version, entities.extension_hash],
            "relations": [relations.core_version, relations.extension_hash],
        }
    )


def _stored_schema_rows(
    vault_root: Path, registry: Path, contracts: Path
) -> list[dict[str, str]]:
    """``_schema_rows`` for a store-mode vault: the registry and contract pages the store holds,
    each with the hash that the store keeps for the bytes it holds.
    """
    from .vault_store import read

    root = Path(vault_root)
    contracts_prefix = f"{contracts.relative_to(root).as_posix()}/"
    with read.snapshot(root) as view:
        metas = [view.page_meta(registry.relative_to(root).as_posix())]
        # ``*.yaml`` directly below ``contracts``: a file extension is a closed set that the format
        # fixes.
        metas.extend(
            meta for meta in view.pages(contracts_prefix)
            if meta.path.endswith(".yaml") and "/" not in meta.path[len(contracts_prefix):]
        )
    return [{"path": meta.path, "sha256": meta.sha256} for meta in metas if meta is not None]


def _schema_rows(vault_root: Path) -> list[dict[str, str]]:
    """The path and hash of each schema registry file that an executable proof binds."""
    from . import semantic_language_registry
    from .vault_store import mode

    schema_root = managed_root(Path(vault_root)) / "_Schema"
    registry = semantic_language_registry.registry_path(vault_root)
    contracts_root = schema_root / "contracts"
    if mode.store_mode_for_reads(Path(vault_root)):
        return _stored_schema_rows(vault_root, registry, contracts_root)
    schema_rows: list[dict[str, str]] = []
    schema_candidates: list[Path] = [registry]
    if contracts_root.is_dir() and not contracts_root.is_symlink():
        schema_candidates.extend(sorted(contracts_root.glob("*.yaml")))
    if not schema_root.is_symlink():
        for path in schema_candidates:
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(vault_root).as_posix()
            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError as error:
                raise _error(
                    "CURATION_REGISTRY_UNREADABLE", "schema registry changed while read"
                ) from error
            schema_rows.append({"path": relative, "sha256": digest})
    return schema_rows


def registry_identities(vault_root: Path) -> dict[str, str]:
    """Return deterministic identities for registries that affect executability."""
    from . import entity_types, relation_registry

    entities = entity_types.load_entity_types(vault_root)
    relations = relation_registry.load_registry(vault_root)
    schema_rows = _schema_rows(vault_root)
    return {
        "entities": _digest(
            {"core_version": entities.core_version, "extension_hash": entities.extension_hash}
        ),
        "relations": _digest(
            {"core_version": relations.core_version, "extension_hash": relations.extension_hash}
        ),
        "schemas": _digest(schema_rows),
    }


def plan_registry_identities(vault_root: Path, plan: Mapping[str, Any]) -> dict[str, str]:
    """The public registry identities plus one per private instance the plan's steps select."""
    return {
        **registry_identities(vault_root),
        **{
            f"instance@{scope}": _instance_identity(vault_root, scope)
            for scope in _step_instances(Path(vault_root), plan)
        },
    }


def step_schemas() -> dict[str, dict[str, list[str]]]:
    return {
        kind: {
            "required": sorted(_REQUIRED_FIELDS[kind]),
            "allowed": sorted(_ARG_FIELDS[kind]),
        }
        for kind in STEP_KINDS
    }


def _guarded_text(vault_root: Path, path: str, *, stale: bool) -> tuple[str, str]:
    """Read a lexical vault path through the shared no-follow descriptor guard."""
    try:
        PathGuard.capture(vault_root, path, leaf_policy="stable")
        source, _guard = read_guarded_text(vault_root, vault_root / path)
    except PathGuardError as error:
        if error.code in {"PATH_GUARD_INVALID", "PATH_GUARD_ROOT", "PATH_GUARD_UNSAFE"}:
            raise _error("CURATION_PATH_UNSAFE", f"target {path!r} crosses an unsafe path") from error
        code = "CURATION_BINDING_STALE" if stale else "CURATION_TARGET_UNREADABLE"
        raise _error(code, f"target {path!r} is unavailable") from error
    except (FileNotFoundError, OSError, UnicodeDecodeError) as error:
        code = "CURATION_BINDING_STALE" if stale else "CURATION_TARGET_NOT_FOUND"
        raise _error(code, f"target {path!r} is unavailable") from error
    return source, text_content_hash(source)


def _guarded_digest(vault_root: Path, path: str, *, stale: bool) -> str:
    """SHA-256 of a vault file's bytes, read through the same no-follow guard as `_guarded_text`.

    A binary original has no text form, and for a text page this equals `text_content_hash` of its text.
    """
    try:
        PathGuard.capture(vault_root, path, leaf_policy="stable")
        raw, _guard = read_guarded_bytes(vault_root, vault_root / path)
    except PathGuardError as error:
        if error.code in {"PATH_GUARD_INVALID", "PATH_GUARD_ROOT", "PATH_GUARD_UNSAFE"}:
            raise _error("CURATION_PATH_UNSAFE", f"target {path!r} crosses an unsafe path") from error
        code = "CURATION_BINDING_STALE" if stale else "CURATION_TARGET_UNREADABLE"
        raise _error(code, f"target {path!r} is unavailable") from error
    except OSError as error:
        code = "CURATION_BINDING_STALE" if stale else "CURATION_TARGET_NOT_FOUND"
        raise _error(code, f"target {path!r} is unavailable") from error
    return hashlib.sha256(raw).hexdigest()


def _guarded_absent(vault_root: Path, path: str) -> bool:
    try:
        PathGuard.capture(vault_root, path, leaf_policy="absent")
        return True
    except PathGuardError as error:
        if error.code in {"PATH_GUARD_INVALID", "PATH_GUARD_ROOT", "PATH_GUARD_UNSAFE"}:
            raise _error("CURATION_PATH_UNSAFE", f"target {path!r} crosses an unsafe path") from error
        return False


def _read_page(
    vault_root: Path, raw: str, *, resolve: Callable[[str], str] | None = None
) -> tuple[str, str]:
    """The page `raw` names, read guarded. `resolve` is a caller's resolver over one identity
    census for many names (`memory_refs.identifier_resolver_read_only`)."""
    from . import memory_refs

    try:
        path = (
            resolve(raw)
            if resolve is not None
            else memory_refs.resolve_identifier_read_only(vault_root, raw)
        )
    except memory_refs.ReferenceError as error:
        raise _error(error.code, error.reason) from error
    path = normalize_target_path(path, field="work_item.path")
    source, _digest_value = _guarded_text(Path(vault_root), path, stale=False)
    return path, source


#: The two recurrence lanes, by the reason the review reports them under, and
#: the grammar id the substrate documents for the wikilink-only lane. A
#: wikilink-only candidate has no ordinary-text grammar to name, so `audit`
#: emits None for both grammar fields and the lane id lives here instead.
#: Follow-up outside this lane: audit should emit the lane id explicitly.
_ORDINARY_TEXT_REASON = "ordinary_identity_recurs"
_UNRESOLVED_WIKILINK_REASON = "unresolved_identity_recurs"
_UNRESOLVED_WIKILINK_GRAMMAR = "unresolved-wikilink-v1"


#: The one review family whose item carries an entity-candidate binding. Any other review
#: item names a page, and this lane opens that page for it.
ENTITY_CANDIDATE_FAMILY = "entity_recurrence"


def _entity_candidate_binding(item: Any, *, registry_fallback: str) -> dict[str, Any]:
    reasons = [
        reason
        for reason in list(getattr(item, "reasons", None) or [])
        if reason.get("category") == ENTITY_CANDIDATE_FAMILY
    ]
    if len(reasons) != 1:
        raise _error(
            "CURATION_ENTITY_CANDIDATE_INVALID",
            "review ref does not resolve to exactly one recurring identity signal",
        )
    meta = dict(reasons[0].get("meta") or {})
    contexts = [
        {
            "path": str(context.get("path") or "").replace("\\", "/").lstrip("/"),
            "context_hash": str(context.get("context_hash") or ""),
        }
        for context in list(meta.get("disconnected_contexts") or [])[:8]
    ]
    contexts.sort(key=lambda row: (row["path"], row["context_hash"]))
    target_refs = sorted(
        {
            str(candidate.get("path") or "")
            for candidate in list(meta.get("resolution_candidates") or [])
            if candidate.get("path")
        }
    )
    # No silent default for the state: defaulting it to "promotion" offers a
    # create-entity route for a candidate whose state we could not read, which
    # is the executable direction.
    if not str(meta.get("candidate_state") or "").strip():
        raise _error(
            "CURATION_ENTITY_CANDIDATE_INVALID",
            "recurring identity signal does not carry candidate_state",
        )
    # The grammar is a LANE, not a free-text field, and the review reports the
    # lane through its reasons.  A candidate that never matched the ordinary
    # text grammar is emitted with `grammar_version: None` by design, so a
    # presence check alone refuses the entire unresolved-wikilink lane.  Bind
    # that lane by name; anything we cannot place still fails closed rather than
    # sealing a binding against a grammar the review never named.
    reasons = {str(reason) for reason in list(meta.get("reasons") or [])}
    raw_grammar = meta.get("grammar_version")
    if raw_grammar is None and _ORDINARY_TEXT_REASON not in reasons:
        if _UNRESOLVED_WIKILINK_REASON not in reasons:
            raise _error(
                "CURATION_ENTITY_CANDIDATE_INVALID",
                "recurring identity signal does not name a recognised grammar lane",
            )
        grammar_version = _UNRESOLVED_WIKILINK_GRAMMAR
        predicate_digest = None
    elif not str(raw_grammar or "").strip():
        raise _error(
            "CURATION_ENTITY_CANDIDATE_INVALID",
            "recurring identity signal does not carry grammar_version",
        )
    else:
        grammar_version = str(raw_grammar)
        predicate_digest = meta.get("predicate_table_digest")
    registry_identity = str(meta.get("registry_fingerprint") or registry_fallback)
    binding = {
        "review_ref": str(getattr(item, "ref", None) or ""),
        "review_fingerprint": str(getattr(item, "fingerprint", None) or ""),
        "candidate_state": str(meta["candidate_state"]),
        "identity": str(meta.get("identity") or ""),
        "signal_version": str(meta.get("signal_version") or ""),
        "first_disconnected_context_batch": contexts,
        "remaining_disconnected_count": int(meta.get("remaining_disconnected_count") or 0),
        "batch_fingerprint": meta.get("batch_fingerprint"),
        "target_refs": target_refs,
        "grammar_identity": {
            "version": grammar_version,
            "predicate_table_digest": predicate_digest,
        },
        "registry_identity": registry_identity,
    }
    return _validate_entity_candidate_binding(binding)


def project_entity_candidate_binding(
    binding: Mapping[str, Any], *, hydration_recheck: int
) -> dict[str, Any]:
    """Project one identity-bound continuation under the eight-batch budget.

    The eighth terminal receipt may buy one final state read, but that read
    cannot expose an executable ninth batch: the redaction withholds the batch,
    its fingerprint and its pages from the response itself rather than returning
    them with an instruction to ignore them.

    What is NOT enforced here is the counter.  The ordinal arrives from the
    caller and the server does not bind a recheck to the receipt that earned it,
    so per the spec the session budget is agent-side: an agent that keeps asking
    for ordinal 1 is not stopped by this function.  Binding rechecks to terminal
    receipts server-side is a separate design question.
    """
    if type(hydration_recheck) is not int or not 1 <= hydration_recheck <= 8:
        raise _error(
            "INVALID_HYDRATION_RECHECK",
            "hydration_recheck must be an integer from 1 through 8",
        )
    projected = json.loads(canonical_json(dict(binding)))
    if projected.get("candidate_state") != "hydration":
        raise _error(
            "INVALID_HYDRATION_RECHECK",
            "identity-bound continuation requires a hydration candidate",
        )
    if hydration_recheck < 8:
        projected.update(executable=True, closure_only=False)
        return projected
    batch = list(projected.get("first_disconnected_context_batch") or [])
    deferred = len(batch) + int(projected.get("remaining_disconnected_count") or 0)
    projected.update(
        first_disconnected_context_batch=[],
        remaining_disconnected_count=deferred,
        batch_fingerprint=None,
        executable=False,
        closure_only=True,
        deferred_remaining_count=deferred,
    )
    return projected


def _current_entity_candidate_binding(
    vault_root: Path, review_ref: str
) -> tuple[Any, dict[str, Any]]:
    from . import attention as attention_module
    from . import entity_types as entity_types_module

    try:
        item = attention_module.entity_candidate_by_ref(vault_root, review_ref)
    except ValueError as error:
        raise _error(
            "CURATION_ENTITY_CANDIDATE_STALE",
            "entity candidate no longer resolves to the reviewed signal",
        ) from error
    registry_fallback = entity_types_module.load_entity_types(vault_root).fingerprint
    return item, _entity_candidate_binding(item, registry_fallback=registry_fallback)


def _candidate_step_kinds(state: str) -> list[str]:
    return {
        "promotion": ["create-entity"],
        "hydration": ["accept-relation", "edit"],
        "ambiguous": [],
    }[state]


def _pages_of_review_item(vault_root: Path, review_ref: str) -> list[str] | None:
    """The pages a current review item that is not an entity candidate stands for now, else None.

    A page-level item stands for its page; a vault-wide family's one item stands for the batch
    its family names (`attention.pages_of_item`). The audience filter of the review surface
    applies, so a page the caller may not see resolves to None exactly as a ref that names no
    item does.
    """
    from . import attention as attention_module

    batch = attention_module.pages_of_item(vault_root, review_ref)
    if batch is not None:
        return batch
    try:
        item = attention_module.item_by_ref(vault_root, review_ref)
    except ValueError:
        return None
    if any(reason.get("category") == ENTITY_CANDIDATE_FAMILY for reason in item.reasons):
        return None
    return [item.path]


def _page_work_item(
    vault_root: Path, selected: list[str], max_chars_per_page: int
) -> dict[str, Any]:
    """The bounded context of these explicitly named pages."""
    pages: list[dict[str, Any]] = []
    truncated: list[str] = []
    seen: set[str] = set()
    for raw in selected:
        path, source = _read_page(Path(vault_root), raw)
        if path in seen:
            continue
        seen.add(path)
        was_truncated = len(source) > max_chars_per_page
        if was_truncated:
            truncated.append(path)
        pages.append(
            {
                "path": path,
                "content": source[:max_chars_per_page],
                "content_hash": text_content_hash(source),
                "chars": len(source),
                "truncated": was_truncated,
            }
        )
    return {
        "action": "work-item",
        "mutated": False,
        "pages": pages,
        "truncation": {"pages": truncated, "disclosed": bool(truncated)},
        "registry_ids": registry_identities(Path(vault_root)),
        "step_schemas": step_schemas(),
    }


def work_item(
    vault_root: Path,
    *,
    refs: list[str] | None = None,
    paths: list[str] | None = None,
    review_ref: str | None = None,
    hydration_recheck: int | None = None,
    max_chars_per_page: int = 6_000,
) -> dict[str, Any]:
    """Assemble only explicitly named recorded context; never select or rank it."""
    if type(max_chars_per_page) is not int or not 1 <= max_chars_per_page <= MAX_WORK_ITEM_CHARS:
        raise _error("CURATION_WORK_ITEM_TOO_LARGE", "per-page character cap is invalid")
    if review_ref is not None:
        if refs or paths:
            raise _error(
                "CURATION_WORK_ITEM_MIXED",
                "a review item's work item cannot add unrelated refs or paths",
            )
        if hydration_recheck is not None and (
            type(hydration_recheck) is not int or not 1 <= hydration_recheck <= 8
        ):
            raise _error(
                "INVALID_HYDRATION_RECHECK",
                "hydration_recheck must be an integer from 1 through 8",
            )
        # A ref that was never well-formed is a caller error, not a review that
        # drifted.  Parsing it before the store lookup keeps STALE meaning
        # "re-read and retry", which is advice a malformed ref can never follow.
        from . import review_state as review_state_module

        try:
            review_state_module.parse_review_ref(str(review_ref))
        except ValueError as error:
            raise _error(
                "CURATION_ENTITY_REVIEW_REF_INVALID",
                "review_ref is not a well-formed review reference",
            ) from error
        try:
            item, binding = _current_entity_candidate_binding(
                Path(vault_root), str(review_ref)
            )
        except CurationError as error:
            if error.code != "CURATION_ENTITY_CANDIDATE_STALE":
                raise
            if hydration_recheck is None:
                # Not an entity candidate: a page-level item opens its page, as a ref to
                # that page does, and a vault-wide item opens the first pages of its batch.
                # A ref that names no current item keeps the refusal.
                covered = _pages_of_review_item(Path(vault_root), str(review_ref))
                if not covered:
                    raise
                opened = _page_work_item(
                    Path(vault_root), covered[:MAX_WORK_ITEM_PAGES], max_chars_per_page
                )
                if len(covered) > MAX_WORK_ITEM_PAGES:
                    opened["unopened_pages"] = len(covered) - MAX_WORK_ITEM_PAGES
                return opened
            # A continuation is read-only and identity-bound.  When the exact
            # row disappears after the terminal batch receipt, the useful
            # result is bounded closure rather than a misleading stale error.
            # Validate both inputs even though no executable binding is exposed.
            return {
                "action": "work-item",
                "mutated": False,
                "pages": [],
                "truncation": {"pages": [], "disclosed": False},
                "entity_candidate": {
                    "review_ref": str(review_ref),
                    "candidate_state": "quiet",
                    "closed": True,
                    "executable": False,
                    "closure_only": True,
                    "deferred_remaining_count": 0,
                },
                "candidate_evidence": {"candidate_state": "quiet", "closed": True},
                "allowed_candidate_step_kinds": [],
                "mutation_authority": "restructure_execution",
                "unknown_kind_route": {
                    "tool": "schema_memory",
                    "operation": "save-entity-types",
                    "authority": "restructure_execution",
                    "refresh_candidate_after_save": True,
                },
                "registry_ids": registry_identities(Path(vault_root)),
                "step_schemas": step_schemas(),
            }
        projected = (
            project_entity_candidate_binding(binding, hydration_recheck=hydration_recheck)
            if hydration_recheck is not None
            else binding
        )
        evidence = dict(item.reasons[0].get("meta") or {})
        # Read the batch from the PROJECTION, never the live binding: the
        # ordinal-8 projection has already redacted the ninth batch, and paging
        # the live binding would hand back the bytes the redaction withheld.
        closure_only = bool(projected.get("closure_only"))
        if closure_only:
            # Counts stay on entity_candidate so the agent still learns work
            # remains; the paths, hashes and fingerprint that would let it act
            # on that work this session do not travel.
            for withheld in (
                "disconnected_contexts",
                "batch_fingerprint",
                "remaining_disconnected_count",
            ):
                evidence.pop(withheld, None)
        context_paths = [
            context["path"]
            for context in (projected.get("first_disconnected_context_batch") or [])
        ]
        selected_pages: list[dict[str, Any]] = []
        truncated_pages: list[str] = []
        for path in context_paths:
            try:
                source, digest = _guarded_text(Path(vault_root), path, stale=False)
            except CurationError:
                # The review evidence itself remains sufficient and bounded for
                # append-only/protected contexts that curation is forbidden to
                # mutate.  Never weaken target policy merely to return full text.
                continue
            was_truncated = len(source) > max_chars_per_page
            if was_truncated:
                truncated_pages.append(path)
            selected_pages.append(
                {
                    "path": path,
                    "content": source[:max_chars_per_page],
                    "content_hash": digest,
                    "chars": len(source),
                    "truncated": was_truncated,
                }
            )
        return {
            "action": "work-item",
            "mutated": False,
            "pages": selected_pages,
            "truncation": {
                "pages": truncated_pages,
                "disclosed": bool(truncated_pages),
            },
            "entity_candidate": projected,
            "candidate_evidence": evidence,
            "allowed_candidate_step_kinds": _candidate_step_kinds(
                binding["candidate_state"]
            ),
            "mutation_authority": "restructure_execution",
            "unknown_kind_route": {
                "tool": "schema_memory",
                "operation": "save-entity-types",
                "authority": "restructure_execution",
                "refresh_candidate_after_save": True,
            },
            "registry_ids": registry_identities(Path(vault_root)),
            "step_schemas": step_schemas(),
        }
    if hydration_recheck is not None:
        raise _error(
            "INVALID_HYDRATION_RECHECK",
            "hydration_recheck requires one exact entity candidate review_ref",
        )
    selected = [*(refs or []), *(paths or [])]
    if not selected:
        raise _error("CURATION_WORK_ITEM_EMPTY", "work-item requires explicit refs or paths")
    if len(selected) > MAX_WORK_ITEM_PAGES:
        raise _error(
            "CURATION_WORK_ITEM_TOO_LARGE",
            f"work-item may contain at most {MAX_WORK_ITEM_PAGES} explicit pages",
        )
    return _page_work_item(vault_root, selected, max_chars_per_page)


def _read_target(vault_root: Path, path: str) -> tuple[str, str]:
    return _guarded_text(Path(vault_root), path, stale=True)


def _require_committable(
    review: Mapping[str, Any], validation: Mapping[str, Any], *, relation_pending: bool
) -> None:
    """Refuse a create or supersede step the leaf would refuse at commit.

    The review is held back from the leaf's validation, so the leaf cannot object
    to it there. A review the page does not need is no refusal: the leaf drops it
    at commit and says so in its warnings.
    """
    _refuse_blocked(
        validation.get("contract_result"), relation_answered=bool(review) or relation_pending
    )


def _refuse_blocked(contract_result: Any, *, relation_answered: bool) -> None:
    """Refuse at proposal a step whose leaf validation says its commit will block.

    A relation-disposition finding is set aside when the step answers it with a
    review the leaf could not see, and deferred to execution when the step links to
    a page an earlier step creates, which the leaf can judge only once that page
    exists. A truncated finding list cannot show that nothing else blocks.
    """
    from . import semantic_contract

    result = dict(contract_result or {})
    if not result.get("should_block"):
        return
    findings = list(result.get("blocking_findings") or [])
    if relation_answered:
        findings = [
            finding
            for finding in findings
            if tuple(finding.get("resolved_rule") or ())
            != semantic_contract.RELATION_DISPOSITION_RULE
        ]
        if not findings and not dict(result.get("omitted_counts") or {}).get("blocking_findings"):
            return
    detail = "; ".join(
        f"{finding.get('code')} at {finding.get('path')}: {finding.get('detail')}"
        for finding in findings
    )
    raise _error(
        "SEMANTIC_CONTRACT_BLOCKED",
        f"the step would block at commit: {detail or 'blocking findings were truncated'}",
    )


def _preservation_call(vault_root: Path, kind: str, args: Mapping[str, Any], *, prepared: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Use native custody and native publication, with one sealed core allocation."""
    from dataclasses import replace

    from . import (
        add,
        artifact_recovery,
        evidence_reuse,
        memory_refs,
        preserve,
        retained_inputs,
        schema,
    )
    from .governance.principal import effective_principal
    from .vault import PreparedBinaryContent, _NativePrepared

    original = args["original"]
    values = dict(prepared["normalized_native_args"] if prepared is not None else {key: value for key, value in args.items() if key != "original"})
    page = retained_inputs._read_snapshot(vault_root, original["reference"])  # noqa: SLF001
    who = replace(effective_principal(), administrative_ingress=False)
    with artifact_recovery.stage(vault_root, page.page.path, who, expected_sha256=original["version"]) as (held, descriptor):
        if held.size != original["size"] or held.held is None:
            raise artifact_recovery.RecoveryUnavailable
        if prepared is None and kind == "preserve-evidence":
            if evidence_reuse.already_in_family(vault_root, scope=values["scope"], category=values["category"], sha256=held.sha256, protected=values.get("raw_protection", False)):
                raise _error("EPISODE_PRIOR_PRESERVATION", "original already preserved at this native destination")
        with memory_refs._allocated_ids(prepared["memory_ids"] if prepared is not None else None) as identities:
            def validate_inputs() -> None:
                artifact_recovery.require_current(vault_root, held, who)
                if prepared is not None and identities != prepared["memory_ids"]:
                    raise _error("CURATION_BINDING_STALE", "native page allocation count changed")

            def stop(snapshot: dict) -> None:
                core, references = [], {}
                for write in snapshot.pop("writes"):
                    row = {"path": write.path.relative_to(vault_root).as_posix()}
                    if isinstance(write.content, PreparedBinaryContent):
                        row.update(content_hash=write.content.sha256, size=write.content.size)
                    else:
                        row["content_hash"] = text_content_hash(write.content)
                        reference = memory_refs.ref_from_markdown(write.content)
                        if reference is not None:
                            references[row["path"]] = reference
                    core.append(row)
                raise _NativePrepared({**snapshot, "memory_ids": list(identities), "core": core, "references": references,
                                      "original_descriptor": {key: descriptor[key] for key in ("file_name", "mime_type")}})

            with os.fdopen(os.dup(held.held.stream.fileno()), "rb") as stream:
                internal = {"_allocation": prepared["allocation"] if prepared is not None else None,
                            "_prepare": stop if prepared is None else None, "_validate_inputs": validate_inputs}
                try:
                    binary = prepared["original_descriptor"] if prepared is not None else descriptor
                    if kind == "capture-source":
                        result = add.add(vault_root, schema.load_source_schema(vault_root),
                                         content=values.pop("content", ""), artifact=add.SourceArtifact(stream, binary["file_name"], binary["mime_type"]), **values, **internal)
                    else:
                        result = preserve.preserve(vault_root, content_stream=stream, **values, **internal)
                except _NativePrepared as snapshot:
                    return snapshot.value
            return result.as_dict()


#: The steps that rename a page: a `move`, and the inverse move that compensates one.
# nosemgrep: ep-word-set -- the two step kinds this module defines that rename a page.
_RENAME_STEP_KINDS: Final[frozenset[str]] = frozenset({"move", UNMOVE_STEP_KIND})
#: The two run phases that leave a step to run; every other phase ends the run.
# nosemgrep: ep-word-set -- the two run phases this module defines that leave a step to run.
_OPEN_RUN_PHASES: Final[frozenset[str]] = frozenset({"executing", "compensating"})
#: The steps that rewrite pages in place to sealed bytes, append-only pages among them.
# nosemgrep: ep-word-set -- the three step kinds this module defines that rewrite sealed pages.
_REWRITE_STEP_KINDS: Final[frozenset[str]] = frozenset(
    {REPOINT_STEP_KIND, UNREPOINT_STEP_KIND, REVERT_POINTER_STEP_KIND}
)


@dataclass
class _Chain:
    """What the earlier steps of a plan leave, for the steps that may bind it (`_CHAINED_STEP_KINDS`).

    `pages` holds each page an earlier step leaves at exact bytes, by path, with that step's id
    and hash; `vacated` holds each path an earlier step removes and no later one writes; `moves`
    holds the plan's forward moves in order. `inverse_moves` judges the plan's inverse moves
    against that state (`semantic_writes.ProjectedMoves`), made on the first one.
    """

    moves: list[tuple[str, str]] = field(default_factory=list)
    pages: dict[str, tuple[str, str, str]] = field(default_factory=dict)
    vacated: set[str] = field(default_factory=set)
    inverse_moves: Any = None

    def text(self, vault_root: Path, path: str) -> str:
        """The page's text as the earlier steps leave it, else as the vault holds it now."""
        held = self.pages.get(path)
        if held is not None:
            return held[2]
        source, _hash = _read_target(vault_root, path)
        return source

    def projection(self) -> dict[str, str | None]:
        """Each page the earlier steps leave at exact bytes, and None at each path they vacate."""
        return {
            **dict.fromkeys(self.vacated),
            **{path: text for path, (_step, _hash, text) in self.pages.items()},
        }


def _require_lifecycle_settled(vault_root: Path, pages: Mapping[str, str]) -> None:
    """Refuse a relocation step, at proposal, on a page whose lifecycle record it cannot carry."""
    from . import semantic_writes

    try:
        semantic_writes.require_lifecycle_settled(vault_root, pages)
    except semantic_writes.SemanticWriteError as error:
        raise _error(error.code, error.reason) from error


def _move_call(vault_root: Path, args: Mapping[str, Any]) -> dict[str, Any]:
    """The move leaf's arguments for a curation `move` step.

    The step moves bytes and lineage. Its `fields` patch rides the move's content transform, so the
    page changes where it belongs in the same commit; the plan's `repoint-links` step rewrites the
    links, so the leaf's `update_wikilinks` stays off.
    """
    from . import move_file as move_module
    from . import page_fields
    from . import set_frontmatter_field as frontmatter

    call: dict[str, Any] = {
        key: args[key]
        for key in ("old_path", "new_path", "allow_curated", "promotion_reason")
        if key in args
    }
    call["update_wikilinks"] = False
    fields = dict(args.get("fields") or {})
    if not fields:
        return call
    pointers = page_fields.pointer_fields(vault_root)
    refused = sorted(name for name in fields if name not in pointers)
    if refused:
        raise _error(
            "CURATION_STEP_INVALID",
            f"a move's fields patch sets only pointer fields, and {refused[0]!r} is not one",
        )
    destination = str(args["new_path"])

    def patch(text: str) -> str:
        try:
            return frontmatter.apply_pointer_patch(vault_root, destination, text, fields)
        except frontmatter.SetFrontmatterError as error:
            raise move_module.MoveFileError(code=error.code, reason=error.reason) from error

    try:
        call["extra_writes"] = frontmatter.pointer_patch_writes(
            vault_root, fields, folders=args.get("project_folders")
        )
    except frontmatter.SetFrontmatterError as error:
        raise _error(error.code, error.reason) from error
    call["content_transform"] = patch
    return call


def _unmove_call(material: Mapping[str, Any]) -> dict[str, Any]:
    """The move leaf's arguments for an inverse move: the sealed line delta alone decides the
    bytes, so the forward move's lineage entry and `fields` patch go and no entry is added."""
    from . import duplicate_fold
    from . import move_file as move_module

    def restore(text: str) -> str:
        if text_content_hash(text) != material["before_hash"]:
            raise move_module.MoveFileError(
                code="SOURCE_CHANGED", reason="the page changed since its inverse move was sealed"
            )
        return duplicate_fold._apply(  # noqa: SLF001 - the shared sealed line delta
            text, material["restore"], forward=False, path=str(material["old_path"])
        )

    return {
        "old_path": material["old_path"],
        "new_path": material["new_path"],
        "update_wikilinks": False,
        "allow_curated": bool(material.get("allow_curated", False)),
        "content_transform": restore,
        "_exact_content": True,
    }


def _takes_pointer_route(vault_root: Path, args: Mapping[str, Any]) -> bool:
    """Whether an `edit` step lands through the governed pointer write.

    An append-only page has no other route (proposal admits only a frontmatter patch there). Any
    other page takes it for a patch of one pointer field that no operation owns, so where a page
    belongs changes reversibly for every page type, the types that `replace_memory` cannot
    re-create among them. The field set and the owners come from the page-field registry.
    """
    from . import page_fields

    if in_append_only_tree(args["path"]) is not None:
        return True
    operation = args.get("operation") or {}
    field = operation.get("field")
    return (
        operation.get("kind") == "patch_frontmatter"
        and field in page_fields.pointer_fields(vault_root)
        and field not in page_fields.writers(vault_root)
    )


def _prepare_step(
    vault_root: Path,
    step: Mapping[str, Any],
    ordinal: int,
    *,
    relation_pending: bool = False,
    projection: Any = None,
    chain: _Chain | None = None,
) -> dict[str, Any]:
    """Run the existing leaf's read-only preparation or exact structural guards.

    `chain` holds what the plan's earlier steps leave, which a `repoint-links` step and the
    relocation inverses read. The item's `exact` key maps each page the step leaves at exact
    bytes to those bytes; `_prepare_manifest` takes it off before sealing.
    """
    from . import (
        commands,
        relation_queue,
        vocabulary_resolution,
    )
    from . import delete_file as delete_module
    from . import (
        link as link_module,
    )
    from . import move_file as move_module
    from . import (
        note as note_module,
    )
    from . import (
        recover_from_trash as recover_module,
    )
    from .collection_store import curation_step
    from .collection_store.preview import selected_writer

    kind = str(step["kind"])
    args = dict(step["args"])
    prepared: dict[str, Any] = {}
    preimage: str | None = None
    before_hash: str | None = None
    expected_absent = False
    effect_before: list[dict[str, Any]] = []
    effect_after: list[dict[str, Any]] = []
    exact: dict[str, str] = {}
    chain = chain if chain is not None else _Chain()

    try:
        if kind in PRESERVATION_STEP_KINDS:
            native = _preservation_call(vault_root, kind, args)
            prepared = {**native, "destination": native["allocation"]["destination"]}
            path = prepared["destination"]
            expected_absent = True
            effect_before = [{"path": item["path"], "absent": True} for item in native["core"]]
            effect_after = [{"path": item["path"], "content_hash": item["content_hash"]} for item in native["core"]]
            after_hash = next(item["content_hash"] for item in native["core"] if item["path"] == path)
        elif kind == "create-note":
            review = {
                key: args.pop(key)
                for key in ("relation_disposition", "relation_review_reason")
                if key in args
            }
            preflight = commands._invoke_note(
                vault_root,
                validate_only=True,
                note_type=args.get("note_type", "insight"),
                **{key: value for key, value in args.items() if key != "note_type"},
            )
            if isinstance(preflight, vocabulary_resolution.VocabularyPreparation):
                raise _error(
                    "CURATION_VOCABULARY_PREPARATION_REQUIRED",
                    canonical_json({"vocabulary_preparation": preflight.evidence}),
                )
            validation = preflight.as_dict()
            _require_committable(review, validation, relation_pending=relation_pending)
            path = normalize_target_path(validation["destination"], field="destination")
            if not _guarded_absent(vault_root, path):
                raise _error("CURATION_BINDING_STALE", f"create destination {path!r} exists")
            expected_absent = True
            prepared = {
                "draft_id": validation["draft_id"],
                "draft_hash": validation["draft_hash"],
                "draft_token": validation["draft_token"],
                "destination": validation["destination"],
                **review,
            }
            if review:
                prepared["relation_review_hash"] = validation["draft_hash"]
            after_hash = text_content_hash(preflight.source)
            effect_before = [{"path": path, "absent": True}]
            effect_after = [{"path": path, "content_hash": after_hash}]
        elif kind == "edit" and _takes_pointer_route(vault_root, args):
            from . import duplicate_fold
            from . import set_frontmatter_field as frontmatter

            # A pointer correction (`_takes_pointer_route`): the leaf refuses any field that is not
            # a registry pointer, or that another operation owns. Its undo reverts the pointer
            # write's record or the sealed line delta, so no preimage is sealed.
            path = args["path"]
            source, before_hash = _read_target(vault_root, path)
            operation = dict(args["operation"])
            if operation.get("expected_hash") != before_hash:
                raise _error("CURATION_BINDING_STALE", f"reviewed hash for {path!r} is stale")
            try:
                frontmatter.set_pointer_field(
                    vault_root,
                    path=path,
                    field=operation["field"],
                    value=operation.get("value"),
                    why=args["why"],
                    expected_hash=before_hash,
                    validate_only=True,
                )
            except frontmatter.SetFrontmatterError as error:
                raise _error(error.code, error.reason) from error
            after_text = frontmatter.apply_pointer_patch(
                vault_root, path, source, {operation["field"]: operation.get("value")}
            )
            # The pointer write sets no `updated`, so the sealed bytes are exactly the bytes it lands.
            prepared = {"pointer": True}
            if in_append_only_tree(path) is None:
                # A writable page reverts by this line delta, as a repoint's writable rows do.
                prepared["restore"] = duplicate_fold._delta(source, after_text)  # noqa: SLF001
            after_hash = text_content_hash(after_text)
            exact[path] = after_text
            effect_before = [{"path": path, "content_hash": before_hash}]
            effect_after = [{"path": path, "content_hash": after_hash}]
        elif kind == "edit":
            path = args["path"]
            preimage, before_hash = _read_target(vault_root, path)
            from .vault import parse_frontmatter

            frontmatter, _body, _raw = parse_frontmatter(preimage, strict=True)
            # Compensation replays through the typed writer, which places only its own types.
            if str(frontmatter.get("type") or "") not in note_module.NOTE_TYPES:
                raise _error(
                    "CURATION_COMPENSATION_UNAVAILABLE",
                    "edit target has no history-preserving compensation leaf",
                )
            expected_hash = args["operation"].get("expected_hash")
            if expected_hash != before_hash:
                raise _error("CURATION_BINDING_STALE", f"reviewed hash for {path!r} is stale")
            operation = {**args["operation"], "validate_only": True}
            validation = commands.op_edit_memory(
                vault_root, path=path, why=args["why"], operation=operation
            )
            semantic = dict(validation.get("semantic") or {})
            _refuse_blocked(semantic.get("contract_result"), relation_answered=relation_pending)
            prepared = {
                "transition_token": semantic.get("transition_token"),
                "relation_review_hash": semantic.get("relation_review_hash"),
            }
            after_hash = semantic.get("after_hash")
            effect_before = [{"path": path, "content_hash": before_hash}]
            effect_after = [{"path": path, "content_hash": after_hash}]
        elif kind == REMOVE_RELATION_STEP_KIND:
            from . import promotions

            path = args["subject"]
            # A withheld subject answers exactly as a missing one.
            if not promotions.page_visible(vault_root, path):
                raise _error("CURATION_BINDING_STALE", f"target {path!r} is unavailable")
            _source, page_hash = _read_target(vault_root, path)
            if not promotions.edge_present(vault_root, path, args["predicate"], args["target"]):
                raise _error("CURATION_RELATION_ABSENT", f"no {args['predicate']!r} edge on {path!r}")
            # No hash binds this step: a concurrent edit of another part of the page survives. The
            # page hash rides in `prepared` for the plan fingerprint only, so the revert the user
            # saw is the revert that runs.
            prepared = {"page_hash": page_hash}
            before_hash = None
            after_hash = None
            effect_before = []
            effect_after = [{"path": path, "exists": True}]
        elif kind == OBSERVE_UNIT_STEP_KIND:
            path = args["path"]
            preimage, before_hash = _read_target(vault_root, path)
            observation = dict(args["observation"])
            if observation["expected_hash"] != before_hash:
                raise _error("CURATION_BINDING_STALE", "the reviewed parent hash changed")
            validation = commands.op_observe_memory(
                vault_root, path=path, operation="validate", reason=args["why"], **observation,
            )
            semantic = dict(validation.get("semantic") or {})
            prepared = {"transition_token": semantic.get("transition_token")}
            after_hash = validation["after_hash"]
            effect_before = [{"path": path, "content_hash": before_hash}]
            effect_after = [{"path": path, "content_hash": after_hash}]
        elif kind == "supersede":
            path = args["old_path"]
            preimage, before_hash = _read_target(vault_root, path)
            review = {
                key: args.pop(key)
                for key in ("relation_disposition", "relation_review_reason")
                if key in args
            }
            validation = commands.op_replace_memory(vault_root, validate_only=True, **args)
            preparation = validation.get("vocabulary_preparation")
            if isinstance(preparation, Mapping):
                raise _error(
                    "CURATION_VOCABULARY_PREPARATION_REQUIRED",
                    canonical_json({"vocabulary_preparation": preparation}),
                )
            _require_committable(review, validation, relation_pending=relation_pending)
            prepared = {
                "draft_id": validation["draft_id"],
                "draft_hash": validation["draft_hash"],
                "draft_token": validation["draft_token"],
                "destination": validation["destination"],
                **review,
            }
            if review:
                prepared["relation_review_hash"] = validation["draft_hash"]
            after_hash = validation.get("content_fingerprint") or validation.get("draft_hash")
            destination = normalize_target_path(validation["destination"], field="destination")
            if not _guarded_absent(vault_root, destination):
                raise _error(
                    "CURATION_BINDING_STALE", f"supersession destination {destination!r} exists"
                )
            effect_before = [
                {"path": path, "content_hash": before_hash},
                {"path": destination, "absent": True},
            ]
            effect_after = [
                {"path": path, "exists": True},
                {"path": destination, "exists": True},
            ]
        elif kind == "create-entity":
            validation = link_module.link(vault_root, validate_only=True, **args)
            if isinstance(validation, link_module.IdentityPreparation):
                raise _error(
                    "CURATION_IDENTITY_DECISION_REQUIRED",
                    canonical_json(validation.as_dict()),
                )
            path = normalize_target_path(validation.path, field="destination")
            if not _guarded_absent(vault_root, path):
                raise _error("CURATION_BINDING_STALE", f"entity destination {path!r} exists")
            expected_absent = True
            prepared = {"destination": path}
            after_hash = None
            effect_before = [{"path": path, "absent": True}]
            effect_after = [{"path": path, "exists": True}]
        elif kind == "accept-relation":
            resolved = relation_queue.resolve_candidate(vault_root, args["ref"])
            if resolved.fingerprint != args["expected_fingerprint"]:
                raise _error("CURATION_BINDING_STALE", "relation fingerprint is stale")
            path = normalize_target_path(str(resolved.candidate.get("from") or ""), field="path")
            preimage, before_hash = _read_target(vault_root, path)
            if before_hash != args["expected_hash"]:
                raise _error("CURATION_BINDING_STALE", "relation target hash is stale")
            # A step cannot name a relation, so a candidate that needs a chosen one is accepted on
            # the relation queue instead.
            relation_queue.require_relation_choice(
                vault_root, resolved.candidate, None, page=resolved.source_page
            )
            prepared = {"candidate": dict(resolved.candidate)}
            after_hash = None
            effect_before = [{"path": path, "content_hash": before_hash}]
            effect_after = [{"path": path, "exists": True}]
        elif (
            kind in RECORDS_STEP_KINDS
            and selected_writer(vault_root, args["collection"]) is not None
        ):
            # The store owns this collection and this runs on its thread. The writer prepares the
            # step; the seal binds the store's guards, never the generated views' paths and hashes.
            found = curation_step.prepare(
                vault_root, "append" if kind == RECORDS_STEP_KIND else "update", args
            )
            path = normalize_target_path(found["path"], field="destination", allow_records=True)
            prepared = {
                "destination": path,
                "collection_id": found["collection_id"],
                "manifest_path": normalize_target_path(
                    found["manifest_path"], field="collection", allow_records=True
                ),
                "item_key": found["item_key"],
                "store": {
                    "collection_id": found["collection_id"],
                    "expected_container_hash": found["expected_container_hash"],
                    "expected_item_version": found["expected_item_version"],
                },
            }
            if kind == RECORDS_STEP_KIND:
                prepared["payload_hash"] = found["payload_hash"]
            after_hash = None
            effect_after = [{"path": path, "exists": True}]
        elif kind in PLANNING_STEP_KINDS:
            from . import planning

            action = PLANNING_STEP_ACTIONS[kind]
            native = planning.prepare(vault_root, action, args)
            path = native["path"]
            prepared = {**native, "destination": path}
            after_hash = None
            if native.get("store") is not None:
                effect_before = []
            else:
                path = normalize_target_path(path, _planning_tree=True)
                manifest_path = normalize_target_path(native["manifest_path"], _planning_tree=True)
                _text, manifest_hash = _read_target(vault_root, manifest_path)
                if kind == "add-plan":
                    expected_absent = True
                    effect_before = [{"path": path, "absent": True}]
                else:
                    before_hash = native["before_item_hash"]
                    effect_before = [{"path": path, "content_hash": before_hash}]
                effect_before.append({"path": manifest_path, "content_hash": manifest_hash})
            effect_after = [{"path": path, "exists": True}]
        elif kind in RECORDS_STEP_KINDS:
            from . import record_governance
            from . import records as records_module
            from . import structured_collections as collections

            # The Records owner's own gates, in the order `record_memory`
            # applies them: a released collection, then the records profile.
            try:
                resolved = record_governance.resolve_collection_for_mutation(
                    vault_root, args["collection"]
                )
            except collections.CollectionError as error:
                # Raised only to a caller who may know the collection; others hear it is absent.
                if error.code == "COLLECTION_STORE_UNAVAILABLE":
                    raise _records_store_unsupported(kind) from error
                raise
            manifest = record_governance.require_records_profile(resolved)
            if kind == RECORDS_STEP_KIND:
                appended = records_module.prepare_append(
                    vault_root,
                    manifest,
                    item=args["item"],
                    item_key=args.get("item_key"),
                    body=args.get("body"),
                    expected_container_hash=args["expected_container_hash"],
                    why=args["why"],
                )
                if appended["body"] != (args.get("body") or ""):
                    step["args"]["body"] = appended["body"]
            else:
                appended = records_module.prepare_update(
                    vault_root,
                    manifest,
                    **{name: value for name, value in args.items() if name != "collection"},
                )
            if appended["semantic_profile"] != "records":
                raise _error("RECORDS_PROFILE_REQUIRED", "a Records leaf writes to Records only")
            require_item_files(manifest)
            path = normalize_target_path(appended["path"], field="destination", allow_records=True)
            manifest_path = normalize_target_path(
                appended["manifest_path"], field="collection", allow_records=True
            )
            prepared = {
                "destination": path,
                "collection_id": appended["collection_id"],
                "manifest_path": manifest_path,
                "item_key": appended["item_key"],
            }
            if kind == RECORDS_STEP_KIND:
                _manifest_text, manifest_hash = _read_target(vault_root, manifest_path)
                if not _guarded_absent(vault_root, path):
                    raise _error("CURATION_BINDING_STALE", f"record destination {path!r} exists")
                expected_absent = True
                prepared["payload_hash"] = appended["payload_hash"]
                effect_before = [{"path": path, "absent": True}]
            else:
                manifest_hash = appended["before_manifest_hash"]
                before_hash = appended["before_item_hash"]
                prepared.update(
                    before_manifest_hash=manifest_hash,
                    before_container_hash=appended["before_container_hash"],
                    before_item_hash=before_hash,
                )
                effect_before = [{"path": path, "content_hash": before_hash}]
            after_hash = None
            # Every Records transition rewrites the manifest's audit head, so
            # its hash stands for the whole collection as reviewed.
            effect_before.append({"path": manifest_path, "content_hash": manifest_hash})
            effect_after = [{"path": path, "exists": True}]
        elif kind in FOLD_STEP_KINDS:
            from . import duplicate_fold, episode_fold

            folded = {
                FOLD_RECAP_STEP_KIND: lambda: episode_fold.prepare(vault_root, args),
                UNFOLD_RECAP_STEP_KIND: lambda: episode_fold.prepare_unfold(vault_root, args),
                FOLD_DUPLICATE_STEP_KIND: lambda: duplicate_fold.prepare(vault_root, args, projection),
                UNFOLD_DUPLICATE_STEP_KIND: lambda: duplicate_fold.prepare_unfold(vault_root, args),
            }[kind]()
            path, before_hash, after_hash = folded["path"], folded["before_hash"], folded["after_hash"]
            prepared = folded["prepared"]
            effect_before, effect_after = folded["effect_before"], folded["effect_after"]
        elif kind in _RENAME_STEP_KINDS:
            from . import duplicate_fold, semantic_writes

            if kind == "move":
                call = _move_call(vault_root, args)
            else:
                material = dict(args["move"])
                for key in ("old_path", "new_path"):
                    material[key] = normalize_target_path(material.get(key), field=f"move.{key}")
                # The leaf judges the inverse move's policy; its bytes come from the sealed delta,
                # applied below to the page as the plan's earlier steps leave it, and its contract
                # is judged from that state too.
                call = {
                    **_unmove_call(material),
                    "content_transform": lambda text: text,
                    "_caller_judges_contract": True,
                }
            validation = move_module.move_file(vault_root, validate_only=True, **call)
            if not isinstance(validation, move_module.MoveFileValidation):
                raise _error("CURATION_STEP_INVALID", "move leaf did not return a validation")
            if not validation.atomic_supported:
                raise _error(
                    "CURATION_ATOMICITY_UNAVAILABLE",
                    "curation move requires one unpaired Markdown page",
                )
            path = normalize_target_path(validation.result.old_path, field="old_path")
            destination = normalize_target_path(validation.result.new_path, field="new_path")
            if path not in chain.pages:
                # An earlier step of the plan carries the record of a page it leaves.
                _require_lifecycle_settled(vault_root, {path: chain.text(vault_root, path)})
            if kind == "move":
                # The undo lands the pre-step bytes from the sealed `restore` delta, so no preimage.
                source, before_hash = _read_target(vault_root, path)
                after_text = validation.after_text
                if after_text is None or after_text == source:
                    # Recovery tells a rename that ran from one that did not by the destination's
                    # bytes, so a move must change them: it records lineage in the frontmatter.
                    raise _error(
                        "CURATION_ATOMICITY_UNAVAILABLE",
                        "a curation move records the page's lineage, which needs a frontmatter block",
                    )
                prepared = {
                    "destination": destination,
                    "rename_after": list(validation.rename_after),
                    # The inverse move's bytes: the line delta back to the pre-step page.
                    "restore": duplicate_fold._delta(source, after_text),  # noqa: SLF001
                }
            else:
                current = chain.text(vault_root, path)
                before_hash = text_content_hash(current)
                if before_hash != material["before_hash"]:
                    raise _error("CURATION_BINDING_STALE", f"{path!r} changed since its move")
                after_text = duplicate_fold._apply(  # noqa: SLF001 - the sealed line delta
                    current, material["restore"], forward=False, path=path
                )
                if text_content_hash(after_text) != material["after_hash"]:
                    raise _error(
                        "CURATION_BINDING_STALE", f"{path!r} no longer returns to its pre-step bytes"
                    )
                # An undo restores the links before it moves the pages back, so a page that linked
                # this one names its old path again by then.
                if chain.inverse_moves is None:
                    chain.inverse_moves = semantic_writes.ProjectedMoves(vault_root)
                chain.inverse_moves.refuse_blocked(
                    old_path=path,
                    new_path=destination,
                    source=current,
                    moved_source=after_text,
                    projection=chain.projection(),
                )
                prepared = {"destination": destination}
            expected_absent = True
            after_hash = text_content_hash(after_text)
            exact[destination] = after_text
            effect_before = [
                {"path": path, "content_hash": before_hash},
                {"path": destination, "absent": True},
            ]
            effect_after = [
                {"path": path, "absent": True},
                {"path": destination, "content_hash": after_hash},
            ]
        elif kind == REPOINT_STEP_KIND:
            from . import repoint_links

            sealed = repoint_links.prepare(
                vault_root,
                chain.moves,
                {page: text for page, (_s, _h, text) in chain.pages.items()},
                only=args.get("referrers"),
            )
            rows = sealed["pages"]
            path = str(rows[0]["path"])
            prepared = sealed
            after_hash = rows[0]["after_hash"]
            effect_before = [{"path": row["path"], "content_hash": row["before_hash"]} for row in rows]
            effect_after = [{"path": row["path"], "content_hash": row["after_hash"]} for row in rows]
        elif kind in (UNREPOINT_STEP_KIND, REVERT_POINTER_STEP_KIND):
            from . import repoint_links

            (material,) = args.values()
            rows = list(material["pages"])
            for row in rows:
                normalize_target_path(row.get("path"), field="pages.path", allow_append_only=True)
            restored = repoint_links.restorations(
                vault_root, material, lambda page: chain.text(vault_root, page)
            )
            _require_lifecycle_settled(
                vault_root,
                {
                    page: current
                    for page, current, _before, revert in restored
                    if revert is None and page not in chain.pages
                },
            )
            exact = {page: before for page, _current, before, _revert in restored}
            path = str(rows[0]["path"])
            after_hash = rows[0]["before_hash"]
            effect_before = [{"path": row["path"], "content_hash": row["after_hash"]} for row in rows]
            effect_after = [{"path": row["path"], "content_hash": row["before_hash"]} for row in rows]
        elif kind == "delete":
            path = args["path"]
            # The undo recovers the exact trash identity its receipt seals, so no preimage.
            _source, before_hash = _read_target(vault_root, path)
            validation = delete_module.delete_file(vault_root, validate_only=True, **args)
            if not isinstance(validation, delete_module.DeleteFileValidation):
                raise _error("CURATION_STEP_INVALID", "delete leaf did not return a validation")
            after_hash = None
            effect_before = [{"path": path, "content_hash": before_hash}]
            effect_after = [{"path": path, "absent": True}]
        else:
            assert kind == "recover"
            path = args["trash_path"]
            # The undo deletes the restored path again, so no preimage.
            _source, before_hash = _read_target(vault_root, path)
            validation = recover_module.recover_from_trash(
                vault_root,
                trash_path=path,
                restore_path=args.get("restore_path"),
                allow_curated=bool(args.get("allow_curated", False)),
                validate_only=True,
            )
            destination = normalize_target_path(validation.restored_path, field="restore_path")
            if not _guarded_absent(vault_root, destination):
                raise _error(
                    "CURATION_BINDING_STALE",
                    f"recovery destination {destination!r} exists",
                )
            expected_absent = True
            prepared = {"destination": destination}
            after_hash = before_hash
            effect_before = [
                {"path": path, "content_hash": before_hash},
                {"path": destination, "absent": True},
            ]
            effect_after = [
                {"path": path, "absent": True},
                {"path": destination, "content_hash": before_hash},
            ]
    except CurationError:
        raise
    except Exception as error:
        code = getattr(error, "code", None)
        reason = getattr(error, "reason", None)
        message = str(reason or error)
        if not isinstance(code, str):
            code = str(error).split(":", 1)[0] if ":" in str(error) else "CURATION_STEP_INVALID"
        raise _error(code, message) from error

    return {
        "ordinal": ordinal,
        "step_id": step["step_id"],
        "kind": kind,
        "path": path,
        "before_hash": before_hash,
        "expected_absent": expected_absent,
        "prepared": prepared,
        "postcondition": {"path": prepared.get("destination", path), "content_hash": after_hash},
        "effect_before": effect_before,
        "effect_after": effect_after,
        "preimage": preimage,
        "exact": exact,
    }


# The argument a creating step's leaf registers as its page's title (note.py, link.py `add_pending`).
_CREATED_TITLE_ARGUMENT = {"create-note": "title", "supersede": "title", "create-entity": "name"}


#: The step kinds whose preparation leaves state outside their binding item: a duplicate fold's
#: projection and an inverse move's projected contract. A `Preparation` keeps no such step.
_UNKEPT_STEP_KINDS: Final[frozenset[str]] = _DUPLICATE_FOLD_KINDS | _RELOCATION_INVERSE_KINDS


def _step_digest(step: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(step)).hexdigest()


@dataclass
class Preparation:
    """The steps of a derived plan prepared so far, so that preparing can stop between steps and
    a later call can go on from them (`prepare_proposal`).

    `steps` holds, for each prepared step in plan order, the step's digest, its binding item and
    the exact bytes it leaves, which later steps of the plan read. `steps` and `registry_ids`
    are plain JSON, so a caller can keep them between calls. `this_call` and `slowest` count only
    the steps this object prepared itself.
    """

    steps: list[dict[str, Any]] = field(default_factory=list)
    #: The registry identities the kept steps were prepared under.
    registry_ids: dict[str, str] = field(default_factory=dict)
    this_call: int = 0
    slowest: float = 0.0

    def keep_opening(
        self, steps: Sequence[Mapping[str, Any]], registry_ids: Mapping[str, str]
    ) -> None:
        """Keep only the prepared steps that open `steps` unchanged, under the same registries.

        A step's binding item depends only on the vault, the registries and the steps before
        it, so a kept step is the one a fresh preparation would make. The seal still checks
        every binding against the vault.
        """
        if any(self.registry_ids.get(name, value) != value for name, value in registry_ids.items()):
            self.steps.clear()
        kept = 0
        # The kept steps may outnumber the new plan's, or fall short of it.
        for step, done in zip(steps, self.steps, strict=False):
            if done["digest"] != _step_digest(step) or step["kind"] in _UNKEPT_STEP_KINDS:
                break
            kept += 1
        del self.steps[kept:]
        self.registry_ids = dict(registry_ids)


class PreparationPaused(Exception):
    """Preparing stopped between steps because its caller's time ran short. The `Preparation`
    holds every step prepared so far."""


def _prepare_manifest(
    vault_root: Path,
    steps: Sequence[Mapping[str, Any]],
    *,
    preparation: Preparation | None = None,
    stop: Callable[[float], bool] | None = None,
) -> list[dict[str, Any]]:
    """Prepare each step with links to the pages the plan's earlier steps create.

    A later step may link to a page an earlier step creates, so its links are
    rendered as they will be once that page exists. Only that rendering, and an
    inverse move's contract, are projected; the other semantic checks still read
    the vault as it is. A plan that projection cannot make run as sealed is
    refused here instead of stopping part-way: a later step bound to a path an
    earlier step writes, a step that links to a page an earlier step supersedes,
    and a step that gives a relation disposition while it links to a page an
    earlier step creates (that link is its relation once the page exists). A
    step whose leaf validation already blocks is refused too
    (`_refuse_blocked`). Relocation steps, which remove pages, are refused
    before any step runs (`_require_plan_relocation_history`).

    A step of `_CHAINED_STEP_KINDS` may bind a page that an earlier step leaves at
    exact bytes (a move's destination, a pointer correction, an inverse's restored
    page) at that step's sealed after hash. The item's `chained` map names that
    earlier step for each such path, and `_binding_blockers` checks the path only
    once that step has committed.

    With a `preparation`, the steps it holds count as prepared, and each step
    prepared here is added to it. Before each later step, `stop` is asked with the
    slowest step this call prepared; when it answers true, `PreparationPaused`
    ends the call. The first step of a call always runs, so every call advances.
    """
    import time

    from . import duplicate_fold
    from . import find as find_module

    manifest: list[dict[str, Any]] = []
    created: list[tuple[str, str | None]] = []
    superseded: list[tuple[str, str | None]] = []
    written: set[str] = set()
    # The pages that earlier duplicate folds of this plan rewrite: a later fold of the same canonical
    # page reads them through `projection`, which proves each one's whole chain of hashes.
    chained: set[str] = set()
    projection = duplicate_fold.Projection()
    chain = _Chain()

    def record(step: Mapping[str, Any], item: dict[str, Any], exact: Mapping[str, str]) -> None:
        """Add a prepared step to the manifest and to what the later steps read."""
        manifest.append(item)
        after = {str(entry["path"]) for entry in item["effect_after"]}
        if step["kind"] == "move":
            chain.moves.append((str(item["path"]), str(item["postcondition"]["path"])))
        for path in after:
            chain.pages.pop(path, None)
        chain.vacated = (chain.vacated - after) | {
            str(entry["path"]) for entry in item["effect_after"] if entry.get("absent")
        }
        for path, text in exact.items():
            chain.pages[path] = (str(step["step_id"]), text_content_hash(text), text)
        if step["kind"] in _DUPLICATE_FOLD_KINDS:
            # Only the rows a step changes count as written: the canonical original a fold reads stays as sealed.
            chained.update(duplicate_fold.changed_paths(item))
            written.update(duplicate_fold.changed_paths(item))
        else:
            written.update(after)
        title_argument = _CREATED_TITLE_ARGUMENT.get(str(step["kind"]))
        title = step["args"].get(title_argument) if title_argument else None
        created.extend(
            (str(entry["path"]), title if isinstance(title, str) else None)
            for entry in item["effect_before"]
            if entry.get("absent") is True and entry["path"] in after
        )
        if step["kind"] == "supersede":
            from .vault import parse_frontmatter, resolve_display_title

            # The resolver indexes a page by the title read surfaces show, not only `title:`.
            fields, body, _raw = parse_frontmatter(str(item["preimage"] or ""))
            superseded.append(
                (str(item["path"]), resolve_display_title(fields, body, str(item["path"])))
            )

    done = list(preparation.steps) if preparation is not None else []
    for step, kept in zip(steps, done, strict=False):  # `done` holds a prefix of `steps`
        record(step, kept["item"], kept["exact"])
    for ordinal, step in enumerate(steps):
        if ordinal < len(done):
            continue
        if (
            preparation is not None
            and stop is not None
            and preparation.this_call
            and stop(preparation.slowest)
        ):
            raise PreparationPaused
        args = step["args"]
        retired = _planned_link_targets(vault_root, args, superseded)
        if retired:
            raise _error(
                "CURATION_STEP_CONFLICT",
                f"step {step['step_id']!r} links to {retired[0]!r}, which an earlier step "
                "supersedes; link its successor",
            )
        operation = args.get("operation")
        disposition = args.get("relation_disposition") or (
            operation.get("relation_disposition") if isinstance(operation, Mapping) else None
        )
        related = _planned_link_targets(vault_root, args, created)
        if related and disposition:
            raise _error(
                "CURATION_STEP_CONFLICT",
                f"step {step['step_id']!r} links to {related[0]!r}, which an earlier step "
                "creates; that link is its relation, so it takes no relation disposition",
            )
        started = time.monotonic()
        try:
            with find_module.planned_pages(vault_root, created):
                item = _prepare_step(
                    vault_root,
                    step,
                    ordinal,
                    relation_pending=bool(related),
                    projection=projection,
                    chain=chain,
                )
        except CurationError as error:
            # A derived plan drops the page whose step its leaf refused and prepares again.
            error.step_id = error.step_id or str(step["step_id"])
            raise
        exact: dict[str, str] = item.pop("exact")
        if step["kind"] in _CHAINED_STEP_KINDS:
            follows = {
                str(row["path"]): chain.pages[str(row["path"])][0]
                for row in item["effect_before"]
                if str(row["path"]) in chain.pages
                and row.get("content_hash") == chain.pages[str(row["path"])][1]
            }
            if follows:
                item["chained"] = follows
        duplicate = step["kind"] in _DUPLICATE_FOLD_KINDS
        expected_paths = {
            str(expected["path"]) for expected in _binding_expectations(item)
        } - set(item.get("chained") or ())
        conflicts = sorted((expected_paths - chained if duplicate else expected_paths) & written)
        if conflicts:
            raise _error(
                "CURATION_STEP_CONFLICT",
                f"step {step['step_id']!r} is bound to {conflicts[0]!r}, "
                "which an earlier step of the plan changes",
            )
        record(step, item, exact)
        if preparation is not None:
            preparation.steps.append({"digest": _step_digest(step), "item": item, "exact": exact})
            preparation.this_call += 1
            preparation.slowest = max(preparation.slowest, time.monotonic() - started)
    duplicate_fold.seal_chains(manifest, projection)
    if duplicate_fold.sealed_size(manifest) > duplicate_fold.MANIFEST_BUDGET:
        raise _error(
            "CURATION_PLAN_TOO_LARGE",
            "the undo of these folds would not fit in one compensation plan; fold fewer copies at once",
        )
    return manifest


# Fields of the closed step argument schema whose text never reaches the page, so a link
# there is neither a relation nor a rendered link.
_UNWRITTEN_ARGUMENTS = frozenset({"why", "reason", "relation_review_reason", "old_string"})


def _written_arguments(args: Mapping[str, Any]) -> dict[str, Any]:
    """The step's arguments without the schema fields whose text never reaches the page.

    Only the step schema's own levels are filtered: the step arguments, an edit's
    operation and its `edits`. Deeper keys come from the user's data.
    """

    def written(fields: Mapping[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in fields.items() if key not in _UNWRITTEN_ARGUMENTS}

    result = written(args)
    operation = result.get("operation")
    if isinstance(operation, Mapping):
        result["operation"] = operation = written(operation)
        edits = operation.get("edits")
        if isinstance(edits, list):
            operation["edits"] = [
                written(edit) if isinstance(edit, Mapping) else edit for edit in edits
            ]
    return result


def _planned_link_targets(
    vault_root: Path, args: Mapping[str, Any], pages: Sequence[tuple[str, str | None]]
) -> list[str]:
    """The `pages` that the wikilinks a step writes resolve to, by path or by title."""
    from .vault import (
        WikilinkError,
        WikilinkResolver,
        find_body_wikilinks,
        normalize_wikilink,
    )

    if not pages:
        return []
    resolver = WikilinkResolver.from_entries(vault_root, pages)
    found: set[str] = set()
    texts: list[Any] = [_written_arguments(args)]
    while texts:
        value = texts.pop()
        if isinstance(value, Mapping):
            texts.extend(value.values())
        elif isinstance(value, list):
            texts.extend(value)
        elif isinstance(value, str):
            for match in find_body_wikilinks(value):
                try:
                    target, _warning = normalize_wikilink(
                        match.group(0), vault_root, resolver=resolver, strict=True
                    )
                except WikilinkError:
                    continue
                found.add(target)
    return sorted(found)


def _validate_entity_candidate_plan(
    vault_root: Path, plan: Mapping[str, Any]
) -> None:
    binding = plan.get("entity_candidate")
    if not isinstance(binding, Mapping):
        return
    _item, current = _current_entity_candidate_binding(
        vault_root, str(binding["review_ref"])
    )
    if canonical_json(current) != canonical_json(binding):
        raise _error(
            "CURATION_ENTITY_CANDIDATE_STALE",
            "candidate, target, grammar, registry, or context batch changed",
        )
    state = str(binding["candidate_state"])
    steps = list(plan["steps"])
    kinds = [str(step["kind"]) for step in steps]
    if state == "ambiguous":
        raise _error(
            "CURATION_ENTITY_AMBIGUOUS",
            "an ambiguous recurring identity has no executable default target",
        )
    if state == "promotion":
        if kinds != ["create-entity"]:
            code = (
                "CURATION_ENTITY_DUPLICATE_CREATE"
                if kinds.count("create-entity") > 1
                else "CURATION_ENTITY_ROUTE_INVALID"
            )
            raise _error(
                code,
                "promotion requires exactly one governed create-entity step",
            )
        from .entity_candidates import identity_key

        if identity_key(steps[0]["args"].get("name")) != identity_key(
            binding["identity"]
        ):
            raise _error(
                "CURATION_ENTITY_ROUTE_INVALID",
                "promotion name must preserve the reviewed normalized identity",
            )
        return
    assert state == "hydration"
    if not 1 <= len(steps) <= 8 or any(
        kind not in {"edit", "accept-relation"} for kind in kinds
    ):
        raise _error(
            "CURATION_ENTITY_ROUTE_INVALID",
            "hydration permits one to eight guarded edit or accept-relation steps",
        )
    context_paths = {
        str(context["path"])
        for context in binding["first_disconnected_context_batch"]
    }
    target_paths = set(binding["target_refs"])
    for step in steps:
        if step["kind"] != "edit":
            continue
        path = str(step["args"].get("path") or "")
        if path not in context_paths | target_paths:
            raise _error(
                "CURATION_ENTITY_BATCH_STALE",
                "hydration edit is outside the reviewed context batch and target",
            )


@dataclass(frozen=True)
class PreparedProposal:
    """A validated plan with its bindings, not yet sealed into the run store."""

    plan: dict[str, Any]
    binding_manifest: list[dict[str, Any]]
    registry_ids: dict[str, str]
    #: The server action that derived the plan (`DERIVED_BY`), or None for an agent-written plan.
    derived_by: str | None = None
    #: The grouped summary a derived plan is approved by, sealed with it for every later preview.
    summary: dict[str, Any] | None = None


def propose(
    vault_root: Path, plan: Mapping[str, Any], *, allow_records: bool = False
) -> dict[str, Any]:
    """Seal a plan. `allow_records` admits only private episode leaves
    (`episode_workflow`); every public curation door leaves it off, so none can
    seal a Records plan."""
    return seal_proposal(
        vault_root,
        prepare_proposal(vault_root, plan, allow_records=allow_records),
        allow_records=allow_records,
    )


def seal_proposal(
    vault_root: Path, prepared: PreparedProposal, *, allow_records: bool = False
) -> dict[str, Any]:
    """Publish a prepared plan into the run store; it re-validates the plan.

    Preparation runs outside the vault mutation boundary (the dispatcher narrows
    a proposal to the writer fence), so the seal takes the boundary itself and
    refuses a plan whose bound pages or registries changed since preparation.
    Under an outer boundary (the episode workflow, the wide-boundary kill switch)
    it takes no second guard: that boundary already serializes the seal, and a
    nested guard would advance the commit generation a second time.
    """
    root = Path(vault_root)
    sealed = {
        **prepared.plan,
        "binding_manifest": prepared.binding_manifest,
        "registry_ids": prepared.registry_ids,
    }
    with _step_boundary(root, "curation_propose_commit"):
        blockers = _binding_blockers(root, sealed, plan_registry_identities(root, sealed))
        if blockers:
            raise _error(blockers[0]["code"], blockers[0]["reason"])
        created = CurationStore(root).create_forward(
            prepared.plan,
            binding_manifest=prepared.binding_manifest,
            registry_ids=prepared.registry_ids,
            allow_records=allow_records,
            derived_by=prepared.derived_by,
            summary=prepared.summary,
        )
    warnings = _plan_warnings(prepared.binding_manifest)
    return {**created, "warnings": warnings} if warnings else created


def _plan_warnings(binding_manifest: Iterable[Mapping[str, Any]]) -> list[str]:
    """What a plan's preparation could not check, shown beside the plan, not only in a binding.

    A repoint step whose catalogue lookup could not answer leaves prose mentions of the old
    paths unchecked (`repoint_links.prepare`).
    """
    from . import path_mentions

    unchecked = any(
        isinstance(item.get("prepared"), Mapping) and item["prepared"].get("mentions_unchecked")
        for item in binding_manifest
    )
    return [path_mentions.NOT_CHECKED] if unchecked else []


def prepare_proposal(
    vault_root: Path,
    plan: Mapping[str, Any],
    *,
    allow_records: bool = False,
    derived_by: str | None = None,
    preparation: Preparation | None = None,
    stop: Callable[[float], bool] | None = None,
) -> PreparedProposal:
    """Every check `propose` makes, without sealing anything. Read-only, so a
    caller preparing several plans can refuse them all before sealing one.
    `derived_by` names the server action that derived the plan (`DERIVED_BY`).

    A derived plan may prepare in parts: `preparation` keeps the steps that open
    this plan unchanged and gains each step prepared here, and `stop` may end the
    call between steps with `PreparationPaused` (`_prepare_manifest`)."""
    root = Path(vault_root)
    if derived_by is not None and derived_by not in DERIVED_BY:
        raise _error("INVALID_CURATION_PLAN", "plan derivation is invalid")
    if preparation is not None and derived_by is None:
        # Only the derived steps are known to keep all their preparation state in their items.
        raise _error("INVALID_CURATION_PLAN", "only a derived plan prepares in parts")
    validated = validate_forward_plan(
        plan, allow_records=allow_records, derived=derived_by is not None
    )
    # Captured before any step is prepared, so a registry change during preparation
    # differs from the seal-time recheck instead of being absorbed into the plan.
    registry_ids = plan_registry_identities(root, validated)
    # Candidate identity and state are re-read before any plan artifact is
    # published.  A stale review must not leave an inert but misleading plan in
    # the governed run store.
    _validate_entity_candidate_plan(root, validated)
    from . import vocabulary_gate

    if preparation is not None:
        preparation.keep_opening(validated["steps"], registry_ids)
    with vocabulary_gate.preparation_context(root):
        manifest = _prepare_manifest(root, validated["steps"], preparation=preparation, stop=stop)
    binding = validated.get("entity_candidate")
    if isinstance(binding, Mapping) and binding.get("candidate_state") == "hydration":
        allowed_paths = {
            str(context["path"])
            for context in binding["first_disconnected_context_batch"]
        } | set(binding["target_refs"])
        if any(str(item.get("path") or "") not in allowed_paths for item in manifest):
            raise _error(
                "CURATION_ENTITY_BATCH_STALE",
                "hydration leaf resolved outside the reviewed context batch and target",
            )
        target_stems = {
            str(target).replace("\\", "/").removesuffix(".md")
            for target in binding["target_refs"]
        }
        for step, prepared in zip(validated["steps"], manifest, strict=True):
            if step["kind"] != "accept-relation":
                continue
            candidate = dict(prepared.get("prepared", {}).get("candidate") or {})
            relation_target = str(candidate.get("to") or "").replace("\\", "/").removesuffix(".md")
            if relation_target not in target_stems:
                raise _error(
                    "CURATION_ENTITY_BATCH_STALE",
                    "hydration relation target is outside the reviewed Entity targets",
                )
    _require_plan_relocation_history(root, validated)
    return PreparedProposal(
        plan=validated, binding_manifest=manifest, registry_ids=registry_ids, derived_by=derived_by
    )


def _binding_blockers(
    vault_root: Path,
    plan: Mapping[str, Any],
    current_registries: Mapping[str, str],
    *,
    keep: Callable[[str], bool] | None = None,
) -> list[dict[str, Any]]:
    """Why a sealed plan's bindings no longer hold. `keep` is a caller's
    release filter: a hash-bound path it withholds is read as unreadable, so
    the list is exactly the one a deleted path gives. An expected absence is
    still decided on disk; a withheld file there is never absent."""
    blockers, store_owned = _records_store_blockers(vault_root, plan, keep)
    # A path a step binds at an earlier step's exact after hash is that step's to write first.
    pending_steps = {str(item["step_id"]) for item in plan["binding_manifest"]}
    for name, expected in plan["registry_ids"].items():
        if current_registries.get(name) != expected:
            blockers.append(
                {
                    "code": "CURATION_REGISTRY_CHANGED",
                    "registry": name,
                    "reason": "registry identity changed",
                }
            )
    for index, item in enumerate(plan["binding_manifest"]):
        if index in store_owned:
            # Its bound files are the store's generated views; the store blocker says why it cannot run.
            continue
        if item.get("kind") in FOLD_STEP_KINDS:
            from . import duplicate_fold, episode_fold

            # A fold may stand half done, so its files are judged by live state, not by one hash each.
            judge = (
                duplicate_fold.binding_blockers
                if item["kind"] in _DUPLICATE_FOLD_KINDS
                else episode_fold.binding_blockers
            )
            blockers.extend(judge(vault_root, item))
            continue
        follows = item.get("chained") or {}
        for expected_item in _binding_expectations(item):
            path = str(expected_item.get("path") or "")
            if follows.get(path) in pending_steps:
                continue
            if expected_item.get("absent") is True:
                if _guarded_absent(vault_root, path):
                    continue
                blockers.append(
                    {
                        "code": "CURATION_BINDING_STALE",
                        "path": path,
                        "reason": "expected absence changed",
                    }
                )
                continue
            expected = expected_item.get("content_hash")
            actual = _bound_hash(vault_root, path, keep)
            if actual != expected:
                blockers.append(
                    {
                        "code": "CURATION_BINDING_STALE",
                        "path": path,
                        "reason": "content hash changed",
                    }
                )
    return blockers


def _binding_expectations(item: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The paths a sealed step binds, each expected absent or at a content hash."""
    manifest = item.get("effect_before")
    if isinstance(manifest, list) and manifest:
        return [dict(expected) for expected in manifest]
    path = str(item["path"])
    if item.get("expected_absent"):
        destination = str(item.get("prepared", {}).get("destination") or path)
        return [{"path": destination, "absent": True}]
    if item.get("before_hash") is None:
        return []
    return [{"path": path, "content_hash": item["before_hash"]}]


def _records_store_blockers(
    vault_root: Path, plan: Mapping[str, Any], keep: Callable[[str], bool] | None
) -> tuple[list[dict[str, Any]], set[int]]:
    """The Records items whose collection the vault's marker now routes to the store.

    On the store thread the item is checked against the store's own guards (`resolve_guards`):
    a step sealed for the store, or one sealed before the migration that maps to it, holds;
    anything else is stale. Off that thread nothing can run it: it refuses with the code
    that leaves a run approved before the migration resumable once the store thread runs it.
    The marker and `keep` decide through the one rule the Records writer applies: a caller
    `keep` withholds the collection from finds no blocker here, and its bound pages read as
    missing below, as a deleted collection's do.
    """
    from . import structured_collections as collections
    from .collection_store import curation_step
    from .collection_store.preview import require_file_collection, selected_writer

    steps = {step["step_id"]: step for step in plan["steps"]}
    blockers: list[dict[str, Any]] = []
    owned: set[int] = set()
    for index, item in enumerate(plan["binding_manifest"]):
        prepared = item.get("prepared") or {}
        if item.get("kind") not in COLLECTION_STEP_KINDS or not isinstance(
            prepared.get("collection_id"), str
        ):
            continue
        manifest_path = prepared.get("manifest_path")
        writer = selected_writer(vault_root, prepared["collection_id"])
        if writer is not None:
            owned.add(index)
            try:
                curation_step.resolve_guards(
                    vault_root, writer, item, steps[item["step_id"]]["args"]
                )
            except collections.CollectionError as error:
                blockers.append(
                    {"code": error.code, "path": str(manifest_path or ""), "reason": error.reason}
                )
            continue
        try:
            require_file_collection(
                vault_root,
                prepared["collection_id"],
                authorize_path=keep or (lambda _path: True),
                notice_path=manifest_path if isinstance(manifest_path, str) else None,
            )
        except collections.CollectionError as error:
            if error.code == "COLLECTION_NOT_FOUND":
                continue
            refusal = (
                _records_store_unsupported(str(item["kind"]))
                if error.code == "COLLECTION_STORE_UNAVAILABLE"
                else _error(error.code, error.reason)
            )
            blockers.append(
                {"code": refusal.code, "path": str(manifest_path or ""), "reason": refusal.reason}
            )
            owned.add(index)
        else:
            if prepared.get("store") is not None:
                # Sealed against the store's guards, which a file collection does not have.
                blockers.append(
                    {
                        "code": "CURATION_BINDING_STALE",
                        "path": str(manifest_path or ""),
                        "reason": "the sealed collection is no longer in the store",
                    }
                )
                owned.add(index)
    return blockers, owned


def _bound_hash(vault_root: Path, path: str, keep: Callable[[str], bool] | None) -> str | None:
    """The live hash a binding compares, or None when the page is unreadable --
    gone, or withheld from the caller `keep` filters for. A withheld page is
    read like any other and the result discarded, so both take the same path."""
    try:
        _source, actual = _read_target(vault_root, path)
    except CurationError as error:
        if error.code == "CURATION_PATH_UNSAFE":
            raise
        return None
    if keep is not None and not keep(path):
        return None
    return actual


def preview(
    vault_root: Path, *, run_id: str, keep: Callable[[str], bool] | None = None
) -> dict[str, Any]:
    """A sealed plan's current health. `keep` (the episode `resume` passes the
    caller's release filter) reads a withheld bound page as a missing one."""
    root = Path(vault_root)
    store = CurationStore(root)
    plan = store.load_plan(run_id)
    identity, fingerprint = store.identities(run_id)
    blockers = _binding_blockers(root, plan, plan_registry_identities(root, plan), keep=keep)
    if (
        isinstance(plan.get("entity_candidate"), Mapping)
        and not _committed_step_ids(store.reconstruct(run_id))
    ):
        try:
            _validate_entity_candidate_plan(root, plan)
        except CurationError as error:
            blockers.insert(0, {"code": error.code, "reason": error.reason})
    if plan.get("derived_by") is not None:
        # A derived plan may hold hundreds of steps: its preview is the summary it was approved by.
        return {
            "action": "preview",
            "mutated": False,
            "run_id": run_id,
            "plan_id": identity,
            "plan_fingerprint": fingerprint,
            "title": plan["title"],
            # The groups the person approved, with the step counts the plan itself gives.
            "summary": {**derived_summary(plan), **(plan.get("approval_summary") or {})},
            "binding_health": "stale" if blockers else "current",
            # The same list shape as any preview, bounded; the count covers every blocker.
            "blockers": blockers[:SUMMARY_SAMPLE],
            "blocker_count": len(blockers),
        }
    return {
        "action": "preview",
        "mutated": False,
        "run_id": run_id,
        "plan_id": identity,
        "plan_fingerprint": fingerprint,
        "title": plan["title"],
        "actions": plan["steps"],
        "binding_manifest": plan["binding_manifest"],
        "binding_health": "stale" if blockers else "current",
        "blockers": blockers,
        **({"warnings": warnings} if (warnings := _plan_warnings(plan["binding_manifest"])) else {}),
        "compensation_classes": [
            compensation_kind(step["kind"], binding)
            for step, binding in zip(plan["steps"], plan["binding_manifest"], strict=True)
        ],
        **(
            {"entity_candidate": plan["entity_candidate"]}
            if isinstance(plan.get("entity_candidate"), Mapping)
            else {}
        ),
    }


def summary_group(rows: Sequence[Any]) -> dict[str, Any]:
    """One group of a derived plan's summary: every row counted, the first `SUMMARY_SAMPLE` shown."""
    return {
        "count": len(rows),
        "shown": list(rows[:SUMMARY_SAMPLE]),
        "truncated": len(rows) > SUMMARY_SAMPLE,
    }


def rewritten_referrers(step: Mapping[str, Any], binding: Mapping[str, Any]) -> int:
    """The pages whose links a link rewrite, or its inverse, changes; 0 for any other step."""
    if step["kind"] == REPOINT_STEP_KIND:
        # A repoint step also binds the pages it leaves as they are: a moved page, a named referrer.
        return sum(
            1 for row in binding["prepared"]["pages"] if row["after_hash"] != row["before_hash"]
        )
    if step["kind"] == UNREPOINT_STEP_KIND:
        return len(step["args"]["repoint"]["pages"])
    return 0


def derived_summary(plan: Mapping[str, Any]) -> dict[str, Any]:
    """A derived plan, or the undo of one, as the counts and samples a person approves."""
    moves: list[dict[str, str]] = []
    corrected: list[str] = []
    referrers = 0
    by_kind: dict[str, int] = {}
    for step, binding in zip(plan["steps"], plan["binding_manifest"], strict=True):
        by_kind[step["kind"]] = by_kind.get(step["kind"], 0) + 1
        if step["kind"] in {REPOINT_STEP_KIND, UNREPOINT_STEP_KIND}:
            referrers += rewritten_referrers(step, binding)
            continue
        before, after = str(binding["path"]), str(binding["postcondition"]["path"])
        if before != after:
            moves.append({"from": before, "to": after})
        else:
            corrected.append(before)
    return {
        "steps": len(plan["steps"]),
        "by_kind": by_kind,
        "moves": summary_group(moves),
        "corrected_in_place": summary_group(corrected),
        "referrers_rewritten": referrers,
    }


def compensation_kind(kind: str, binding: Mapping[str, Any] | None = None) -> str:
    """The step kind that reverses `kind`; `binding` tells a pointer correction from another edit."""
    if kind == "edit" and (binding or {}).get("prepared", {}).get("pointer") is True:
        return REVERT_POINTER_STEP_KIND
    return {
        "create-note": "delete",
        "create-entity": "delete",
        "accept-relation": "supersede",
        "edit": "supersede",
        "supersede": "supersede",
        "move": UNMOVE_STEP_KIND,
        REPOINT_STEP_KIND: UNREPOINT_STEP_KIND,
        # An undone relocation is redone by a new reviewed plan, never by undoing the undo.
        **dict.fromkeys(_RELOCATION_INVERSE_KINDS, "unavailable"),
        "delete": "recover",
        "recover": "delete",
        FOLD_RECAP_STEP_KIND: UNFOLD_RECAP_STEP_KIND,
        # An undone fold is folded again by a new reviewed plan, never by undoing the undo.
        UNFOLD_RECAP_STEP_KIND: "unavailable",
        FOLD_DUPLICATE_STEP_KIND: UNFOLD_DUPLICATE_STEP_KIND,
        UNFOLD_DUPLICATE_STEP_KIND: "unavailable",
        # Records history is corrected by a later Records transition, never
        # by removing the one that happened.
        RECORDS_STEP_KIND: "unavailable",
        "update-record": "unavailable",
        OBSERVE_UNIT_STEP_KIND: "unavailable",
        # Authoring the edge again is a new write and a new promotion, never a rollback.
        REMOVE_RELATION_STEP_KIND: "unavailable",
        **dict.fromkeys(PRESERVATION_STEP_KINDS | PLANNING_STEP_KINDS, "unavailable"),
    }[kind]


class CurationFault(RuntimeError):
    """Test-only deterministic process-cut signal."""


def _fault_barrier(_name: str) -> None:
    return None


def _witness_basis(
    *,
    run_identity: str,
    plan_identity: str,
    ordinal: int,
    step: Mapping[str, Any],
    operation_identity: str,
    binding: Mapping[str, Any],
    parent_compensation_plan_id: str | None = None,
) -> dict[str, Any]:
    sealed_before = binding.get("effect_before")
    before: list[dict[str, Any]] = (
        [dict(item) for item in sealed_before]
        if isinstance(sealed_before, list)
        else []
    )
    if not before and binding.get("before_hash") is not None:
        before.append({"path": binding["path"], "content_hash": binding["before_hash"]})
    postcondition = dict(binding.get("postcondition") or {})
    after_path = str(postcondition.get("path") or binding["path"])
    after_hash = postcondition.get("content_hash")
    sealed_after = binding.get("effect_after")
    exact_after = bool(
        isinstance(sealed_after, list)
        and sealed_after
        and not (step["kind"] == "delete" and len(sealed_after) == 1)
        and all(
            isinstance(item, Mapping)
            and (
                (set(item) == {"path", "absent"} and item.get("absent") is True)
                or (
                    set(item) == {"path", "content_hash"}
                    and _HEX64.fullmatch(str(item.get("content_hash") or "")) is not None
                )
            )
            for item in sealed_after
        )
    )
    if exact_after:
        after = [dict(item) for item in sealed_after]
    elif step["kind"] == "delete":
        after = [{"path": binding["path"], "absent": True}]
    elif after_hash is not None:
        after = [{"path": after_path, "content_hash": after_hash}]
    else:
        after = [{"path": after_path, "exists": True}]
    leaf_identity = {
        "create-note": "remember",
        "create-entity": "connect_memory:create-entity",
        "accept-relation": "connect_memory:accept-relation",
        "edit": "edit_memory",
        "supersede": "replace_memory",
        "move": "manage_memory_file:move",
        REPOINT_STEP_KIND: "manage_memory_file:repoint-links",
        UNMOVE_STEP_KIND: "manage_memory_file:unmove",
        UNREPOINT_STEP_KIND: "manage_memory_file:unrepoint-links",
        REVERT_POINTER_STEP_KIND: "edit_memory:revert-pointer",
        "delete": "manage_memory_file:delete",
        "recover": "manage_memory_file:recover",
        FOLD_RECAP_STEP_KIND: "maintain_memory:fold-recap-revisions",
        UNFOLD_RECAP_STEP_KIND: "maintain_memory:unfold-recap-revisions",
        FOLD_DUPLICATE_STEP_KIND: "maintain_memory:fold-duplicate",
        UNFOLD_DUPLICATE_STEP_KIND: "maintain_memory:unfold-duplicate",
        RECORDS_STEP_KIND: "record_memory:append",
        "update-record": "record_memory:update",
        OBSERVE_UNIT_STEP_KIND: "observe_memory:update",
        REMOVE_RELATION_STEP_KIND: "edit_memory:remove-relation",
        "capture-source": "capture_memory:source", "preserve-evidence": "preserve_memory:evidence",
        **{kind: f"{PLANNING_PROFILE.tool}:{action}" for kind, action in PLANNING_STEP_ACTIONS.items()},
    }[step["kind"]]
    return {
        "version": 1,
        "run_id": run_identity,
        "plan_id": plan_identity,
        "ordinal": ordinal,
        "step_id": step["step_id"],
        "operation_id": operation_identity,
        "before": before,
        "after": after,
        "leaf_identity": leaf_identity,
        "parent_compensation_plan_id": parent_compensation_plan_id,
    }


def _witness_payload(**kwargs: Any) -> dict[str, Any]:
    basis = _witness_basis(**kwargs)
    return {
        **basis,
        "result_digest": _digest(basis),
        "committed_at": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
    }


def _finalize_witness_batch(
    vault_root: Path,
    step: Mapping[str, Any],
    binding: Mapping[str, Any],
    writes: list[Any],
    content: str,
) -> str | None:
    """Bind exact authored postimages from the leaf's own planned batch."""
    from . import curation_witness

    root = Path(vault_root).absolute()
    contents: dict[str, str] = {}
    for write in writes:
        if not isinstance(getattr(write, "content", None), str):
            continue
        try:
            relative = Path(write.path).absolute().relative_to(root).as_posix()
        except ValueError:
            continue
        contents[relative] = write.content
    hashes = {path: text_content_hash(content) for path, content in contents.items()}
    kind = str(step["kind"])
    if kind in PRESERVATION_STEP_KINDS:
        from .vault import PreparedBinaryContent

        planned = {}
        for write in writes:
            path = Path(write.path).absolute().relative_to(root).as_posix()
            if isinstance(write.content, PreparedBinaryContent):
                planned[path] = {"path": path, "content_hash": write.content.sha256, "size": write.content.size}
            elif isinstance(write.content, str):
                planned[path] = {"path": path, "content_hash": hashes[path]}
        core = binding["prepared"]["core"]
        pending = curation_witness.pending_for(vault_root)
        if pending is None or pending.native_core is None or set(pending.native_core) != {item["path"] for item in core}:
            raise _error("CURATION_BINDING_STALE", "native core allocation paths changed")
        if any(planned.get(item["path"]) != item for item in core):
            raise _error("CURATION_BINDING_STALE", "native core changed after preparation")
        return curation_witness.render_with_after(content, binding["effect_after"])
    kind = str(step["kind"])
    if kind in FOLD_STEP_KINDS:
        # The fold's own files are moved by held writers, never by this batch: its witness is
        # committed alone and last, once `pending.ready` says the whole effect stands.
        pending = curation_witness.pending_for(vault_root)
        if pending is None or not pending.ready:
            return None
        return curation_witness.render_with_after(content, binding["effect_after"])
    source = str(binding["path"])
    destination = str(binding.get("postcondition", {}).get("path") or source)
    prior_hash = binding.get("before_hash")
    if kind == "delete":
        return None
    if kind in _RENAME_STEP_KINDS or binding.get("prepared", {}).get("pointer") is True:
        # The batch that lands the page's sealed bytes carries the witness: for a move, the batch
        # after its rename (or the adopting batch); for a pointer correction, its one write.
        if destination not in hashes:
            return None
        if hashes[destination] != binding["postcondition"]["content_hash"]:
            raise _error("CURATION_BINDING_STALE", f"{destination!r} no longer lands its sealed bytes")
        return curation_witness.render_with_after(content, binding["effect_after"])
    if kind in _REWRITE_STEP_KINDS:
        # Every page the step changes lands in its one batch; a step that changes none commits
        # its witness alone once it is ready (`repoint_links._commit`).
        before = {str(row["path"]): row.get("content_hash") for row in binding["effect_before"]}
        changed = [
            row for row in binding["effect_after"] if row["content_hash"] != before.get(row["path"])
        ]
        if any(row["path"] in hashes and hashes[row["path"]] != row["content_hash"] for row in changed):
            raise _error("CURATION_BINDING_STALE", "a page no longer lands its sealed bytes")
        if not all(row["path"] in hashes for row in changed):
            return None
        if not changed:
            pending = curation_witness.pending_for(vault_root)
            if pending is None or not pending.ready:
                return None
        return curation_witness.render_with_after(content, binding["effect_after"])
    if kind == "recover":
        pending = curation_witness.pending_for(vault_root)
        if pending is None or not pending.ready:
            return None
        after = [
            {"path": source, "absent": True},
            {"path": destination, "content_hash": hashes.get(destination) or prior_hash},
        ]
    elif kind == "supersede":
        if source not in hashes or destination not in hashes:
            return None
        after = [
            {"path": path, "content_hash": hashes[path]}
            for path in (source, destination)
            if path in hashes
        ]
    else:
        exact_hash = hashes.get(destination)
        if exact_hash is None:
            return None
        if kind == "edit":
            _require_reviewed_edit(binding, contents[destination])
        after = [{"path": destination, "content_hash": exact_hash}]
    return curation_witness.render_with_after(content, after)


def _require_reviewed_edit(binding: Mapping[str, Any], planned: str) -> None:
    """Refuse, before any byte lands, an edit that differs from the approved one.

    Only the `updated:` instant may differ: `_dispatch_step` mints the transition at execution.
    """
    from . import edit as edit_module
    from . import semantic_writes

    reviewed = semantic_writes.transition_token_stamp(binding["prepared"].get("transition_token"))
    rendered = edit_module.reviewed_render(planned, reviewed) if reviewed is not None else planned
    if text_content_hash(rendered) != binding["postcondition"]["content_hash"]:
        raise _error("CURATION_BINDING_STALE", "the edit no longer writes the approved content")


def _validate_witness(
    value: Any,
    *,
    run_identity: str,
    plan_identity: str,
    ordinal: int,
    step: Mapping[str, Any],
    operation_identity: str,
    binding: Mapping[str, Any],
    parent_compensation_plan_id: str | None = None,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _error("CURATION_OUTCOME_UNCERTAIN", "curation witness is not an object")
    allowed = {
        "version",
        "run_id",
        "plan_id",
        "ordinal",
        "step_id",
        "operation_id",
        "before",
        "after",
        "leaf_identity",
        "parent_compensation_plan_id",
        "result_digest",
        "committed_at",
    }
    if set(value) != allowed:
        raise _error("CURATION_OUTCOME_UNCERTAIN", "curation witness schema is invalid")
    expected = _witness_basis(
        run_identity=run_identity,
        plan_identity=plan_identity,
        ordinal=ordinal,
        step=step,
        operation_identity=operation_identity,
        binding=binding,
        parent_compensation_plan_id=parent_compensation_plan_id,
    )
    actual_basis = {key: value[key] for key in expected}
    expected_without_after = {key: item for key, item in expected.items() if key != "after"}
    actual_without_after = {key: item for key, item in actual_basis.items() if key != "after"}
    after = actual_basis.get("after")
    if (
        actual_without_after != expected_without_after
        or not isinstance(after, list)
        or not after
        or value.get("result_digest") != _digest(actual_basis)
    ):
        raise _error("CURATION_OUTCOME_UNCERTAIN", "curation witness identity is invalid")
    kind = str(step["kind"])
    source = str(binding["path"])
    destination = str(binding.get("postcondition", {}).get("path") or source)
    normalized: list[tuple[str, str]] = []
    for item in after:
        if not isinstance(item, Mapping):
            raise _error("CURATION_OUTCOME_UNCERTAIN", "curation postimage is invalid")
        keys = set(item)
        if (
            keys == {"path", "content_hash"}
            and isinstance(item.get("content_hash"), str)
            and _HEX64.fullmatch(str(item["content_hash"]))
        ):
            shape = "content"
        elif keys == {"path", "absent"} and item.get("absent") is True:
            shape = "absent"
        else:
            raise _error("CURATION_OUTCOME_UNCERTAIN", "curation postimage is invalid")
        try:
            path = normalize_target_path(
                item.get("path"),
                field="witness.after.path",
                allow_trash=kind in {"delete", "recover"},
                allow_fold=kind in FOLD_STEP_KINDS,
                allow_records=kind in RECORDS_STEP_KINDS,
                allow_append_only=kind in _REWRITE_STEP_KINDS
                or binding.get("prepared", {}).get("pointer") is True,
                _planning_tree=kind in PLANNING_STEP_KINDS,
                _planning_identity=(binding["prepared"]["collection_id"], binding["prepared"]["item_key"]) if kind in PLANNING_STEP_KINDS and binding["prepared"].get("store") is not None else None,
                _native_paths=frozenset(item["path"] for item in binding["prepared"]["core"]) if kind in PRESERVATION_STEP_KINDS else None,
            )
        except CurationError as error:
            raise _error("CURATION_OUTCOME_UNCERTAIN", "curation postimage is invalid") from error
        normalized.append((path, shape))
    if len({path for path, _shape in normalized}) != len(normalized):
        raise _error("CURATION_OUTCOME_UNCERTAIN", "curation postimage is duplicated")
    sealed_after = binding.get("effect_after")
    if (
        isinstance(sealed_after, list)
        and sealed_after
        and not (kind == "delete" and len(sealed_after) == 1)
        and all(
        isinstance(item, Mapping)
        and (
            set(item) == {"path", "content_hash"}
            or (set(item) == {"path", "absent"} and item.get("absent") is True)
        )
        for item in sealed_after
        )
    ):
        required = {
            (str(item["path"]), "absent" if item.get("absent") is True else "content")
            for item in sealed_after
        }
    elif kind in {"move", "recover"}:
        required = {(source, "absent"), (destination, "content")}
    elif kind == "supersede":
        required = {(source, "content"), (destination, "content")}
    elif kind == "delete":
        trash = [
            path
            for path, shape in normalized
            if path != source
            and shape == "content"
            and _is_governed_trash(path)
        ]
        required = {(source, "absent"), *((path, "content") for path in trash)}
        if len(trash) != 1:
            raise _error("CURATION_OUTCOME_UNCERTAIN", "delete witness is invalid")
    else:
        required = {(destination, "content")}
    if set(normalized) != required or len(normalized) != len(required):
        raise _error("CURATION_OUTCOME_UNCERTAIN", "curation postimage scope is invalid")
    return dict(value)


def records_collections(vault_root: Path, run_id: str) -> set[str]:
    """The collections whose Records steps a sealed plan holds; none when it cannot be read."""
    try:
        plan = CurationStore(Path(vault_root)).load_plan(run_id)
    except (CurationError, OSError):
        return set()
    return {
        item["prepared"]["collection_id"]
        for item in plan["binding_manifest"]
        if item.get("kind") in COLLECTION_STEP_KINDS
        and isinstance((item.get("prepared") or {}).get("collection_id"), str)
    }


def _publish_store_witness(
    store: CurationStore,
    run_identity: str,
    *,
    writer: Any,
    plan_identity: str,
    ordinal: int,
    step: Mapping[str, Any],
    binding: Mapping[str, Any],
    operation_identity: str,
) -> None:
    """Write the evidence file of a store step from its transaction row, once.

    The row is the witness; the file is its derivation, so every attempt that finds the row
    writes the same bytes and a crash between the store's commit and this write loses nothing.
    """
    from . import curation_witness
    from .collection_store import curation_step

    path = _evidence_path(store, run_identity, operation_identity)
    if path.exists():
        return
    found = curation_step.committed(writer, store_request_id(run_identity, operation_identity))
    if found is None:
        return
    receipt = found["receipt"]
    basis = _witness_basis(
        run_identity=run_identity,
        plan_identity=plan_identity,
        ordinal=ordinal,
        step=step,
        operation_identity=operation_identity,
        binding=binding,
    )
    after = [
        {"path": str(binding["postcondition"]["path"]), "content_hash": receipt.get("after_item_hash")}
    ]
    witness = json.loads(
        curation_witness.render_with_after(
            canonical_json({**basis, "committed_at": found["committed_at"]}),
            after,
        )
    )
    store.create_evidence(run_identity, operation_identity, witness)


def _committed_operation_witness(
    store: CurationStore,
    run_identity: str,
    *,
    writer: Any,
    plan_identity: str,
    ordinal: int,
    step: Mapping[str, Any],
    binding: Mapping[str, Any],
    operation_identity: str,
    compensation: bool,
) -> dict[str, Any] | None:
    """The validated witness of a step that committed, or None when it did not.

    A store step that committed leaves its row before its evidence file, so the file is derived
    first when the row is there. A native step's witness is checked as history alone.
    """
    if writer is not None:
        _publish_store_witness(
            store,
            run_identity,
            writer=writer,
            plan_identity=plan_identity,
            ordinal=ordinal,
            step=step,
            binding=binding,
            operation_identity=operation_identity,
        )
    return _validated_operation_witness(
        store,
        run_identity,
        plan_identity=plan_identity,
        ordinal=ordinal,
        step=step,
        binding=binding,
        operation_identity=operation_identity,
        compensation=compensation,
        live=step["kind"] not in _NATIVE_STEP_KINDS,
    )


def _records_item_visible(
    vault_root: Path, binding: Mapping[str, Any], path: str
) -> bool | None:
    """Canonical visibility of a sealed Records item; None only for a file-backed leaf."""
    from . import structured_collections as collections
    from .collection_store import ownership
    from .collection_store.preview import selected_writer

    if not isinstance(binding, Mapping):
        return False
    if binding.get("kind") not in COLLECTION_STEP_KINDS:
        return None
    prepared = binding.get("prepared")
    if not isinstance(prepared, Mapping):
        return False
    cid, key = prepared.get("collection_id"), prepared.get("item_key")
    if not isinstance(cid, str) or not isinstance(key, str) or prepared.get("destination") != path:
        return False
    writer = selected_writer(vault_root, cid)
    if writer is None:
        proof = ownership.current(vault_root)
        return False if proof.selected(vault_root, cid) is not None or proof.owner(vault_root, path) is not None else None
    try:
        return writer.admitted_item_metadata(cid, key, path) is not None
    except collections.CollectionError:
        return False


def _verify_records_receipt(
    vault_root: Path,
    binding: Mapping[str, Any],
    witness: Mapping[str, Any],
    *,
    kind: str = RECORDS_STEP_KIND,
) -> bool:
    """A Records leaf's witness holds only while the Records receipt agrees.

    The owner verifies the append payload or update preimages against one
    audit transition. Its item identity and hash must match the sealed
    destination and the witness's own postimage. Returns True when the store's
    transaction row was that receipt, so the item's file is only a generated view.
    """
    from . import records as records_module
    from .collection_store.preview import selected_writer

    prepared = dict(binding.get("prepared") or {})
    after = list(witness["after"])
    if len(after) != 1:
        raise _error("CURATION_OUTCOME_UNCERTAIN", "the Records postimage is invalid")
    # The evidence this reads may be damaged: a binding without a collection identity is no store step.
    collection_id = prepared.get("collection_id")
    writer = selected_writer(vault_root, collection_id) if isinstance(collection_id, str) else None
    if kind in PLANNING_STEP_KINDS:
        from . import planning

        action = PLANNING_STEP_ACTIONS[kind]
        if writer is not None:
            if not planning.store_receipt(writer, store_request_id(str(witness["run_id"]), str(witness["operation_id"])), prepared=prepared, after=after[0], action=action):
                raise _error("CURATION_OUTCOME_UNCERTAIN", "Planning transaction does not corroborate")
            return True
        if action == "add":
            receipt = planning.add_receipt(vault_root, prepared["manifest_path"], item_key=prepared["item_key"], payload_hash=prepared["payload_hash"])
        else:
            receipt = planning.update_receipt(vault_root, prepared["manifest_path"], operation=action, item_key=prepared["item_key"], before_manifest_hash=prepared["before_manifest_hash"], before_container_hash=prepared["before_container_hash"], before_item_hash=prepared["before_item_hash"], after_item_hash=after[0]["content_hash"])
        if receipt is None or receipt["canonical_path"] != prepared["destination"] or receipt["after_item_hash"] != after[0]["content_hash"]:
            raise _error("CURATION_OUTCOME_UNCERTAIN", "Planning audit does not corroborate")
        return False
    if writer is not None:
        # On the store thread the transaction row for the step's request id is the receipt.
        from .collection_store import curation_step

        # A file witness predates the store's request rows. Its imported byte binding
        # proves that historical postimage without treating an operation id as a commit.
        if curation_step.verify_imported(writer, prepared, after[0], kind=kind):
            return True
        if not curation_step.verify(
            writer,
            store_request_id(str(witness["run_id"]), str(witness["operation_id"])),
            collection_id=collection_id,
            item_key=str(prepared.get("item_key")),
            path=str(after[0].get("path")),
            after_item_hash=str(after[0].get("content_hash")),
        ):
            raise _error("CURATION_OUTCOME_UNCERTAIN", "the store's transaction does not corroborate")
        return True
    if kind == "update-record":
        receipt = records_module.update_receipt(
            vault_root,
            str(prepared.get("manifest_path") or ""),
            item_key=str(prepared.get("item_key") or ""),
            before_manifest_hash=str(prepared.get("before_manifest_hash") or ""),
            before_container_hash=str(prepared.get("before_container_hash") or ""),
            before_item_hash=str(prepared.get("before_item_hash") or ""),
            after_item_hash=str(after[0].get("content_hash") or ""),
        )
    else:
        receipt = records_module.append_receipt(
            vault_root,
            str(prepared.get("manifest_path") or ""),
            item_key=str(prepared.get("item_key") or ""),
            payload_hash=str(prepared.get("payload_hash") or ""),
        )
    if (
        receipt is None
        or receipt["collection_id"] != prepared.get("collection_id")
        or receipt["canonical_path"] != prepared.get("destination")
        or [receipt["canonical_path"], receipt["after_item_hash"]]
        != [after[0].get("path"), after[0].get("content_hash")]
    ):
        raise _error("CURATION_OUTCOME_UNCERTAIN", "the Records receipt does not corroborate")
    return False


def _chained_states(step: Mapping[str, Any], binding: Mapping[str, Any]) -> dict[str, frozenset[str]] | None:
    """The hashes each page of a duplicate fold may hold while its plan runs, or None for any other step."""
    if step.get("kind") not in _DUPLICATE_FOLD_KINDS:
        return None
    from . import duplicate_fold

    return duplicate_fold.live_states(binding)


def _verify_live_postcondition(
    vault_root: Path,
    witness: Mapping[str, Any],
    *,
    step: Mapping[str, Any] | None = None,
    binding: Mapping[str, Any] | None = None,
    accepted: Mapping[str, frozenset[str]] | None = None,
    drift: str | None = "CURATION_OUTCOME_UNCERTAIN",
) -> None:
    """Refuse unless each page the witness names holds its sealed postcondition.

    `drift` is the code for a readable page that differs. A step's own recovery cannot tell
    whose bytes those are, so it reports uncertain. Compensation reads a witness that matched
    when its step committed, so a difference there is later work and it passes the stale code.
    None accepts the difference as later work: only a call that saw its own leaf's batch commit
    passes it (`_execute_step`). An unsafe or unreadable path and a Records receipt that does not
    corroborate still refuse.
    """
    if step is not None and step.get("kind") in PRESERVATION_STEP_KINDS:
        from dataclasses import replace

        from . import artifact_recovery, originals, retained_inputs
        from .governance.principal import effective_principal

        if binding is None:
            raise _error("CURATION_OUTCOME_UNCERTAIN", "native preservation needs its original binding")
        native = binding["prepared"]
        binaries = {item["path"]: item for item in native["core"] if "size" in item}
        verified = set()
        try:
            who = replace(effective_principal(), administrative_ingress=False)
            for path, reference in native["references"].items():
                page = retained_inputs._read_snapshot(vault_root, reference)  # noqa: SLF001
                if page.page.path != path:
                    raise _error("CURATION_OUTCOME_UNCERTAIN", "native preservation identity changed")
                original = originals.binary_binding(page.page.frontmatter)
                if original is None or original.path not in binaries:
                    continue
                expected = binaries[original.path]
                with artifact_recovery.stage(vault_root, path, who, expected_sha256=expected["content_hash"]) as (held, _descriptor):
                    if held.size != expected["size"]:
                        raise artifact_recovery.RecoveryUnavailable
                    artifact_recovery.require_current(vault_root, held, who)
                verified.add(original.path)
            if verified != set(binaries):
                raise _error("CURATION_OUTCOME_UNCERTAIN", "native preserved originals are incomplete")
        except (retained_inputs.RetainedInputError, artifact_recovery.RecoveryUnavailable) as error:
            raise _error("CURATION_OUTCOME_UNCERTAIN", "native preserved original is unavailable") from error
        return
    if step is not None and step.get("kind") in COLLECTION_STEP_KINDS:
        if binding is None:
            raise _error("CURATION_OUTCOME_UNCERTAIN", "a Records witness needs its binding")
        if _verify_records_receipt(vault_root, binding, witness, kind=step["kind"]):
            # The postimage is the store's item version; the item's file is a generated view.
            return
    for item in witness["after"]:
        relative = str(item["path"])
        if item.get("absent") is True:
            try:
                absent = _guarded_absent(vault_root, relative)
            except CurationError as error:
                raise _error(
                    "CURATION_OUTCOME_UNCERTAIN", "witness target path is unsafe"
                ) from error
            if not absent and drift is not None:
                raise _error(drift, f"{relative!r} exists where the step left it absent")
            continue
        expected = item.get("content_hash")
        if expected is not None:
            try:
                actual = _guarded_digest(vault_root, relative, stale=True)
            except CurationError as error:
                if error.code == "CURATION_BINDING_STALE":
                    if drift is None:
                        continue
                    raise _error(drift, f"{relative!r} is gone from where the step left it") from error
                raise _error(
                    "CURATION_OUTCOME_UNCERTAIN", "witness target is unreadable"
                ) from error
            # A page that a later step of the same plan rewrote again holds one of its chained hashes.
            if drift is not None and actual != expected and actual not in (accepted or {}).get(relative, ()):
                raise _error(drift, f"{relative!r} changed after the step wrote it")


def _retryable_failure(error: Exception) -> bool:
    """Whether a step that wrote nothing may run again on `resume`.

    An I/O fault may clear. So may the server's own "not now", such as a busy vault or a
    warm-up that has not finished: that refusal declares itself retryable and uncommitted, and
    the step's committed-witness check has already ruled out a write.
    """
    from .cli_ops import OpError

    if isinstance(error, OpError):
        return error.details.get("status") == "retryable" and not error.details.get("committed")
    return isinstance(error, OSError)


def _dispatch_step(
    vault_root: Path,
    step: Mapping[str, Any],
    binding: Mapping[str, Any],
    runtime_args: Mapping[str, Any] | None = None,
    run: Mapping[str, str] | None = None,
    before_rename: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Run the step's leaf. `before_rename` records a move's intent, once, before its rename."""
    from . import commands

    kind = str(step["kind"])
    args = json.loads(canonical_json(step["args"]))
    prepared = dict(binding.get("prepared") or {})
    if kind in FOLD_STEP_KINDS:
        from . import duplicate_fold, episode_fold

        return {
            FOLD_RECAP_STEP_KIND: episode_fold.fold,
            UNFOLD_RECAP_STEP_KIND: episode_fold.unfold,
            FOLD_DUPLICATE_STEP_KIND: duplicate_fold.fold,
            UNFOLD_DUPLICATE_STEP_KIND: duplicate_fold.unfold,
        }[kind](vault_root, binding)
    if kind in PRESERVATION_STEP_KINDS:
        return _preservation_call(vault_root, kind, args, prepared=prepared)
    if kind == "create-note":
        relation = {
            key: prepared[key]
            for key in (
                "relation_disposition",
                "relation_review_hash",
                "relation_review_reason",
            )
            if prepared.get(key) is not None
        }
        for key in ("relation_disposition", "relation_review_reason"):
            args.pop(key, None)
        return commands.op_remember(
            vault_root,
            **args,
            draft_id=prepared["draft_id"],
            draft_hash=prepared["draft_hash"],
            draft_token=prepared["draft_token"],
            **relation,
        )
    if kind == "create-entity":
        return commands.op_connect_memory(vault_root, operation="create-entity", **args)
    if kind == "accept-relation":
        return commands.op_connect_memory(vault_root, operation="accept-relation", **args)
    if kind == "edit" and prepared.get("pointer") is True:
        from . import set_frontmatter_field as frontmatter

        # A pointer correction sets no `updated:`, so the sealed bytes land as approved; its record
        # is filed under the step's operation id, where its compensation finds it.
        assert run is not None
        try:
            result = frontmatter.set_pointer_field(
                vault_root,
                path=args["path"],
                field=args["operation"]["field"],
                value=args["operation"].get("value"),
                why=args["why"],
                expected_hash=binding["before_hash"],
                operation=run["operation"],
            )
        except frontmatter.SetFrontmatterError as error:
            raise _error(error.code, error.reason) from error
        return {**result.as_dict(), "path": args["path"]}
    if kind == "edit":
        # The sealed transition pins the reviewed `updated:` instant, which the writer refuses once
        # it is a day old, and approval often comes later. The transition is minted now, never from
        # a token copied into the plan; `_require_reviewed_edit` proves the approved bytes land.
        operation = {
            key: value
            for key, value in args["operation"].items()
            if key not in {"validate_only", "transition_token", "relation_review_hash"}
        }
        validation = commands.op_edit_memory(
            vault_root,
            path=args["path"],
            why=args["why"],
            operation={**operation, "validate_only": True},
        )
        semantic = dict(validation.get("semantic") or {})
        operation["transition_token"] = semantic.get("transition_token")
        operation["relation_review_hash"] = semantic.get("relation_review_hash")
        return commands.op_edit_memory(
            vault_root, path=args["path"], why=args["why"], operation=operation
        )
    if kind == REMOVE_RELATION_STEP_KIND:
        from . import promotion_revert

        return promotion_revert.remove_relation(vault_root, **args)
    if kind == OBSERVE_UNIT_STEP_KIND:
        return commands.op_observe_memory(
            vault_root, path=args["path"], operation="update", reason=args["why"],
            transition_token=prepared["transition_token"], **args["observation"],
        )
    if kind == "supersede":
        relation = {
            key: prepared[key]
            for key in (
                "relation_disposition",
                "relation_review_hash",
                "relation_review_reason",
            )
            if prepared.get(key) is not None
        }
        for key in ("relation_disposition", "relation_review_reason"):
            args.pop(key, None)
        return commands.op_replace_memory(
            vault_root,
            **args,
            draft_id=prepared["draft_id"],
            draft_hash=prepared["draft_hash"],
            draft_token=prepared["draft_token"],
            **relation,
        )
    if kind in PLANNING_STEP_KINDS:
        from . import plan_memory
        from .collection_store.preview import selected_writer

        action = PLANNING_STEP_ACTIONS[kind]
        args = dict(prepared["normalized_native_args"])
        intent = prepared.get("source_intent")
        def validate_intent() -> None:
            from .governance import policy
            from .origin_bindings import resolve_origin_input

            # Exact identity and release are native checks; the agent judges expressed intent.
            with policy.outside_request_policy_snapshot():
                resolve_origin_input(vault_root, intent)

        writer = selected_writer(vault_root, prepared["collection_id"])
        request = None
        if writer is not None:
            from .collection_store import curation_step

            container, version = curation_step.resolve_guards(vault_root, writer, binding, args)
            args["expected_container_hash"] = container
            if action != "add":
                args["expected_item_version"] = version
            request = store_request_id(run["curation_run"], run["operation"])
        result = plan_memory._write(vault_root, action, args, request_id=request, history_binding=run, _validate_inputs=validate_intent if intent is not None else None)  # noqa: SLF001
        return {**result, "path": prepared["destination"]}
    if kind in RECORDS_STEP_KINDS:
        from . import record_governance
        from . import records as records_module
        from .collection_store import curation_step
        from .collection_store.preview import selected_writer

        writer = selected_writer(vault_root, prepared["collection_id"])
        if writer is not None:
            assert run is not None
            # The store's transaction row for this request id is the step's witness.
            result = curation_step.commit(
                vault_root,
                writer,
                "append" if kind == RECORDS_STEP_KIND else "update",
                args,
                binding,
                store_request_id(run["curation_run"], run["operation"]),
            )
            return {**result, "path": prepared["destination"]}
        manifest = record_governance.require_records_profile(
            record_governance.resolve_collection_for_mutation(vault_root, args["collection"])
        )
        if (manifest.collection_id, manifest.path) != (
            prepared["collection_id"],
            prepared["manifest_path"],
        ):
            raise _error("CURATION_BINDING_STALE", "the Records collection changed identity")
        if kind == RECORDS_STEP_KIND:
            result = records_module.append_record(
                vault_root,
                manifest,
                item=args["item"],
                item_key=prepared["item_key"],
                expected_container_hash=args["expected_container_hash"],
                why=args["why"],
                body=args.get("body"),
                hold=False,
            )
        else:
            result = records_module.update_record(
                vault_root,
                manifest,
                **{name: value for name, value in args.items() if name != "collection"},
                hold=False,
                # The kept prior payload names the run that corrected it.
                _history_binding=run,
            )
        return {**result, "path": prepared["destination"]}
    if kind in _RENAME_STEP_KINDS:
        from . import move_file as move_module

        def stage(name: str) -> None:
            if name == "before-rename":
                # The sealed source, rechecked under the move's own guard: a `fields` patch sets its
                # field whatever the page holds, so the moved bytes alone cannot show another write.
                if _guarded_digest(vault_root, str(binding["path"]), stale=True) != binding["before_hash"]:
                    raise move_module.MoveFileError(
                        code="SOURCE_CHANGED", reason="the page changed since its move was sealed"
                    )
                _fault_barrier("move-before-intent")
                if before_rename is not None:
                    before_rename()
                _fault_barrier("move-after-intent")
            else:
                _fault_barrier(f"move-{name}")

        call = _move_call(vault_root, args) if kind == "move" else _unmove_call(args["move"])
        try:
            result = move_module.move_file(vault_root, **call, _stage=stage)
        except move_module.MoveFileError as error:
            raise _error(error.code, error.reason) from error
        return result.as_dict()
    if kind == REPOINT_STEP_KIND:
        from . import repoint_links

        assert run is not None
        return repoint_links.commit(vault_root, prepared, operation=run["operation"])
    if kind in (UNREPOINT_STEP_KIND, REVERT_POINTER_STEP_KIND):
        from . import repoint_links

        assert run is not None
        (material,) = args.values()
        return repoint_links.restore(vault_root, material, operation=run["operation"])
    if kind == "delete":
        from . import delete_file as delete_module

        now_value = (runtime_args or {}).get("now")
        now = dt.datetime.fromisoformat(now_value) if isinstance(now_value, str) else None
        try:
            result = delete_module.delete_file(vault_root, now=now, **args)
        except delete_module.DeleteFileError as error:
            raise _error(error.code, error.reason) from error
        if isinstance(result, delete_module.DeleteFileValidation):
            raise _error("CURATION_STEP_FAILED", "delete unexpectedly remained validate-only")
        return result.as_dict()
    assert kind == "recover"
    return commands.op_manage_memory_file(vault_root, operation="recover", **args)


def _runtime_execution_binding(
    vault_root: Path,
    step: Mapping[str, Any],
    binding: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    execution = json.loads(canonical_json(binding))
    effect = {
        "kind": step["kind"],
        "path": str(binding["postcondition"]["path"]),
    }
    runtime_args: dict[str, Any] = {}
    if step["kind"] == "delete":
        from . import delete_file as delete_module

        now = dt.datetime.now()
        try:
            validation = delete_module.delete_file(
                vault_root, validate_only=True, now=now, **dict(step["args"])
            )
        except delete_module.DeleteFileError as error:
            raise _error(error.code, error.reason) from error
        if not isinstance(validation, delete_module.DeleteFileValidation):
            raise _error("CURATION_STEP_INVALID", "delete leaf did not seal its destination")
        source = str(binding["path"])
        trash = validation.result.trash_path
        execution["effect_after"] = [
            {"path": source, "absent": True},
            {"path": trash, "content_hash": validation.content_hash},
        ]
        runtime_args["now"] = now.isoformat()
        effect.update(
            {
                "path": source,
                "trash_path": trash,
                "trash_meta_path": validation.result.trash_meta_path,
            }
        )
    elif step["kind"] in _RENAME_STEP_KINDS:
        effect.update(
            {
                "old_path": str(binding["path"]),
                "new_path": str(binding["postcondition"]["path"]),
                "path": str(binding["postcondition"]["path"]),
            }
        )
    elif step["kind"] == "recover":
        effect.update(
            {
                "trash_path": step["args"]["trash_path"],
                "restored_path": binding["postcondition"]["path"],
                "path": binding["postcondition"]["path"],
            }
        )
    return execution, effect, runtime_args


def _evidence_path(store: CurationStore, run_identity: str, operation_identity: str) -> Path:
    return store.evidence_dir(run_identity) / f"{operation_identity}.json"


#: Where a move whose intent record exists left its page, by the rename-after state it sealed.
_UNPROVABLE = "unprovable"
_ADOPT = "adopt"
_STALE = "stale"
_UNCERTAIN = "uncertain"


def _rename_placement(vault_root: Path, binding: Mapping[str, Any]) -> str:
    """Where a move whose intent record exists left its page (the move intent requirement).

    The page at its old path with the sealed bytes is a rename that did not run or was renamed
    back, which no evidence tells apart; the old path empty and the new path holding the sealed
    bytes is a rename the batch has yet to follow; the old path holding other bytes is later work.
    """
    old, new = str(binding["path"]), str(binding["postcondition"]["path"])
    sealed = binding["before_hash"]

    def held(path: str) -> str | None:
        if _guarded_absent(vault_root, path):
            return None
        try:
            return _guarded_digest(vault_root, path, stale=True)
        except CurationError:
            return ""

    at_old, at_new = held(old), held(new)
    if at_new is None and at_old == sealed:
        return _UNPROVABLE
    if at_old is None and at_new == sealed:
        return _ADOPT
    if at_new is None and at_old is not None:
        return _STALE
    return _UNCERTAIN


def _void_unrenamed(
    vault_root: Path,
    store: CurationStore,
    run_identity: str,
    *,
    step: Mapping[str, Any],
    binding: Mapping[str, Any],
    operation_identity: str,
) -> None:
    """Void the intent record of a move that raised in this process and left its page in place.

    The raise proves that no rename stands, which no later evidence can prove (`_UNPROVABLE`), so
    a resume retries the step instead of blocking for good. A stop that kills the process leaves
    the record, and the resume still refuses.
    """
    if step["kind"] not in _RENAME_STEP_KINDS:
        return
    try:
        if store.rename_intent(run_identity, operation_identity) is None:
            return
        if _rename_placement(vault_root, binding) == _UNPROVABLE:
            store.void_rename_intent(run_identity, operation_identity)
    except (CurationError, OSError):
        log.warning("could not void the intent record of %s", operation_identity, exc_info=True)


def _forget_old_path(vault_root: Path, binding: Mapping[str, Any]) -> None:
    """Drop a committed move's old path from the derived indexes again.

    The move leaf does that after its batch, so a stop between the two leaves the indexes and the
    graph serving the old path; the removal is idempotent.
    """
    from . import index_sync

    try:
        index_sync.delete_after_remove(vault_root, [str(binding["path"])])
    except Exception:  # noqa: BLE001 - the move stands; reconcile repairs the indexes
        log.exception("index delete failed for the recovered move of %s", binding["path"])


def _record_rename_intent(
    store: CurationStore,
    run_identity: str,
    *,
    plan_identity: str,
    step: Mapping[str, Any],
    binding: Mapping[str, Any],
    operation_identity: str,
) -> None:
    """Publish the intent record of a move about to rename, with its sealed rename-after state."""
    old, new = str(binding["path"]), str(binding["postcondition"]["path"])
    store.create_rename_intent(
        run_identity,
        {
            "version": 1,
            "run_id": run_identity,
            "plan_id": plan_identity,
            "operation_id": operation_identity,
            "step_id": step["step_id"],
            "old_path": old,
            "new_path": new,
            "before_hash": binding["before_hash"],
            "rename_after": [
                {"path": old, "absent": True},
                {"path": new, "content_hash": binding["before_hash"]},
            ],
        },
    )


def _adopt_rename(
    vault_root: Path,
    store: CurationStore,
    run_identity: str,
    *,
    plan_identity: str,
    ordinal: int,
    step: Mapping[str, Any],
    binding: Mapping[str, Any],
    operation_identity: str,
    compensation: bool,
) -> dict[str, Any]:
    """Roll forward a move that a stop cut between its rename and its batch, without renaming.

    The adopting batch lands the sealed destination bytes with the witness, and the move's
    semantic, catalog, graph and index effects follow (`move_file.adopt_move`).
    """
    from . import curation_witness
    from . import move_file as move_module

    kind = str(step["kind"])
    args = json.loads(canonical_json(step["args"]))
    call = _move_call(vault_root, args) if kind == "move" else _unmove_call(args["move"])
    witness = _witness_payload(
        run_identity=run_identity,
        plan_identity=plan_identity,
        ordinal=ordinal,
        step=step,
        operation_identity=operation_identity,
        binding=binding,
        parent_compensation_plan_id=plan_identity if compensation else None,
    )
    evidence_path = _evidence_path(store, run_identity, operation_identity)
    with curation_witness.atomic_witness(
        vault_root,
        evidence_path,
        canonical_json(witness),
        finalize=lambda writes, content: _finalize_witness_batch(
            vault_root, step, binding, writes, content
        ),
    ) as pending:
        try:
            move_module.adopt_move(
                vault_root,
                old_path=str(binding["path"]),
                new_path=str(binding["postcondition"]["path"]),
                source_hash=binding["before_hash"],
                content_transform=call.get("content_transform"),
                extra_writes=tuple(call.get("extra_writes") or ()),
                exact_content=kind != "move",
            )
        except move_module.MoveFileError as error:
            raise _error(error.code, error.reason) from error
        if not pending.consumed:
            raise _error("CURATION_OUTCOME_UNCERTAIN", "the adopted move did not commit its witness")
    validated = _validate_witness(
        store._read_json(evidence_path),
        run_identity=run_identity,
        plan_identity=plan_identity,
        ordinal=ordinal,
        step=step,
        operation_identity=operation_identity,
        binding=binding,
        parent_compensation_plan_id=plan_identity if compensation else None,
    )
    _verify_live_postcondition(vault_root, validated, step=step, binding=binding)
    _execution, effect, _runtime = _runtime_execution_binding(vault_root, step, binding)
    receipt = {
        "version": 1,
        "attempt": _next_attempt(store, run_identity, step["step_id"]),
        "ordinal": ordinal,
        "step_id": step["step_id"],
        "operation_id": operation_identity,
        "outcome": "recovered-committed",
        "result_digest": validated["result_digest"],
        "effect": effect,
    }
    store.create_receipt(run_identity, ordinal, step["step_id"], receipt)
    final = store.reconstruct(run_identity)
    store.write_state(run_identity, final)
    return _compensation_phase(
        {**final, "mutated": True, "step": {**receipt, "path": effect["path"]}},
        compensation=compensation,
    )


def _recover_rename(
    vault_root: Path,
    store: CurationStore,
    run_identity: str,
    *,
    plan_identity: str,
    ordinal: int,
    step: Mapping[str, Any],
    binding: Mapping[str, Any],
    operation_identity: str,
    compensation: bool,
) -> dict[str, Any]:
    """Decide a move whose intent record exists from its live placement; never rename."""
    placement = _rename_placement(vault_root, binding)
    if placement == _ADOPT:
        return _adopt_rename(
            vault_root,
            store,
            run_identity,
            plan_identity=plan_identity,
            ordinal=ordinal,
            step=step,
            binding=binding,
            operation_identity=operation_identity,
            compensation=compensation,
        )
    if placement == _STALE:
        _record_failed_attempt(
            store,
            run_identity,
            ordinal=ordinal,
            step_id=step["step_id"],
            operation_identity=operation_identity,
            code="CURATION_BINDING_STALE",
            retryable=False,
        )
        raise _error("CURATION_BINDING_STALE", f"{binding['path']!r} changed after its move began")
    code, reason = (
        (
            "CURATION_RENAME_HISTORY_UNPROVABLE",
            "the page is at its old path after its move's intent record; re-propose the remaining work",
        )
        if placement == _UNPROVABLE
        else ("CURATION_OUTCOME_UNCERTAIN", "the moved page is at neither or both of its paths")
    )
    _record_blocked_state(
        store,
        run_identity,
        operation_identity=operation_identity,
        step_id=step["step_id"],
        code=code,
    )
    raise _error(code, reason)


def _committed_step_ids(state: Mapping[str, Any]) -> set[str]:
    return set(str(item) for item in state.get("committed_steps", []))


def _next_step(
    plan: Mapping[str, Any], state: Mapping[str, Any]
) -> tuple[int, Mapping[str, Any], Mapping[str, Any], bool] | None:
    """The first uncommitted step, and whether a receipt refused it for good.

    A refused step is still returned: its refusal stands only while the step has no committed
    witness and, for a move, no rename intent. Another process can record that refusal before the
    step's leaf commits, and the call that ran the leaf can stop before it records the receipt.
    Each caller probes that evidence first.
    """
    committed = _committed_step_ids(state)
    for ordinal, step in enumerate(plan["steps"]):
        if step["step_id"] not in committed:
            refused = state.get("failed_step") == step["step_id"] and not state.get("retryable", False)
            return ordinal, step, plan["binding_manifest"][ordinal], refused
    return None


def _uncommitted_plan(plan: Mapping[str, Any], committed: set[str]) -> dict[str, Any]:
    return {
        **plan,
        "binding_manifest": [
            item for item in plan["binding_manifest"] if item["step_id"] not in committed
        ],
    }


def _blockers_for_uncommitted(
    vault_root: Path, plan: Mapping[str, Any], committed: set[str]
) -> list[dict[str, Any]]:
    if not committed and isinstance(plan.get("entity_candidate"), Mapping):
        try:
            _validate_entity_candidate_plan(vault_root, plan)
        except CurationError as error:
            return [{"code": error.code, "reason": error.reason}]
    return _binding_blockers(
        vault_root, _uncommitted_plan(plan, committed), plan_registry_identities(vault_root, plan)
    )


def _record_blocked_state(
    store: CurationStore,
    run_identity: str,
    *,
    operation_identity: str,
    step_id: str,
    code: str = "CURATION_OUTCOME_UNCERTAIN",
) -> None:
    store.write_state(
        run_identity,
        {
            "version": 1,
            "run_id": run_identity,
            "phase": "blocked",
            "error_code": code,
            "prepared": {"operation_id": operation_identity, "step_id": step_id},
        },
    )


def _compensation_phase(result: dict[str, Any], *, compensation: bool) -> dict[str, Any]:
    if not compensation:
        return result
    phase = {
        "proposed": "proposed",
        "approved": "compensating",
        "executing": "compensating",
        "partial": "compensation-partial",
        "failed": "compensation-partial",
        "completed": "compensated",
        "blocked": "blocked",
    }.get(str(result.get("phase")), str(result.get("phase")))
    return {**result, "phase": phase}


def _effect_projection(
    kind: str, result: Mapping[str, Any], binding: Mapping[str, Any]
) -> dict[str, Any]:
    allowed = {
        key: result[key]
        for key in (
            "path",
            "new_path",
            "old_path",
            "trash_path",
            "trash_meta_path",
            "restored_path",
            "ref",
        )
        if isinstance(result.get(key), str)
    }
    if "path" not in allowed:
        allowed["path"] = str(binding["postcondition"]["path"])
    allowed["kind"] = kind
    return allowed


def _next_attempt(store: CurationStore, run_identity: str, step_id: str) -> int:
    attempts = [
        int(receipt["attempt"])
        for receipt in store._receipt_records(run_identity)
        if receipt.get("step_id") == step_id and type(receipt.get("attempt")) is int
    ]
    return max(attempts, default=0) + 1


def _validated_operation_witness(
    store: CurationStore,
    run_identity: str,
    *,
    plan_identity: str,
    ordinal: int,
    step: Mapping[str, Any],
    binding: Mapping[str, Any],
    operation_identity: str,
    compensation: bool,
    live: bool = True,
) -> dict[str, Any] | None:
    """Load one exact immutable witness, distinguishing absence from corruption.

    `live=False` validates the witness as historical evidence only; its live
    postcondition is then the caller's to check (the episode coverage chain
    checks it for the last leaf on each path alone).
    """
    try:
        raw = store._read_json(_evidence_path(store, run_identity, operation_identity))
    except CurationError as error:
        if error.code == "CURATION_RUN_NOT_FOUND":
            return None
        raise
    witness = _validate_witness(
        raw,
        run_identity=run_identity,
        plan_identity=plan_identity,
        ordinal=ordinal,
        step=step,
        operation_identity=operation_identity,
        binding=binding,
        parent_compensation_plan_id=plan_identity if compensation else None,
    )
    if live:
        _verify_live_postcondition(
            store.vault_root, witness, step=step, binding=binding, accepted=_chained_states(step, binding)
        )
    return witness


def _recover_committed_witness(
    store: CurationStore,
    run_identity: str,
    *,
    ordinal: int,
    step: Mapping[str, Any],
    operation_identity: str,
    witness: Mapping[str, Any],
    effect: Mapping[str, Any],
    compensation: bool,
) -> dict[str, Any]:
    """Publish the receipt required by a valid same-batch content witness."""
    receipt = {
        "version": 1,
        "attempt": _next_attempt(store, run_identity, str(step["step_id"])),
        "ordinal": ordinal,
        "step_id": step["step_id"],
        "operation_id": operation_identity,
        "outcome": "recovered-committed",
        "result_digest": witness["result_digest"],
        "effect": dict(effect),
    }
    store.create_receipt(run_identity, ordinal, str(step["step_id"]), receipt)
    final = store.reconstruct(run_identity)
    store.write_state(run_identity, final)
    return _compensation_phase(
        {
            **final,
            "mutated": True,
            "step": {**receipt, "path": witness["after"][0]["path"]},
        },
        compensation=compensation,
    )


def _record_failed_attempt(
    store: CurationStore,
    run_identity: str,
    *,
    ordinal: int,
    step_id: str,
    operation_identity: str,
    code: str,
    retryable: bool,
) -> dict[str, Any]:
    receipt = {
        "version": 1,
        "attempt": _next_attempt(store, run_identity, step_id),
        "ordinal": ordinal,
        "step_id": step_id,
        "operation_id": operation_identity,
        "outcome": "failed",
        "error_code": code,
        "retryable": retryable,
    }
    store.create_receipt(run_identity, ordinal, step_id, receipt)
    state = store.reconstruct(run_identity)
    store.write_state(run_identity, state)
    return receipt


_ACTIVE_EPISODE_INVOCATION: ContextVar[tuple | None] = ContextVar("curation_episode_invocation", default=None)


@contextmanager
def _episode_invocation(vault_root: Path, plan: Mapping, binding: Mapping, run: str, operation: str):
    # The run's claim comes before the journal's attempt mark: a busy run marks no attempt.
    with _run_claim(Path(vault_root), run):
        invocation = _join_episode_invocation(vault_root, plan, binding, run, operation)
        token = _ACTIVE_EPISODE_INVOCATION.set((Path(vault_root).resolve(), run, operation, invocation))
        try:
            yield invocation
        finally:
            _ACTIVE_EPISODE_INVOCATION.reset(token)


def _join_episode_invocation(vault_root: Path, plan: Mapping, binding: Mapping, run: str, operation: str) -> dict | None:
    metadata = binding.get("prepared", {}).get("episode")
    if metadata is None:
        return None
    from . import episode_model, episode_workflow

    session = episode_workflow._Session(vault_root, metadata["key"])  # noqa: SLF001
    candidate, leaf_id = metadata["candidate_id"], metadata["leaf_id"]
    leaf = episode_model._owned(session.state, candidate, leaf_id)  # noqa: SLF001
    if leaf["effect_digest"] != metadata["effect_digest"] or leaf["binding"] is None or (
        leaf["binding"]["run_id"], leaf["binding"]["operation_id"]
    ) != (run, operation):
        # Checked before any knowledge write (no step runs): this run is not the episode's bound invocation.
        raise _error("CURATION_BINDING_STALE", "the episode binds this leaf to another run or effect")
    episode_model._binding({**leaf["binding"], "sealed_plan": plan}, leaf)  # noqa: SLF001
    active = _ACTIVE_EPISODE_INVOCATION.get()
    if active is not None and active[:3] == (Path(vault_root).resolve(), run, operation):
        # Only the enclosing, still-running owner call may join its already-recorded invocation.
        if active[3] is None or leaf.get("invocation_id") != active[3]["invocation_id"] or leaf["outcome"] != "uncertain":
            raise _error("CURATION_OUTCOME_UNCERTAIN", "active episode invocation changed")
        return active[3]
    # Pre-dispatch journal bookkeeping, like this store's plan and state files, is not the
    # command's commit: a writer refusal after it must answer uncommitted with its own code.
    if leaf["outcome"] == "uncertain":
        _record_recovery_proof(vault_root, session.state, candidate, leaf_id)
        session.transition("reconcile_curation_leaf", commit_point=False, candidate=candidate, leaf=leaf_id)
        leaf = episode_model._owned(session.state, candidate, leaf_id)  # noqa: SLF001
        if leaf["outcome"] != "proven_uncommitted":
            raise _error(
                "CURATION_OUTCOME_UNCERTAIN",
                "the earlier episode attempt is not proven uncommitted, so this leaf cannot run again automatically",
            )
    session.transition("mark_curation_invocation", commit_point=False, candidate=candidate, leaf=leaf_id)
    leaf = episode_model._owned(session.state, candidate, leaf_id)  # noqa: SLF001
    return {"invocation_id": leaf["invocation_id"], "episode_attempt": leaf["attempts"]}


def _no_effect_evidence(reason: Any, *, run: str, step: Mapping, binding: Mapping, operation: str) -> dict | None:
    """The exact evidence a no-effect terminal of this step carries for `reason`; None when it does not fit.

    Each closed reason fits one step family alone: the native destination stop and curation's
    unstarted-publication proof fit a preservation step, the transaction-absence proof a store-sealed step.
    """
    prepared = binding.get("prepared", {})
    if step["kind"] in PRESERVATION_STEP_KINDS:
        if reason == DefiniteNoEffect.DESTINATION_EXISTS:
            return DefiniteNoEffect.destination_evidence(prepared["destination"])
        if reason == RECOVERY_PUBLICATION_NOT_STARTED:
            return {"destination": prepared["destination"], "publication_started": False}
    if step["kind"] in COLLECTION_STEP_KINDS and prepared.get("store") is not None and reason == RECOVERY_STORE_TRANSACTION_ABSENT:
        return {"request_id": store_request_id(run, operation)}
    return None


def _no_effect_receipt(
    store: CurationStore, run: str, *, ordinal: int, step: Mapping, operation: str, invocation: Mapping,
    plan_identity: str, binding: Mapping, reason: str, evidence: Mapping,
) -> dict[str, Any]:
    """The one terminal shape of a typed no-effect stop, native or curation's recovery proof."""
    return {
        "version": 1, "attempt": _next_attempt(store, run, str(step["step_id"])),
        "ordinal": ordinal, "step_id": step["step_id"], "operation_id": operation,
        "outcome": "definite-no-effect", "invocation_id": invocation["invocation_id"],
        "episode_attempt": invocation["episode_attempt"], "plan_id": plan_identity,
        "prepared_effect_digest": _digest(binding), "native_reason": reason, "native_evidence": dict(evidence),
    }


def _publication_record(
    run: str, plan_identity: str, operation: str, invocation: Mapping, binding: Mapping, *, started: bool
) -> dict[str, Any]:
    """One native preservation attempt's publication record, in its intent or its started form.

    `intent` and `started` are the two states that this record format fixes. Curation creates the
    intent before dispatch, and the vault marks it started just before the attempt's first install.
    """
    return {
        "version": 1, "state": "started" if started else "intent", "run_id": run, "plan_id": plan_identity,
        "operation_id": operation, "invocation_id": invocation["invocation_id"],
        "episode_attempt": invocation["episode_attempt"], "prepared_effect_digest": _digest(binding),
    }


def _valid_no_effect(receipt: Mapping, *, run: str, plan_identity: str, step: Mapping, binding: Mapping, operation: str) -> bool:
    from . import episode_model

    metadata = binding.get("prepared", {}).get("episode")
    # The terminal fields are fixed by the typed no-effect protocol; `_no_effect_evidence` owns its closed reasons.
    if not isinstance(metadata, Mapping) or set(receipt) != {
        "version", "attempt", "ordinal", "step_id", "operation_id", "outcome", "invocation_id",
        "episode_attempt", "plan_id", "prepared_effect_digest", "native_reason", "native_evidence",
    } or receipt.get("outcome") != "definite-no-effect":
        return False
    evidence = _no_effect_evidence(receipt.get("native_reason"), run=run, step=step, binding=binding, operation=operation)
    attempt = receipt.get("episode_attempt")
    if evidence is None or type(attempt) is not int or attempt < 1 or receipt.get("plan_id") != plan_identity or receipt.get("prepared_effect_digest") != _digest(binding):
        return False
    identity = episode_model._hash(  # noqa: SLF001
        "exomem-episode-invocation-v1", episode_model.episode_id(metadata["key"]), metadata["leaf_id"], metadata["effect_digest"], operation, attempt
    )
    return receipt.get("invocation_id") == identity and receipt.get("native_evidence") == evidence


def _record_recovery_proof(vault_root: Path, state: Mapping[str, Any], candidate_id: str, leaf_id: str) -> None:
    """Prove that a stopped episode attempt published nothing, or repair the commit it left.

    Under the held vault mutation boundary alone, for the latest attempt of a bound uncertain leaf
    with an invocation identity, and only when every receipt of its operation is an earlier
    attempt's no-effect terminal and no commitment is found. A Records or Planning step sealed to
    the store that this thread serves proves that the store holds no transaction for its request
    id. A native preservation step proves that its publication record still holds the exact intent
    bytes, so the attempt never reached its first install; it never reads its output paths, and a
    vault whose writer lease is coordinated across hosts gets no such proof. Anything else changes
    nothing, so the leaf stays uncertain. Episode reconciliation then reads the terminal like any
    other receipt.
    """
    from . import episode_model, writer_lease
    from .collection_store.preview import selected_writer

    root = Path(vault_root)
    if not writer_lease.active_manager().holds_mutation_boundary(root):
        return
    leaf = episode_model._owned(state, candidate_id, leaf_id)  # noqa: SLF001
    binding, attempts = leaf["binding"], leaf["attempts"]
    if (
        leaf["outcome"] != "uncertain"
        or binding is None
        or leaf.get("invocation_id") is None
        or leaf["invocation_id"] != episode_model.invocation_id(state["episode_id"], leaf, attempts)
        or any(attempt["outcome"] == "uncertain" for attempt in leaf["attempt_history"][:-1])
    ):
        return
    store = CurationStore(root)
    run = binding["run_id"]
    try:
        plan = store.load_plan(run)
        identity, fingerprint = store.identities(run)
        verified = episode_model._binding({**binding, "sealed_plan": plan}, leaf)  # noqa: SLF001
        if (identity, fingerprint) != (verified["plan_id"], verified["plan_fingerprint"]):
            return
        store.validated_approval(run, required=True)
        reconstructed = store.reconstruct(run)
    except (CurationError, episode_model.EpisodeError):
        return
    if reconstructed["phase"] == "blocked":
        return
    ordinal, operation = verified["ordinal"], verified["operation_id"]
    step, sealed = plan["steps"][ordinal], plan["binding_manifest"][ordinal]
    if step["kind"] in COLLECTION_STEP_KINDS and sealed["prepared"].get("store") is not None:
        from .cli_ops import OpError

        try:
            writer = selected_writer(root, sealed["prepared"]["collection_id"])
        except OpError:
            return
        if writer is None:
            return
        reason = RECOVERY_STORE_TRANSACTION_ABSENT
    elif step["kind"] in PRESERVATION_STEP_KINDS and not writer_lease.active_manager().config.coordinated_across_hosts:
        # Under writer-lease coordination across hosts, another host can take the lease over before this
        # host's `started` reaches it.
        writer, reason = None, RECOVERY_PUBLICATION_NOT_STARTED
    else:
        return
    receipts = [receipt for receipt in reconstructed["receipts"] if receipt.get("operation_id") == operation]
    if any(receipt.get("outcome") in COMMITTED_OUTCOMES for receipt in receipts):
        return
    try:
        # A store step's witness is its transaction row, which can exist without its evidence file.
        witness = _committed_operation_witness(
            store, run, writer=writer, plan_identity=identity, ordinal=ordinal, step=step,
            binding=sealed, operation_identity=operation, compensation=False,
        )
    except CurationError:
        return
    if witness is not None:
        _recover_committed_witness(
            store, run, ordinal=ordinal, step=step, operation_identity=operation, witness=witness,
            effect=_effect_projection(str(step["kind"]), {}, sealed), compensation=False,
        )
        return
    if not all(
        receipt.get("outcome") == "definite-no-effect"
        and _valid_no_effect(receipt, run=run, plan_identity=identity, step=step, binding=sealed, operation=operation)
        and receipt["episode_attempt"] < attempts
        for receipt in receipts
    ):
        return
    invocation = {"invocation_id": leaf["invocation_id"], "episode_attempt": attempts}
    if reason == RECOVERY_PUBLICATION_NOT_STARTED:
        intent = _publication_record(run, identity, operation, invocation, sealed, started=False)
        try:
            recorded = store._read_bytes(store.publication_record_path(run, leaf["invocation_id"]))
        except CurationError:
            return
        if recorded != canonical_json_bytes(intent):
            return
    receipt = _no_effect_receipt(
        store, run, ordinal=ordinal, step=step, operation=operation, invocation=invocation,
        plan_identity=identity, binding=sealed, reason=reason,
        evidence=_no_effect_evidence(reason, run=run, step=step, binding=sealed, operation=operation),
    )
    writer_lease.validate_active_write_fence()
    store.create_receipt(run, ordinal, str(step["step_id"]), receipt)
    store.write_state(run, store.reconstruct(run))


def _guards_itself(step: Mapping[str, Any], binding: Mapping[str, Any]) -> bool:
    """Whether this step's leaf may run outside curation's hold (`SELF_GUARDED_STEP_KINDS`).

    A pointer correction writes through `set_frontmatter_field`, and an episode-bound step joins
    the episode's journal, so both keep the whole step under one hold.
    """
    prepared = binding.get("prepared", {})
    return (
        step["kind"] in SELF_GUARDED_STEP_KINDS
        and prepared.get("pointer") is not True
        and not isinstance(prepared.get("episode"), Mapping)
    )


def _execute_next(
    vault_root: Path,
    run_identity: str,
    plan_identity: str,
    *,
    store: CurationStore | None = None,
    compensation: bool = False,
) -> dict[str, Any]:
    # `seam` is a scoped step's second short hold: `_execute_step` enters it when its leaf returns
    # or raises, and it covers the witness check, the postcondition, the receipt and the state.
    with ExitStack() as seam:
        return _execute_step(
            vault_root, run_identity, plan_identity, store=store, compensation=compensation, seam=seam
        )


def _execute_step(
    vault_root: Path,
    run_identity: str,
    plan_identity: str,
    *,
    store: CurationStore | None = None,
    compensation: bool = False,
    seam: ExitStack,
) -> dict[str, Any]:
    from . import curation_witness, writer_lease
    from .cli_ops import OpError
    from .collection_store.preview import selected_writer

    store = store or CurationStore(vault_root)
    root = Path(vault_root)
    # Under an outer hold (the episode workflow, the wide-boundary switch, `under_boundary`) the
    # step runs whole, as before. Otherwise curation holds the boundary only at its own seams.
    scoped = not writer_lease.active_manager().holds_mutation_boundary(root)

    def under_boundary() -> dict[str, Any]:
        # A step that is not a clean self-guarded one is decided again under one hold, from fresh reads.
        with _step_boundary(root, "curation_step"):
            return _execute_next(
                vault_root, run_identity, plan_identity, store=store, compensation=compensation
            )

    plan = store.load_plan(run_identity)
    expected_identity, _fingerprint = store.identities(run_identity)
    if plan_identity != expected_identity:
        raise _error("CURATION_PLAN_IDENTITY_MISMATCH", "resume cannot switch plans")
    _require_plan_relocation_history(vault_root, plan)
    reconstructed = store.reconstruct(run_identity)
    if reconstructed.get("phase") == "blocked":
        raise _error("CURATION_OUTCOME_UNCERTAIN", "run evidence is invalid")
    next_item = _next_step(plan, reconstructed)
    if next_item is None:
        return _compensation_phase(
            {**reconstructed, "outcome": "replayed", "mutated": False},
            compensation=compensation,
        )
    ordinal, step, binding, refused = next_item
    if scoped and not _guards_itself(step, binding):
        return under_boundary()
    operation_identity = operation_id(plan_identity, ordinal, step["step_id"])
    evidence_path = _evidence_path(store, run_identity, operation_identity)
    # A Records step on a collection the store owns commits there; its transaction row is the witness.
    writer = (
        selected_writer(vault_root, binding["prepared"]["collection_id"])
        if step["kind"] in COLLECTION_STEP_KINDS and not compensation
        else None
    )
    execution_binding = dict(binding)
    recovered_effect = _effect_projection(str(step["kind"]), {}, execution_binding)
    try:
        committed_witness = _committed_operation_witness(
            store,
            run_identity,
            writer=writer,
            plan_identity=plan_identity,
            ordinal=ordinal,
            step=step,
            binding=execution_binding,
            operation_identity=operation_identity,
            compensation=compensation,
        )
    except CurationError:
        if scoped:
            return under_boundary()
        _record_blocked_state(
            store,
            run_identity,
            operation_identity=operation_identity,
            step_id=step["step_id"],
        )
        raise
    if committed_witness is not None:
        if scoped:
            return under_boundary()
        if step["kind"] in _RENAME_STEP_KINDS:
            _forget_old_path(vault_root, binding)
        return _recover_committed_witness(
            store,
            run_identity,
            ordinal=ordinal,
            step=step,
            operation_identity=operation_identity,
            witness=committed_witness,
            effect=recovered_effect,
            compensation=compensation,
        )
    if step["kind"] in _RENAME_STEP_KINDS and writer is None:
        try:
            intent = store.rename_intent(run_identity, operation_identity)
        except CurationError:
            if scoped:
                return under_boundary()
            _record_blocked_state(
                store, run_identity, operation_identity=operation_identity, step_id=step["step_id"]
            )
            raise
        if intent is not None:
            if scoped:
                return under_boundary()
            return _recover_rename(
                vault_root,
                store,
                run_identity,
                plan_identity=plan_identity,
                ordinal=ordinal,
                step=step,
                binding=binding,
                operation_identity=operation_identity,
                compensation=compensation,
            )
    if refused:
        # The run stopped on a refusal that left neither a witness nor a rename intent: a retry
        # hears that refusal again, never a replay of a run that did not complete.
        codes = [
            receipt["error_code"]
            for receipt in reconstructed["receipts"]
            if receipt.get("step_id") == step["step_id"] and receipt.get("outcome") == "failed"
        ]
        raise _error(codes[-1], f"curation step {step['step_id']!r} was refused and did not commit")

    committed = _committed_step_ids(reconstructed)
    blockers = _blockers_for_uncommitted(vault_root, plan, committed)
    if blockers and scoped:
        return under_boundary()
    if blockers:
        blocker = blockers[0]
        if blocker["code"] == RECORDS_STORE_UNSUPPORTED:
            # The store owns the collection and this is not its thread. Refusing without a receipt
            # keeps the run's phase, so a run approved before the migration stays resumable there.
            raise _error(blocker["code"], blocker["reason"])
        _record_failed_attempt(
            store,
            run_identity,
            ordinal=ordinal,
            step_id=step["step_id"],
            operation_identity=operation_identity,
            code=blocker["code"],
            retryable=False,
        )
        raise _error(blocker["code"], blocker["reason"])
    execution_binding, prepared_effect, runtime_args = _runtime_execution_binding(
        vault_root, step, binding
    )
    invocation = _join_episode_invocation(vault_root, plan, binding, run_identity, operation_identity)
    before_publication = None
    if invocation is not None and step["kind"] in PRESERVATION_STEP_KINDS:
        # A stopped attempt may retry only while this record still reads intent (see `_record_recovery_proof`).
        intent = _publication_record(run_identity, plan_identity, operation_identity, invocation, binding, started=False)
        started = _publication_record(run_identity, plan_identity, operation_identity, invocation, binding, started=True)
        record_path = store.create_publication_record(run_identity, intent)
        before_publication = functools.partial(store.mark_publication_started, record_path, intent, started)
    prepared_state = {
        "version": 1,
        "run_id": run_identity,
        "plan_id": plan_identity,
        "phase": "executing",
        "prepared": {
            "ordinal": ordinal,
            "step_id": step["step_id"],
            "operation_id": operation_identity,
        },
    }
    store.write_state(run_identity, prepared_state)
    _fault_barrier("after-prepared-state")
    witness = _witness_payload(
        run_identity=run_identity,
        plan_identity=plan_identity,
        ordinal=ordinal,
        step=step,
        operation_identity=operation_identity,
        binding=binding,
        parent_compensation_plan_id=plan_identity if compensation else None,
    )
    settle_refusal: BaseException | None = None
    # The leaf's witness context binds it; it stays None for a store step and before the leaf.
    pending = None

    def settle() -> None:
        # A scoped leaf ran outside curation's hold; what settles it runs under one short hold.
        # When that hold is refused, nothing more is written: the next resume settles the step.
        nonlocal settle_refusal
        if not scoped:
            return
        try:
            seam.enter_context(_step_boundary(root, "curation_step_settle"))
            # The run claim is per process, so another process can record this step while its leaf
            # runs. A committed receipt settles it: that process's witness kept this leaf's batch
            # out, or it recovered this leaf's witness. A refusal does not: it can predate this
            # leaf's commit, so this call settles its own step after it.
            if any(
                receipt.get("step_id") == step["step_id"]
                and receipt.get("outcome") in COMMITTED_OUTCOMES
                for receipt in store._receipt_records(run_identity)
            ):
                raise CurationRunBusy(
                    "CURATION_RUN_BUSY",
                    "another call settled this step first",
                    leaf_committed=True if pending is not None and pending.consumed else None,
                )
        except BaseException as refusal:
            settle_refusal = refusal
            raise

    try:
        with (
            # A store step has no file batch to carry its witness: the store's row is the witness.
            nullcontext()
            if writer is not None
            else curation_witness.atomic_witness(
                vault_root,
                evidence_path,
                canonical_json(witness),
                finalize=lambda writes, content: _finalize_witness_batch(
                    vault_root, step, execution_binding, writes, content
                ),
                before_publication=before_publication,
            )
        ) as pending:
            leaf_result = _dispatch_step(
                vault_root,
                step,
                execution_binding,
                runtime_args=runtime_args,
                run={"curation_run": run_identity, "operation": operation_identity},
                before_rename=functools.partial(
                    _record_rename_intent,
                    store,
                    run_identity,
                    plan_identity=plan_identity,
                    step=step,
                    binding=execution_binding,
                    operation_identity=operation_identity,
                ),
            )
            settle()
            if writer is not None:
                _publish_store_witness(
                    store,
                    run_identity,
                    writer=writer,
                    plan_identity=plan_identity,
                    ordinal=ordinal,
                    step=step,
                    binding=execution_binding,
                    operation_identity=operation_identity,
                )
            elif not pending.consumed:
                raise _error(
                    "CURATION_OUTCOME_UNCERTAIN",
                    f"{step['kind']} did not commit its witness with the leaf effect",
                )
        witness = _validate_witness(
            store._read_json(evidence_path),
            run_identity=run_identity,
            plan_identity=plan_identity,
            ordinal=ordinal,
            step=step,
            operation_identity=operation_identity,
            binding=execution_binding,
            parent_compensation_plan_id=plan_identity if compensation else None,
        )
        # This call saw its leaf's batch commit the witness, so the step committed. A scoped leaf
        # released the boundary there, and a page that differs now holds a later writer's work. A
        # witness found any other way proves the step only while its pages match it: a batch whose
        # rollback could not remove the witness restored the pages without it.
        proven = scoped and pending is not None and pending.consumed
        _verify_live_postcondition(
            vault_root,
            witness,
            step=step,
            binding=execution_binding,
            accepted=_chained_states(step, execution_binding),
            drift=None if proven else "CURATION_OUTCOME_UNCERTAIN",
        )
    except DefiniteNoEffect as error:
        if invocation is None or step["kind"] not in PRESERVATION_STEP_KINDS:
            raise
        receipt = _no_effect_receipt(
            store, run_identity, ordinal=ordinal, step=step, operation=operation_identity, invocation=invocation,
            plan_identity=plan_identity, binding=binding, reason=error.reason, evidence=error.evidence,
        )
        # A native owner stops with its own reason; the recovery reasons belong to curation alone.
        if error.reason != DefiniteNoEffect.DESTINATION_EXISTS or not _valid_no_effect(
            receipt, run=run_identity, plan_identity=plan_identity, step=step, binding=binding, operation=operation_identity
        ):
            raise _error("CURATION_OUTCOME_UNCERTAIN", "native no-effect terminal is invalid") from error
        store.create_receipt(run_identity, ordinal, step["step_id"], receipt)
        final = store.reconstruct(run_identity)
        store.write_state(run_identity, final)
        return {**final, "mutated": True, "step": receipt}
    except CurationError as error:
        if error is settle_refusal:
            raise
        settle()
        try:
            committed_witness = _committed_operation_witness(
                store,
                run_identity,
                writer=writer,
                plan_identity=plan_identity,
                ordinal=ordinal,
                step=step,
                binding=execution_binding,
                operation_identity=operation_identity,
                compensation=compensation,
            )
        except CurationError:
            _record_blocked_state(
                store,
                run_identity,
                operation_identity=operation_identity,
                step_id=step["step_id"],
            )
            raise
        if committed_witness is not None:
            return _recover_committed_witness(
                store,
                run_identity,
                ordinal=ordinal,
                step=step,
                operation_identity=operation_identity,
                witness=committed_witness,
                effect=_effect_projection(str(step["kind"]), {}, execution_binding),
                compensation=compensation,
            )
        _void_unrenamed(
            vault_root, store, run_identity, step=step, binding=execution_binding,
            operation_identity=operation_identity,
        )
        if error.code == "CURATION_OUTCOME_UNCERTAIN":
            _record_blocked_state(
                store,
                run_identity,
                operation_identity=operation_identity,
                step_id=step["step_id"],
            )
            raise
        _record_failed_attempt(
            store,
            run_identity,
            ordinal=ordinal,
            step_id=step["step_id"],
            operation_identity=operation_identity,
            code=error.code,
            retryable=False,
        )
        raise
    except Exception as error:
        if error is settle_refusal:
            raise
        settle()
        try:
            committed_witness = _committed_operation_witness(
                store,
                run_identity,
                writer=writer,
                plan_identity=plan_identity,
                ordinal=ordinal,
                step=step,
                binding=execution_binding,
                operation_identity=operation_identity,
                compensation=compensation,
            )
        except CurationError:
            _record_blocked_state(
                store,
                run_identity,
                operation_identity=operation_identity,
                step_id=step["step_id"],
            )
            raise
        if committed_witness is not None:
            return _recover_committed_witness(
                store,
                run_identity,
                ordinal=ordinal,
                step=step,
                operation_identity=operation_identity,
                witness=committed_witness,
                effect=_effect_projection(str(step["kind"]), {}, execution_binding),
                compensation=compensation,
            )
        _void_unrenamed(
            vault_root, store, run_identity, step=step, binding=execution_binding,
            operation_identity=operation_identity,
        )
        stable_code = getattr(error, "code", None)
        if not isinstance(stable_code, str) and ":" in str(error):
            candidate = str(error).split(":", 1)[0]
            stable_code = candidate if re.fullmatch(r"[A-Z][A-Z0-9_]+", candidate) else None
        retryable = _retryable_failure(error)
        code = stable_code or (
            "CURATION_RETRYABLE_FAILURE" if retryable else "CURATION_STEP_FAILED"
        )
        _record_failed_attempt(
            store,
            run_identity,
            ordinal=ordinal,
            step_id=step["step_id"],
            operation_identity=operation_identity,
            code=code,
            retryable=retryable,
        )
        if scoped and isinstance(error, OpError) and error.code == "MUTATION_BUSY":
            # Only a leaf outside curation's hold meets a busy vault at its own guard. The refusal
            # keeps its protocol shape, so a keyed invoke absorbs it and a caller sees its retry hint.
            raise
        raise _error(code, str(error)) from error
    _fault_barrier("after-compensation-leaf-witness" if compensation else "after-leaf-witness")
    effect = _effect_projection(str(step["kind"]), leaf_result, execution_binding)
    receipt = {
        "version": 1,
        "attempt": _next_attempt(store, run_identity, step["step_id"]),
        "ordinal": ordinal,
        "step_id": step["step_id"],
        "operation_id": operation_identity,
        "outcome": "committed",
        "result_digest": witness["result_digest"],
        "effect": effect,
    }
    store.create_receipt(run_identity, ordinal, step["step_id"], receipt)
    _fault_barrier("after-terminal-receipt")
    final = store.reconstruct(run_identity)
    store.write_state(run_identity, final)
    path = str(
        leaf_result.get("path")
        or leaf_result.get("new_path")
        or leaf_result.get("restored_path")
        or binding["postcondition"]["path"]
    )
    return _compensation_phase(
        {
            **final,
            "mutated": True,
            "step": {**receipt, "path": path, "leaf_result": leaf_result},
        },
        compensation=compensation,
    )


def _call_budget() -> Any:
    """This call's deadline: the request's own, or one of the same length at a door that binds none.

    Over MCP the dispatcher binds one budget per request (`request_budget`); REST, the CLI and a
    direct call get a fresh budget of the same length, as `adoption_run` paces itself.
    """
    from . import request_budget

    return request_budget.current() or request_budget.RequestBudget(
        seconds=request_budget.budget_seconds()
    )


def _stopped(
    error: Exception, before: Mapping[str, Any], after: Mapping[str, Any], plan: Mapping[str, Any]
) -> dict[str, Any]:
    """What a call reports beside its committed steps when a later step stops it.

    `before` is the run after this call's last committed step, whose next step is the one that
    stopped, and `after` the run now. `committed` says whether the stopped step's own leaf
    committed: `CurationRunBusy` carries its settle's answer, true or null; otherwise it is false
    when this call recorded the step as refused, and null when the call cannot tell, as when a
    settle hold was refused after the leaf ran. The next `resume` settles a null step from its
    witness.
    """
    upcoming = _next_step(plan, before)
    step_id = None if upcoming is None else str(upcoming[1]["step_id"])

    def refusals(run: Mapping[str, Any]) -> int:
        return sum(
            1
            for receipt in run.get("receipts") or ()
            if receipt.get("step_id") == step_id and receipt.get("outcome") == "failed"
        )

    if isinstance(error, CurationRunBusy):
        committed: bool | None = error.leaf_committed
    else:
        committed = False if step_id is not None and refusals(after) > refusals(before) else None
    return {
        "code": getattr(error, "code", None),
        "reason": getattr(error, "reason", None) or getattr(error, "message", None) or str(error),
        "step": step_id,
        "committed": committed,
    }


#: The refusals that say only that another writer held the run or the vault for a moment.
# nosemgrep: ep-word-set -- the two busy codes this module and the mutation lock define.
_BUSY_CODES: Final[frozenset[str]] = frozenset({"CURATION_RUN_BUSY", "MUTATION_BUSY"})


def _stopped_phase(error: Exception, run: Mapping[str, Any]) -> dict[str, Any]:
    """The run's phase as a call that a refusal stopped after committed steps reports it.

    A busy refusal leaves the run `executing`, and `resume` continues it. Any other refusal is
    `partial`, or `blocked` when the run's evidence cannot say what the step did: the run's own
    phase when its receipts already say so, else `partial`, whose `resume` asks the step again.
    A run that another call finished or blocked keeps that phase.
    """
    phase = run.get("phase")
    # Two phases of this module's run state machine that no later step of the run changes.
    if phase in {"completed", "blocked"}:
        return dict(run)
    if getattr(error, "code", None) in _BUSY_CODES:
        return {**run, "phase": "executing", "next_action": "resume"}
    if phase == "partial":
        return dict(run)
    return {**run, "phase": "partial", "next_action": "resume"}


def _advance(
    vault_root: Path,
    run_identity: str,
    plan_identity: str,
    *,
    store: CurationStore,
    compensation: bool,
) -> dict[str, Any]:
    """Run a run's steps until it ends or this call's budget is spent, under the caller's run claim.

    Each step is one `_execute_next`, which takes the vault boundary at that step's own seams, so
    per-step crash safety, witnesses and receipts are those of a single step. Each step's derived
    fan-out runs when the step ends, as it would at the end of a call, unless the caller already
    holds the boundary (the episode workflow, a revert): there every step joins that one hold, and
    the fan-out runs once at its end, so draining it per step would only lengthen the hold.
    Between steps the call yields to waiting writers. It stops starting steps once what is left
    of its budget would not cover its slowest step so far and the delivery reserve, and answers
    `executing` with its progress; the next `resume` continues. A refusal of the call's first step
    raises as before. After this call committed steps, every refusal stops the call and is
    reported beside them (`_stopped`), because those steps are durable; it never raises
    (`_stopped_phase`).
    """
    import time

    from . import request_budget, writer_lease
    from .cli_ops import OpError

    budget = _call_budget()
    manager = writer_lease.active_manager()
    outer_hold = manager.holds_mutation_boundary(vault_root)
    ran = 0
    slowest = 0.0
    stop: Exception | None = None
    while True:
        started = time.monotonic()
        try:
            # The next step reads the corpus that this step's fan-out tells the registries about.
            with nullcontext() if outer_hold else writer_lease.fanout_per_commit():
                result = _execute_next(
                    vault_root, run_identity, plan_identity, store=store, compensation=compensation
                )
        except (CurationError, OpError) as error:
            # A refusal, or a busy vault at a self-guarded leaf's own guard or a step's settle hold.
            if ran == 0:
                raise
            stop = error
            break
        slowest = max(slowest, time.monotonic() - started)
        if not result.get("mutated"):
            break
        ran += 1
        if result.get("phase") not in _OPEN_RUN_PHASES:
            break
        reserve = slowest + request_budget.DELIVERY_RESERVE_SECONDS
        if not budget.can_afford(reserve):
            break
        manager.yield_to_waiting_writes(
            vault_root, max_wait=max(0.0, min(30.0, budget.remaining() - reserve))
        )
        if not budget.can_afford(reserve):
            break
    plan = store.load_plan(run_identity)
    if ran and result.get("phase") not in _OPEN_RUN_PHASES and any(
        step.get("kind") in _RENAME_STEP_KINDS for step in plan["steps"]
    ):
        # The run renamed pages and ended, so the vault-wide name-drift row counts a set that
        # changed.
        from . import due_state

        due_state.schedule_name_drift_refresh(vault_root)
    if stop is not None:
        # The stopped step's receipt, if any, changed the run's phase; the steps before it stay committed.
        run = store.reconstruct(run_identity)
        result = {
            **_compensation_phase(_stopped_phase(stop, run), compensation=compensation),
            "mutated": True,
            "stopped": _stopped(stop, result, run, plan),
        }
    progress = {
        "steps": len(plan["steps"]),
        "committed": len(result.get("committed_steps") or ()),
        "this_call": ran,
    }
    if plan.get("derived_by") is not None:
        # A derived run may hold hundreds of steps: the answer carries counts, and `status` the rest.
        # The two keys are the run's per-step lists that `_execute_next` answers with.
        result = {
            key: value for key, value in result.items() if key not in {"receipts", "committed_steps"}
        }
        if isinstance(result.get("step"), Mapping):
            result["step"] = {
                key: value for key, value in result["step"].items() if key != "leaf_result"
            }
    return {**result, "progress": progress}


def apply(
    vault_root: Path,
    *,
    run_id: str,
    plan_id: str,
    expected_plan_fingerprint: str,
    why: str,
) -> dict[str, Any]:
    root = Path(vault_root)
    with _run_claim(root, run_id):
        store = CurationStore(root)
        # The approval lands only on the exact plan whose bindings still hold, decided under one hold.
        with _step_boundary(root, "curation_approve"):
            plan = store.load_plan(run_id)
            current_id, current_fingerprint = store.identities(run_id)
            if plan_id != current_id or expected_plan_fingerprint != current_fingerprint:
                raise _error("CURATION_PLAN_IDENTITY_MISMATCH", "apply identity does not match stored plan")
            _require_string(why, "approval.why", max_chars=MAX_WHY_CHARS)
            _require_plan_relocation_history(root, plan)
            blockers = _blockers_for_uncommitted(root, plan, set())
            if blockers:
                raise _error(blockers[0]["code"], blockers[0]["reason"])
            store.create_approval(
                run_id,
                plan_id=plan_id,
                fingerprint=expected_plan_fingerprint,
                why=why,
            )
        return _advance(root, run_id, plan_id, store=store, compensation=False)


def resume(vault_root: Path, *, run_id: str, plan_id: str) -> dict[str, Any]:
    root = Path(vault_root)
    with _run_claim(root, run_id):
        store = CurationStore(root)
        forward_identity, _ = store.identities(run_id)
        compensation = plan_id != forward_identity
        active_store: CurationStore = (
            CompensationStore(root, run_id, plan_id) if compensation else store
        )
        _require_plan_relocation_history(root, active_store.load_plan(run_id))
        if not active_store.approval_path(run_id).exists():
            raise _error("CURATION_APPROVAL_REQUIRED", "resume requires an approved immutable plan")
        approval = active_store.validated_approval(run_id, required=True)
        assert approval is not None
        if approval.get("plan_id") != plan_id:
            raise _error("CURATION_PLAN_IDENTITY_MISMATCH", "resume cannot switch plans")
        return _advance(root, run_id, plan_id, store=active_store, compensation=compensation)


def status(vault_root: Path, *, run_id: str) -> dict[str, Any]:
    """Inspect recovery state without writing or repairing any artifact."""
    root = Path(vault_root)
    forward_store = CurationStore(root)
    forward_store.load_plan(run_id)
    forward_store.identities(run_id)
    store: CurationStore = forward_store
    compensation = False
    compensation_root = forward_store.compensation_root(run_id)
    candidates = [
        record.relative_path
        for record in forward_store._artifact_records(compensation_root, recursive=False)
        if record.identity.kind == "directory"
        and _HEX64.fullmatch(record.relative_path)
    ]
    if candidates:
        if len(candidates) > 1:
            return {
                "action": "status",
                "mutated": False,
                "run_id": run_id,
                "phase": "blocked",
                "error_code": "CURATION_OUTCOME_UNCERTAIN",
                "recovery": "blocked",
            }
        store = CompensationStore(root, run_id, candidates[0])
        compensation = True
    plan = store.load_plan(run_id)
    identity, _fingerprint = store.identities(run_id)
    reconstructed = store.reconstruct(run_id)
    projection: Mapping[str, Any] = {}
    if store.state_path(run_id).exists():
        try:
            raw = store._read_json(store.state_path(run_id))
            projection = raw if isinstance(raw, Mapping) else {}
        except CurationError:
            projection = {}
    if projection.get("phase") == "blocked":
        return {
            **reconstructed,
            "action": "status",
            "mutated": False,
            "phase": "blocked",
            "error_code": str(projection.get("error_code") or "CURATION_OUTCOME_UNCERTAIN"),
            "recovery": "blocked",
        }
    settled = _compensation_phase(
        {
            **reconstructed,
            "action": "status",
            "mutated": False,
            "recovery": "blocked" if reconstructed["phase"] == "blocked" else None,
        },
        compensation=compensation,
    )
    next_item = _next_step(plan, reconstructed)
    refused = next_item is not None and next_item[3]
    # nosemgrep: ep-word-membership -- the closed run phases this module defines, each decided by its own branch.
    if reconstructed["phase"] in {"completed", "partial", "failed", "blocked"} and not refused:
        return settled
    if next_item is not None:
        ordinal, step, binding, _refused = next_item
        operation_identity = operation_id(identity, ordinal, step["step_id"])
        try:
            committed_witness = _validated_operation_witness(
                store,
                run_id,
                plan_identity=identity,
                ordinal=ordinal,
                step=step,
                binding=binding,
                operation_identity=operation_identity,
                compensation=compensation,
                live=step["kind"] not in _NATIVE_STEP_KINDS,
            )
        except CurationError:
            return {
                **reconstructed,
                "action": "status",
                "mutated": False,
                "phase": "blocked",
                "error_code": "CURATION_OUTCOME_UNCERTAIN",
                "recovery": "blocked",
            }
        if committed_witness is not None:
            result = {
                **reconstructed,
                "action": "status",
                "mutated": False,
                "phase": "executing",
                "recovery": "receipt-required",
                "active_step": step["step_id"],
                "next_action": "resume",
            }
            return _compensation_phase(result, compensation=compensation)
    if refused:
        return settled
    prepared = projection.get("prepared")
    if isinstance(prepared, Mapping):
        ordinal = prepared.get("ordinal")
        if type(ordinal) is int and 0 <= ordinal < len(plan["steps"]):
            step = plan["steps"][ordinal]
            binding = plan["binding_manifest"][ordinal]
            operation_identity = operation_id(identity, ordinal, step["step_id"])
            recovery = "retry-uncommitted"
            if step["kind"] in _RENAME_STEP_KINDS:
                # A move past its intent record resumes by its placement (`_recover_rename`).
                try:
                    intent = store.rename_intent(run_id, operation_identity)
                    placement = _rename_placement(root, binding) if intent is not None else None
                    blocked_code = (
                        "CURATION_RENAME_HISTORY_UNPROVABLE"
                        if placement == _UNPROVABLE
                        else "CURATION_OUTCOME_UNCERTAIN"
                    )
                except CurationError as error:
                    placement, blocked_code = _UNCERTAIN, error.code
                if placement == _ADOPT:
                    recovery = "roll-forward-required"
                elif placement in (_UNPROVABLE, _UNCERTAIN):
                    return {
                        **reconstructed,
                        "action": "status",
                        "mutated": False,
                        "phase": "blocked",
                        "error_code": blocked_code,
                        "recovery": "blocked",
                    }
            result = {
                **reconstructed,
                "action": "status",
                "mutated": False,
                "phase": "executing",
                "recovery": recovery,
                "active_step": step["step_id"],
            }
            return _compensation_phase(result, compensation=compensation)
    result = {
        **reconstructed,
        "action": "status",
        "mutated": False,
        "recovery": None,
        **(
            {"active_step": next_item[1]["step_id"]}
            if next_item is not None
            else {}
        ),
    }
    return _compensation_phase(result, compensation=compensation)


def valid_replay_result(value: Any) -> bool:
    """Recognize only a durable, terminal curation no-op replay."""
    return (
        isinstance(value, Mapping)
        and value.get("outcome") == "replayed"
        and value.get("mutated") is False
        and value.get("phase") in {"completed", "compensated"}
        and isinstance(value.get("run_id"), str)
        and isinstance(value.get("plan_id"), str)
        and _HEX64.fullmatch(str(value["plan_id"])) is not None
    )


class CurationStore:
    """Canonical run store under the governed vault with create-only evidence."""

    def __init__(self, vault_root: Path):
        self.vault_root = Path(vault_root)
        self.root = managed_root(self.vault_root) / "_Governance" / "curation" / "runs"

    def _assert_safe(self, target: Path) -> None:
        try:
            relative = target.absolute().relative_to(self.vault_root.absolute())
        except ValueError as error:
            raise _error("CURATION_PATH_UNSAFE", "curation artifact escapes the vault") from error
        current = self.vault_root.absolute()
        for part in relative.parts:
            current = current / part
            try:
                if current.is_symlink():
                    raise _error("CURATION_PATH_UNSAFE", "curation artifact crosses a symlink")
            except OSError as error:
                raise _error(
                    "CURATION_PATH_UNSAFE", "curation path could not be inspected"
                ) from error

    def _vault_relative(self, target: Path) -> PurePosixPath:
        try:
            relative = target.absolute().relative_to(self.vault_root.absolute())
        except ValueError as error:
            raise _error("CURATION_PATH_UNSAFE", "curation artifact escapes the vault") from error
        lexical = PurePosixPath(relative.as_posix())
        if not lexical.parts or any(part in {"", ".", ".."} for part in lexical.parts):
            raise _error("CURATION_PATH_UNSAFE", "curation artifact path is invalid")
        return lexical

    @staticmethod
    def _held_error(error: held_fs.HeldFsError | None) -> NoReturn:
        code = error.code if error is not None else "IO_REFUSED"
        if code in {"UNSAFE_PATH", "IDENTITY_CHANGED", "CROSS_DEVICE"}:
            raise _error("CURATION_PATH_UNSAFE", "curation artifact crosses an unsafe path")
        if code == "MISSING":
            raise _error("CURATION_RUN_NOT_FOUND", "curation artifact does not exist")
        raise _error("CURATION_ARTIFACT_CORRUPT", "curation artifact is unreadable")

    def _artifact_records(
        self, directory: Path, *, recursive: bool
    ) -> tuple[held_fs.SagaRecord, ...]:
        """Enumerate one governed directory through retained no-follow handles."""
        self._assert_safe(directory)
        relative = self._vault_relative(directory).as_posix()
        acquired = held_fs.acquire(self.vault_root)
        if not acquired.ok:
            self._held_error(acquired.error)
        with acquired.require() as filesystem:
            opened = filesystem.parent(relative)
            if not opened.ok:
                if opened.error is not None and opened.error.code == "MISSING":
                    return ()
                self._held_error(opened.error)
            with opened.require() as parent:
                observed = (
                    filesystem.enumerate(parent)
                    if recursive
                    else filesystem.children(parent)
                )
                if not observed.ok:
                    self._held_error(observed.error)
                return observed.require()

    def run_dir(self, run_identity: str) -> Path:
        if not re.fullmatch(r"cur-[0-9]{8}-[0-9a-f]{12}", str(run_identity)):
            raise _error("INVALID_RUN_ID", "run id has an invalid shape")
        return self.root / run_identity

    def plan_path(self, run_identity: str) -> Path:
        return self.run_dir(run_identity) / "plan.json"

    def approval_path(self, run_identity: str) -> Path:
        return self.run_dir(run_identity) / "approval.json"

    def identity_path(self, run_identity: str) -> Path:
        return self.run_dir(run_identity) / "identity.json"

    def state_path(self, run_identity: str) -> Path:
        return self.run_dir(run_identity) / "state.json"

    def receipts_dir(self, run_identity: str) -> Path:
        return self.run_dir(run_identity) / "receipts"

    def evidence_dir(self, run_identity: str) -> Path:
        return self.run_dir(run_identity) / "evidence"

    def prepared_dir(self, run_identity: str) -> Path:
        return self.run_dir(run_identity) / "prepared"

    def compensation_root(self, run_identity: str) -> Path:
        return self.run_dir(run_identity) / "compensation"

    def publication_record_path(self, run_identity: str, invocation_id: str) -> Path:
        if not _HEX64.fullmatch(invocation_id):
            raise _error("CURATION_PATH_UNSAFE", "a publication record is named by its invocation id")
        return self.run_dir(run_identity) / "publication" / f"{invocation_id}.json"

    def _read_json(self, path: Path) -> Any:
        try:
            return json.loads(self._read_bytes(path).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
            raise _error("CURATION_ARTIFACT_CORRUPT", "curation artifact is unreadable") from error

    def _read_bytes(self, path: Path) -> bytes:
        """One run artifact's exact bytes, read through retained no-follow handles."""
        self._assert_safe(path)
        relative = self._vault_relative(path)
        parent_relative = PurePosixPath(*relative.parts[:-1]).as_posix()
        acquired = held_fs.acquire(self.vault_root)
        if not acquired.ok:
            self._held_error(acquired.error)
        try:
            with acquired.require() as filesystem:
                opened_parent = filesystem.parent(parent_relative)
                if not opened_parent.ok:
                    self._held_error(opened_parent.error)
                with opened_parent.require() as parent:
                    opened_file = filesystem.file(parent, relative.name)
                    if not opened_file.ok:
                        self._held_error(opened_file.error)
                    with opened_file.require() as file:
                        descriptor = getattr(file, "descriptor", None)
                        if not isinstance(descriptor, int):
                            raise _error(
                                "CURATION_ARTIFACT_CORRUPT",
                                "curation artifact has no retained descriptor",
                            )
                        if os.fstat(descriptor).st_size > MAX_SEALED_BYTES:
                            raise _error(
                                "CURATION_ARTIFACT_CORRUPT",
                                "curation artifact is not a bounded file",
                            )
                        read = filesystem.read(file)
                        if not read.ok:
                            self._held_error(read.error)
                        raw = read.require()
                        if len(raw) > MAX_SEALED_BYTES:
                            raise _error(
                                "CURATION_ARTIFACT_CORRUPT",
                                "curation artifact is not a bounded file",
                            )
                        return raw
        except CurationError:
            raise
        except OSError as error:
            raise _error("CURATION_ARTIFACT_CORRUPT", "curation artifact is unreadable") from error

    def _write_json(
        self, path: Path, value: Any, *, create_only: bool, commit_point: bool = False
    ) -> None:
        """Write one run artifact. Bookkeeping (approval, state, a failed receipt) is no
        commit: a leaf refused after it must answer with its own code, uncommitted. Only
        a plan seal and a receipt for a committed leaf mark the command committed."""
        self._assert_safe(path)
        content = canonical_json(value)
        # Each run artifact is its own seam: it holds the boundary for this one-file batch alone.
        with _step_boundary(self.vault_root, "curation_store_write"):
            try:
                batch_atomic_write(
                    [PlannedWrite(path=path, content=content, create_only=create_only)],
                    vault_root=self.vault_root,
                    commit_point=commit_point,
                )
            except (BatchWriteError, OSError, ValueError) as error:
                raise _write_refusal(error, "curation artifact") from error

    def _create_plan_at(self, path: Path, plan: Mapping[str, Any]) -> None:
        _require_plan_relocation_history(self.vault_root, plan)
        size = len(canonical_json_bytes(plan))
        if size > MAX_SEALED_BYTES:
            # Refused before anything is written: a plan above the read bound would seal and then
            # never load again.
            raise _error(
                "CURATION_PLAN_TOO_LARGE",
                f"the sealed plan would hold {size} bytes, above the {MAX_SEALED_BYTES}-byte "
                "limit; relocate fewer pages per plan",
            )
        identity = plan_id(plan)
        base = {
            key: value
            for key, value in plan.items()
            if key not in {"binding_manifest", "registry_ids"}
        }
        fingerprint = plan_fingerprint(base, plan["binding_manifest"], plan["registry_ids"])
        anchor_path = path.parent / "identity.json"
        anchor = {
            "version": 1,
            "plan_id": identity,
            "plan_fingerprint": fingerprint,
        }
        if path.exists():
            if (
                anchor_path.exists()
                and canonical_json_bytes(self._read_json(path)) == canonical_json_bytes(plan)
                and canonical_json_bytes(self._read_json(anchor_path))
                == canonical_json_bytes(anchor)
            ):
                return
            raise _error("CURATION_PLAN_COLLISION", "immutable plan bytes already differ")
        if anchor_path.exists():
            raise _error("CURATION_PLAN_COLLISION", "immutable plan identity already exists")
        self._assert_safe(path)
        self._assert_safe(anchor_path)
        try:
            batch_atomic_write(
                [
                    PlannedWrite(path=path, content=canonical_json(plan), create_only=True),
                    PlannedWrite(
                        path=anchor_path,
                        content=canonical_json(anchor),
                        create_only=True,
                    ),
                ],
                vault_root=self.vault_root,
            )
        except (BatchWriteError, OSError, ValueError) as error:
            raise _write_refusal(error, "curation plan") from error

    def create_forward(
        self,
        plan: Mapping[str, Any],
        *,
        binding_manifest: Iterable[Mapping[str, Any]],
        registry_ids: Mapping[str, str],
        today: dt.date | None = None,
        allow_records: bool = False,
        derived_by: str | None = None,
        summary: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if derived_by is not None and derived_by not in DERIVED_BY:
            raise _error("INVALID_CURATION_PLAN", "plan derivation is invalid")
        if summary is not None and derived_by is None:
            raise _error("INVALID_CURATION_PLAN", "only a derived plan seals its summary")
        validated = validate_forward_plan(
            plan, allow_records=allow_records, derived=derived_by is not None
        )
        _require_plan_relocation_history(self.vault_root, validated)
        sealed = {
            **validated,
            "binding_manifest": json.loads(canonical_json(list(binding_manifest))),
            "registry_ids": dict(registry_ids),
            **({"derived_by": derived_by} if derived_by is not None else {}),
            **({"approval_summary": json.loads(canonical_json(summary))} if summary is not None else {}),
        }
        if any(isinstance(prepared := item.get("prepared"), Mapping) and isinstance(prepared.get("episode"), Mapping) for item in sealed["binding_manifest"]):
            # This fixed reader version prevents old executors from bypassing journal correlation.
            sealed["episode_invocation_version"] = 1
        identity = plan_id(sealed)
        run_identity = f"cur-{(today or dt.date.today()):%Y%m%d}-{identity[:12]}"
        path = self.plan_path(run_identity)
        self._assert_safe(path)
        self._create_plan_at(path, sealed)
        fingerprint = plan_fingerprint(
            {key: value for key, value in sealed.items() if key not in {"binding_manifest", "registry_ids"}},
            sealed["binding_manifest"], sealed["registry_ids"],
        )
        projection = {
            "version": 1,
            "run_id": run_identity,
            "plan_id": identity,
            "plan_fingerprint": fingerprint,
            "phase": "proposed",
            "active_step": None,
        }
        return {**_sealed_run(self, run_identity, projection), "plan": sealed}

    def load_plan(self, run_identity: str) -> dict[str, Any]:
        plan = _validate_sealed_plan(self._read_json(self.plan_path(run_identity)))
        return plan

    def identities(self, run_identity: str) -> tuple[str, str]:
        stored = self.load_plan(run_identity)
        identity = plan_id(stored)
        base = {
            key: value
            for key, value in stored.items()
            if key not in {"binding_manifest", "registry_ids"}
        }
        fingerprint = plan_fingerprint(base, stored["binding_manifest"], stored["registry_ids"])
        anchor = self._read_json(self.identity_path(run_identity))
        if (
            not isinstance(anchor, Mapping)
            or set(anchor) != {"version", "plan_id", "plan_fingerprint"}
            or anchor.get("version") != 1
            or anchor.get("plan_id") != identity
            or anchor.get("plan_fingerprint") != fingerprint
        ):
            raise _error(
                "CURATION_PLAN_IDENTITY_MISMATCH", "stored plan no longer matches its immutable seal"
            )
        if not isinstance(self, CompensationStore) and not run_identity.endswith(identity[:12]):
            raise _error(
                "CURATION_PLAN_IDENTITY_MISMATCH", "run identity no longer matches stored plan"
            )
        return identity, fingerprint

    def create_approval(
        self,
        run_identity: str,
        *,
        plan_id: str,
        fingerprint: str,
        why: str,
        compensation_plan_id: str | None = None,
    ) -> dict[str, Any]:
        _require_string(why, "approval.why", max_chars=MAX_WHY_CHARS)
        expected_id, expected_fingerprint = self.identities(run_identity)
        if plan_id != expected_id or fingerprint != expected_fingerprint:
            raise _error(
                "CURATION_PLAN_IDENTITY_MISMATCH", "approval does not bind current plan bytes"
            )
        approval = {
            "version": 1,
            "run_id": run_identity,
            "plan_id": plan_id,
            "plan_fingerprint": fingerprint,
            "why": why,
            "approved_at": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
            "compensation_plan_id": compensation_plan_id,
        }
        path = self.approval_path(run_identity)
        if path.exists():
            current = self.validated_approval(run_identity, required=True)
            assert current is not None
            if all(
                current.get(key) == approval[key] for key in ("plan_id", "plan_fingerprint", "why")
            ):
                return current
            raise _error("CURATION_APPROVAL_COLLISION", "a different approval already exists")
        self._write_json(path, approval, create_only=True)
        return approval

    def validated_approval(
        self, run_identity: str, *, required: bool = False
    ) -> dict[str, Any] | None:
        path = self.approval_path(run_identity)
        if not path.exists():
            if required:
                raise _error("CURATION_APPROVAL_REQUIRED", "approved plan evidence is missing")
            return None
        value = self._read_json(path)
        identity, fingerprint = self.identities(run_identity)
        expected_compensation = (
            identity if isinstance(self, CompensationStore) else None
        )
        if (
            not isinstance(value, Mapping)
            or set(value)
            != {
                "version",
                "run_id",
                "plan_id",
                "plan_fingerprint",
                "why",
                "approved_at",
                "compensation_plan_id",
            }
            or value.get("version") != 1
            or value.get("run_id") != run_identity
            or value.get("plan_id") != identity
            or value.get("plan_fingerprint") != fingerprint
            or value.get("compensation_plan_id") != expected_compensation
            or not isinstance(value.get("approved_at"), str)
        ):
            raise _error("CURATION_APPROVAL_INVALID", "approval does not bind the sealed plan")
        _require_string(value.get("why"), "approval.why", max_chars=MAX_WHY_CHARS)
        return dict(value)

    def create_evidence(
        self, run_identity: str, operation_identity: str, evidence: Mapping[str, Any]
    ) -> Path:
        if not _HEX64.fullmatch(operation_identity):
            raise _error("INVALID_OPERATION_ID", "witness operation id is invalid")
        path = self.evidence_dir(run_identity) / f"{operation_identity}.json"
        if path.exists():
            if canonical_json_bytes(self._read_json(path)) == canonical_json_bytes(evidence):
                return path
            raise _error("CURATION_OUTCOME_UNCERTAIN", "a competing witness exists")
        self._write_json(path, dict(evidence), create_only=True)
        return path

    def rename_intent_path(self, run_identity: str, operation_identity: str) -> Path:
        if not _HEX64.fullmatch(operation_identity):
            raise _error("INVALID_OPERATION_ID", "intent operation id is invalid")
        return self.prepared_dir(run_identity) / f"{operation_identity}.json"

    def create_rename_intent(self, run_identity: str, intent: Mapping[str, Any]) -> None:
        """Publish and flush a move's intent record, create-only, before its rename.

        It is the step's own bookkeeping, so it is written outside the step's witness seam and
        marks nothing committed.
        """
        from . import curation_witness

        path = self.rename_intent_path(run_identity, str(intent["operation_id"]))
        self._assert_safe(path)
        try:
            with curation_witness.suspended():
                batch_atomic_write(
                    [PlannedWrite(path=path, content=canonical_json(intent), create_only=True)],
                    vault_root=self.vault_root,
                    post_commit_fanout=False,
                    commit_point=False,
                )
        except (BatchWriteError, OSError, ValueError) as error:
            raise _error(
                "CURATION_OUTCOME_UNCERTAIN", "the move's intent record could not be published"
            ) from error

    def void_rename_intent(self, run_identity: str, operation_identity: str) -> None:
        """Remove a move's intent record once its rename is known not to stand (`_void_unrenamed`)."""
        path = self.rename_intent_path(run_identity, operation_identity)
        self._assert_safe(path)
        path.unlink(missing_ok=True)

    def rename_intent(self, run_identity: str, operation_identity: str) -> dict[str, Any] | None:
        """The move's intent record, or None when the step never reached its rename."""
        path = self.rename_intent_path(run_identity, operation_identity)
        self._assert_safe(path)
        if not path.exists():
            return None
        intent = self._read_json(path)
        if not isinstance(intent, dict) or intent.get("operation_id") != operation_identity:
            raise _error("CURATION_OUTCOME_UNCERTAIN", "the move's intent record is invalid")
        return intent

    def create_publication_record(self, run_identity: str, intent: Mapping[str, Any]) -> Path:
        """Create one native attempt's publication record in its intent form; it lives as long as the run."""
        path = self.publication_record_path(run_identity, str(intent["invocation_id"]))
        self._write_json(path, dict(intent), create_only=True)
        return path

    def mark_publication_started(
        self, path: Path, intent: Mapping[str, Any], started: Mapping[str, Any]
    ) -> None:
        """Replace the exact intent record with its started form, without a commit point or fan-out."""
        self._assert_safe(path)
        try:
            batch_atomic_write(
                [
                    PlannedWrite(
                        path=path,
                        content=canonical_json(started),
                        expected_hash=text_content_hash(canonical_json(intent)),
                    )
                ],
                vault_root=self.vault_root,
                post_commit_fanout=False,
                commit_point=False,
            )
        except (BatchWriteError, ContentHashMismatchError, OSError, ValueError) as error:
            raise _error(
                "CURATION_STORE_WRITE_FAILED", "the publication record could not be marked started"
            ) from error

    def create_receipt(
        self,
        run_identity: str,
        ordinal: int,
        step_id: str,
        receipt: Mapping[str, Any],
    ) -> Path:
        attempt = receipt.get("attempt")
        if type(attempt) is not int or attempt < 1:
            raise _error("CURATION_RECEIPT_INVALID", "receipt attempt must be positive")
        path = self.receipts_dir(run_identity) / f"{ordinal:03d}-{step_id}" / f"{attempt:04d}.json"
        if path.exists():
            if canonical_json_bytes(self._read_json(path)) == canonical_json_bytes(receipt):
                return path
            raise _error("CURATION_OUTCOME_UNCERTAIN", "a competing receipt exists")
        self._write_json(
            path,
            dict(receipt),
            create_only=True,
            commit_point=receipt.get("outcome") in COMMITTED_OUTCOMES,
        )
        return path

    def write_state(self, run_identity: str, state: Mapping[str, Any]) -> None:
        self._write_json(self.state_path(run_identity), dict(state), create_only=False)

    def _receipt_records(self, run_identity: str) -> list[dict[str, Any]]:
        root = self.receipts_dir(run_identity)
        records: list[dict[str, Any]] = []
        paths: list[Path] = []
        for record in self._artifact_records(root, recursive=True):
            relative = PurePosixPath(record.relative_path)
            if (
                record.identity.kind == "file"
                and len(relative.parts) == 2
                and re.fullmatch(
                    r"[0-9]{3}-[A-Za-z0-9][A-Za-z0-9._-]{0,79}", relative.parts[0]
                )
                and re.fullmatch(r"[0-9]{4}\.json", relative.parts[1])
            ):
                paths.append(root / Path(*relative.parts))
        for path in sorted(paths):
            value = self._read_json(path)
            if not isinstance(value, Mapping):
                raise _error("CURATION_ARTIFACT_CORRUPT", "receipt is not an object")
            records.append(dict(value))
        return records

    def reconstruct(self, run_identity: str) -> dict[str, Any]:
        plan = self.load_plan(run_identity)
        identity, fingerprint = self.identities(run_identity)
        steps = plan["steps"]
        try:
            approval = self.validated_approval(run_identity)
        except CurationError:
            return {
                "run_id": run_identity,
                "plan_id": identity,
                "plan_fingerprint": fingerprint,
                "phase": "blocked",
                "error_code": "CURATION_OUTCOME_UNCERTAIN",
                "committed_steps": [],
                "receipts": [],
                "next_action": None,
            }
        receipts = self._receipt_records(run_identity)
        committed: dict[str, dict[str, Any]] = {}
        failed: dict[str, dict[str, Any]] = {}
        for receipt in receipts:
            ordinal = receipt.get("ordinal")
            if type(ordinal) is not int or not 0 <= ordinal < len(steps):
                return self._blocked_reconstruction(run_identity, identity, fingerprint, receipts)
            step = steps[ordinal]
            step_identity = str(receipt.get("step_id") or "")
            expected_operation = operation_id(identity, ordinal, step["step_id"])
            outcome = receipt.get("outcome")
            common_valid = bool(
                receipt.get("version") == 1
                and type(receipt.get("attempt")) is int
                and receipt["attempt"] >= 1
                and step_identity == step["step_id"]
                and receipt.get("operation_id") == expected_operation
            )
            if not common_valid:
                return self._blocked_reconstruction(run_identity, identity, fingerprint, receipts)
            if outcome in COMMITTED_OUTCOMES:
                if approval is None or set(receipt) != {
                    "version",
                    "attempt",
                    "ordinal",
                    "step_id",
                    "operation_id",
                    "outcome",
                    "result_digest",
                    "effect",
                }:
                    return self._blocked_reconstruction(
                        run_identity, identity, fingerprint, receipts
                    )
                try:
                    binding = plan["binding_manifest"][ordinal]
                    witness = _validate_witness(
                        self._read_json(
                            self.evidence_dir(run_identity) / f"{expected_operation}.json"
                        ),
                        run_identity=run_identity,
                        plan_identity=identity,
                        ordinal=ordinal,
                        step=step,
                        operation_identity=expected_operation,
                        binding=binding,
                        parent_compensation_plan_id=(
                            identity if isinstance(self, CompensationStore) else None
                        ),
                    )
                except (CurationError, IndexError, KeyError, TypeError):
                    return self._blocked_reconstruction(
                        run_identity, identity, fingerprint, receipts
                    )
                effect = receipt.get("effect")
                if (
                    receipt.get("result_digest") != witness["result_digest"]
                    or not isinstance(effect, Mapping)
                    or effect.get("kind") != step["kind"]
                ):
                    return self._blocked_reconstruction(
                        run_identity, identity, fingerprint, receipts
                    )
                previous = committed.get(step_identity)
                if previous is not None and canonical_json(previous) != canonical_json(receipt):
                    return {
                        "run_id": run_identity,
                        "plan_id": identity,
                        "plan_fingerprint": fingerprint,
                        "phase": "blocked",
                        "error_code": "CURATION_OUTCOME_UNCERTAIN",
                        "committed_steps": sorted(committed),
                        "next_action": None,
                    }
                committed[step_identity] = receipt
            elif outcome == "definite-no-effect":
                if approval is None or not _valid_no_effect(
                    receipt, run=run_identity, plan_identity=identity, step=step,
                    binding=plan["binding_manifest"][ordinal], operation=expected_operation,
                ):
                    return self._blocked_reconstruction(run_identity, identity, fingerprint, receipts)
                # A typed zero-publication terminal (native, or curation's recovery proof) permits the same operation to retry.
                failed[step_identity] = {**receipt, "retryable": True}
            elif outcome == "failed":
                if set(receipt) != {
                    "version",
                    "attempt",
                    "ordinal",
                    "step_id",
                    "operation_id",
                    "outcome",
                    "error_code",
                    "retryable",
                } or not isinstance(receipt.get("error_code"), str) or type(
                    receipt.get("retryable")
                ) is not bool:
                    return self._blocked_reconstruction(
                        run_identity, identity, fingerprint, receipts
                    )
                failed[step_identity] = receipt
            else:
                return self._blocked_reconstruction(run_identity, identity, fingerprint, receipts)
        ordered_committed = [step["step_id"] for step in steps if step["step_id"] in committed]
        failed_step: str | None = None
        retryable = False
        for step in steps:
            if step["step_id"] in failed and step["step_id"] not in committed:
                failed_step = step["step_id"]
                retryable = bool(failed[failed_step].get("retryable", False))
                break
        if len(ordered_committed) == len(steps):
            phase = "completed"
            next_action = None
        elif failed_step is not None:
            # A failed attempt that a later retry committed no longer holds the run back.
            phase = "partial" if committed else "failed"
            next_action = "resume" if retryable else ("propose-compensation" if committed else None)
        elif approval is None:
            phase = "proposed"
            next_action = "apply"
        else:
            phase = "approved" if not committed else "executing"
            next_action = "resume"
        return {
            "run_id": run_identity,
            "plan_id": identity,
            "plan_fingerprint": fingerprint,
            "phase": phase,
            "committed_steps": ordered_committed,
            "failed_step": failed_step,
            "retryable": retryable,
            "receipts": receipts,
            "next_action": next_action,
        }

    @staticmethod
    def _blocked_reconstruction(
        run_identity: str,
        identity: str,
        fingerprint: str,
        receipts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "run_id": run_identity,
            "plan_id": identity,
            "plan_fingerprint": fingerprint,
            "phase": "blocked",
            "error_code": "CURATION_OUTCOME_UNCERTAIN",
            "committed_steps": [],
            "failed_step": None,
            "retryable": False,
            "receipts": receipts,
            "next_action": None,
        }


def _sealed_run(
    store: CurationStore, run_identity: str, projection: dict[str, Any]
) -> dict[str, Any]:
    """The run a sealed plan names: new and proposed, or the one that plan already has.

    A plan's identity names its run, so sealing the same plan again finds that run, which may
    have started, failed or finished since. It answers in the run's own phase and next action.
    """
    if not store.state_path(run_identity).exists():
        store._write_json(store.state_path(run_identity), projection, create_only=True)
        return projection
    state = store.reconstruct(run_identity)
    if state["phase"] == "proposed":
        return projection
    return {
        **projection,
        **{
            key: state[key]
            for key in (
                "phase", "committed_steps", "failed_step", "retryable", "error_code", "next_action"
            )
            if key in state
        },
    }


class CompensationStore(CurationStore):
    """Artifact view for one immutable compensation plan inside a forward run."""

    def __init__(self, vault_root: Path, run_identity: str, compensation_plan_id: str):
        super().__init__(vault_root)
        if not _HEX64.fullmatch(str(compensation_plan_id)):
            raise _error("INVALID_PLAN_ID", "compensation plan id is invalid")
        self.forward_run_id = run_identity
        self.compensation_plan_id = compensation_plan_id
        self.base = super().compensation_root(run_identity) / compensation_plan_id

    def plan_path(self, _run_identity: str) -> Path:
        return self.base / "plan.json"

    def approval_path(self, _run_identity: str) -> Path:
        return self.base / "approval.json"

    def identity_path(self, _run_identity: str) -> Path:
        return self.base / "identity.json"

    def state_path(self, _run_identity: str) -> Path:
        return self.base / "state.json"

    def receipts_dir(self, _run_identity: str) -> Path:
        return self.base / "receipts"

    def evidence_dir(self, _run_identity: str) -> Path:
        return self.base / "evidence"

    def prepared_dir(self, _run_identity: str) -> Path:
        return self.base / "prepared"

    def load_plan(self, run_identity: str) -> dict[str, Any]:
        stored = super().load_plan(run_identity)
        if stored.get("plan_type") != "compensation":
            raise _error("CURATION_PLAN_CORRUPT", "compensation plan type is invalid")
        if plan_id(stored) != self.compensation_plan_id:
            raise _error("CURATION_PLAN_IDENTITY_MISMATCH", "compensation plan bytes changed")
        return stored


def compensation_descriptors(
    forward_steps: Iterable[Mapping[str, Any]],
) -> list[dict[str, str]]:
    return [
        {
            "forward_step_id": str(step["step_id"]),
            "kind": compensation_kind(str(step["kind"])),
        }
        for step in reversed(list(forward_steps))
    ]


def _compensation_supersession_args(
    *, current_path: str, preimage: str, step_id: str
) -> dict[str, Any]:
    from . import note as note_module
    from .vault import parse_frontmatter

    frontmatter, body, _raw = parse_frontmatter(preimage, strict=True)
    note_type = str(frontmatter.get("type") or "")
    title = str(frontmatter.get("title") or "").strip()
    # `replace_memory` replays through the typed writer, which places only its own types.
    if note_type not in note_module.NOTE_TYPES or not title:
        raise _error(
            "CURATION_COMPENSATION_UNAVAILABLE",
            "sealed preimage cannot be restored through replace_memory",
        )
    args: dict[str, Any] = {
        "old_path": current_path,
        "content": body,
        "title": title,
        "slug": f"compensate-{hashlib.sha256(step_id.encode()).hexdigest()[:16]}",
        "note_type": note_type,
        "reason": f"History-preserving compensation for {step_id}.",
    }
    for key in (
        "project",
        "projects",
        "sources",
        "tags",
        "severity",
        "pattern_type",
        "domain",
        "started",
        "duration",
        "hypothesis",
        "n",
        "concluded",
        "medium",
        "recorded",
        "published",
        "host",
        "editor",
        "bridge_of",
        "bridge_scope",
        "bridge_review",
    ):
        if frontmatter.get(key) is not None:
            args[key] = frontmatter[key]
    return args


def _compensation_args(
    forward_step: Mapping[str, Any],
    binding: Mapping[str, Any],
    receipt: Mapping[str, Any],
    operation_identity: str,
) -> tuple[str, dict[str, Any]] | None:
    """The inverse step of one committed forward step, or None when it changed nothing to undo.

    `operation_identity` is the forward step's operation id, under which its pointer writes
    were recorded.
    """
    kind = str(forward_step["kind"])
    inverse = compensation_kind(kind, binding)
    if inverse == "unavailable":
        raise _error(
            "CURATION_COMPENSATION_UNAVAILABLE",
            "an undone relocation is redone by a new reviewed plan",
        )
    if kind in RECORDS_STEP_KINDS:
        raise _error(
            "CURATION_COMPENSATION_UNAVAILABLE",
            "a Records transition is history; a later Records transition corrects it",
        )
    if kind == OBSERVE_UNIT_STEP_KIND:
        raise _error(
            "CURATION_COMPENSATION_UNAVAILABLE",
            "a prior unit interpretation remains history; a later guarded update corrects it",
        )
    if kind == REMOVE_RELATION_STEP_KIND:
        raise _error(
            "CURATION_COMPENSATION_UNAVAILABLE",
            "a removed edge is authored again as a new write, which is a new promotion",
        )
    if kind in {FOLD_RECAP_STEP_KIND, FOLD_DUPLICATE_STEP_KIND}:
        # The undo is derived from the sealed manifest alone: every identity it needs was sealed.
        return inverse, {"fold": binding["prepared"]}
    effect = dict(receipt.get("effect") or {})
    if kind in {"create-note", "create-entity"}:
        path = str(effect.get("path") or binding["postcondition"]["path"])
        return inverse, {"path": path, "confirm": True}
    if kind == "delete":
        trash_path = effect.get("trash_path")
        if not isinstance(trash_path, str):
            raise _error(
                "CURATION_COMPENSATION_UNAVAILABLE",
                "delete receipt does not seal its exact trash identity",
            )
        return inverse, {"trash_path": trash_path, "restore_path": binding["path"]}
    if kind == "move":
        # The inverse renames the page back and lands its sealed pre-step bytes, so its lineage
        # entry and `fields` patch go with no new entry.
        return inverse, {
            "move": {
                "old_path": binding["postcondition"]["path"],
                "new_path": binding["path"],
                "before_hash": binding["postcondition"]["content_hash"],
                "after_hash": binding["before_hash"],
                "restore": binding["prepared"]["restore"],
                "allow_curated": bool(forward_step["args"].get("allow_curated", False)),
            }
        }
    if kind == REPOINT_STEP_KIND:
        changed = [
            row for row in binding["prepared"]["pages"] if row["after_hash"] != row["before_hash"]
        ]
        if not changed:
            return None
        return inverse, {"repoint": {"operation": operation_identity, "pages": changed}}
    if inverse == REVERT_POINTER_STEP_KIND:
        restore = binding["prepared"].get("restore")
        return inverse, {
            "pointer": {
                "operation": operation_identity,
                "pages": [
                    {
                        "path": binding["path"],
                        "before_hash": binding["before_hash"],
                        "after_hash": binding["postcondition"]["content_hash"],
                        # A writable page reverses its sealed line delta; an append-only page
                        # reverts through the pointer write's record (`repoint_links.restorations`).
                        **({"restore": restore} if restore is not None else {"append_only": True}),
                    }
                ],
            }
        }
    if kind == "recover":
        path = effect.get("restored_path") or binding.get("prepared", {}).get("destination")
        if not isinstance(path, str):
            raise _error(
                "CURATION_COMPENSATION_UNAVAILABLE",
                "recovery receipt does not seal its restored path",
            )
        return inverse, {"path": path, "confirm": True}
    current_path = str(
        effect.get("new_path") or effect.get("path") or binding["postcondition"]["path"]
    )
    preimage = binding.get("preimage")
    if not isinstance(preimage, str):
        raise _error(
            "CURATION_COMPENSATION_UNAVAILABLE", "authored-content step lacks a sealed preimage"
        )
    return inverse, _compensation_supersession_args(
        current_path=current_path,
        preimage=preimage,
        step_id=str(forward_step["step_id"]),
    )


def _later_states(
    plan: Mapping[str, Any], committed: Collection[str], ordinal: int
) -> dict[str, frozenset[str]]:
    """The hashes that later committed steps, chained to a page, leave it at (`_prepare_manifest`)."""
    states: dict[str, set[str]] = {}
    for later in range(ordinal + 1, len(plan["steps"])):
        binding = plan["binding_manifest"][later]
        if plan["steps"][later]["step_id"] not in committed or not binding.get("chained"):
            continue
        for row in binding["effect_after"]:
            if row["path"] in binding["chained"] and row.get("content_hash"):
                states.setdefault(str(row["path"]), set()).add(str(row["content_hash"]))
    return {path: frozenset(hashes) for path, hashes in states.items()}


def propose_compensation(vault_root: Path, *, run_id: str) -> dict[str, Any]:
    """Derive and seal the plan that undoes a forward run's committed steps.

    Like a forward proposal, it prepares without the vault mutation boundary (the dispatcher
    narrows it to the writer fence), then takes the boundary to seal. The seal refuses a plan
    whose forward run committed another step, or whose bound pages or registries changed, since
    preparation. Under an outer boundary it takes no second guard (`seal_proposal`).

    It holds the run's claim throughout: a forward step that is between its leaf's commit and its
    receipt would otherwise be missing from the undo.
    """
    with _run_claim(Path(vault_root), run_id):
        return _propose_compensation(Path(vault_root), run_id=run_id)


def _propose_compensation(root: Path, *, run_id: str) -> dict[str, Any]:
    forward_store = CurationStore(root)
    forward_plan = forward_store.load_plan(run_id)
    forward_identity, _forward_fingerprint = forward_store.identities(run_id)
    state = forward_store.reconstruct(run_id)
    committed = _committed_step_ids(state)
    if not committed:
        raise _error(
            "CURATION_COMPENSATION_UNAVAILABLE", "no committed forward effect can be compensated"
        )
    receipts = {
        str(receipt.get("step_id")): receipt
        for receipt in state.get("receipts", [])
        if receipt.get("outcome") in COMMITTED_OUTCOMES
    }
    steps: list[dict[str, Any]] = []
    for ordinal in reversed(range(len(forward_plan["steps"]))):
        forward_step = forward_plan["steps"][ordinal]
        if forward_step["step_id"] not in committed:
            continue
        binding = forward_plan["binding_manifest"][ordinal]
        receipt = receipts.get(forward_step["step_id"])
        if receipt is None:
            raise _error("CURATION_OUTCOME_UNCERTAIN", "committed step receipt is missing")
        operation_identity = operation_id(
            forward_identity, ordinal, forward_step["step_id"]
        )
        witness = _validate_witness(
            forward_store._read_json(
                forward_store.evidence_dir(run_id) / f"{operation_identity}.json"
            ),
            run_identity=run_id,
            plan_identity=forward_identity,
            ordinal=ordinal,
            step=forward_step,
            operation_identity=operation_identity,
            binding=binding,
        )
        if receipt.get("result_digest") != witness["result_digest"]:
            raise _error(
                "CURATION_OUTCOME_UNCERTAIN", "forward receipt no longer matches its witness"
            )
        _verify_live_postcondition(
            root,
            witness,
            accepted={
                **_later_states(forward_plan, committed, ordinal),
                **(_chained_states(forward_step, binding) or {}),
            },
            drift="CURATION_BINDING_STALE",
        )
        derived = _compensation_args(forward_step, binding, receipt, operation_identity)
        if derived is None:
            continue
        inverse, args = derived
        steps.append(
            {
                "step_id": f"comp-{len(steps):03d}-{forward_step['step_id']}",
                "kind": inverse,
                "args": args,
            }
        )
    derived_by = forward_plan.get("derived_by")
    plan = validate_forward_plan(
        {
            "version": 1,
            "title": f"Compensation for {run_id}",
            "steps": steps,
        },
        # Only this derivation may carry the private inverse of a fold.
        allow_fold_inverse=True,
        # The undo of a derived plan has one inverse step per committed forward step.
        derived=derived_by is not None,
    )
    manifest = _prepare_manifest(root, plan["steps"])
    _require_plan_relocation_history(root, plan)
    registries = registry_identities(root)
    sealed = {
        **plan,
        "plan_type": "compensation",
        "forward_plan_id": forward_identity,
        "binding_manifest": manifest,
        "registry_ids": registries,
        **({"derived_by": derived_by} if derived_by is not None else {}),
    }
    identity = plan_id(sealed)
    store = CompensationStore(root, run_id, identity)
    fingerprint = plan_fingerprint(
        {
            key: value
            for key, value in sealed.items()
            if key not in {"binding_manifest", "registry_ids"}
        },
        manifest,
        registries,
    )
    projection = {
        "version": 1,
        "run_id": run_id,
        "plan_id": identity,
        "plan_fingerprint": fingerprint,
        "forward_plan_id": forward_identity,
        "phase": "proposed",
        "active_step": None,
    }
    with _step_boundary(root, "curation_compensation_propose_commit"):
        if _committed_step_ids(forward_store.reconstruct(run_id)) != committed:
            raise _error(
                "CURATION_BINDING_STALE",
                "the forward run committed another step while its undo was prepared; propose again",
            )
        blockers = _binding_blockers(root, sealed, plan_registry_identities(root, sealed))
        if blockers:
            raise _error(blockers[0]["code"], blockers[0]["reason"])
        store._create_plan_at(store.plan_path(run_id), sealed)
        run = _sealed_run(store, run_id, projection)
    if derived_by is not None:
        # The undo of a derived run is as large as the run: the person approves its summary.
        return {**run, "summary": derived_summary(sealed)}
    return {**run, "plan": sealed}


def apply_compensation(
    vault_root: Path,
    *,
    run_id: str,
    plan_id: str,
    expected_plan_fingerprint: str,
    why: str,
) -> dict[str, Any]:
    root = Path(vault_root)
    with _run_claim(root, run_id):
        store = CompensationStore(root, run_id, plan_id)
        # As in `apply`: the approval lands only on the exact plan whose bindings hold, under one hold.
        with _step_boundary(root, "curation_approve"):
            plan = store.load_plan(run_id)
            identity, fingerprint = store.identities(run_id)
            if identity != plan_id or fingerprint != expected_plan_fingerprint:
                raise _error("CURATION_PLAN_IDENTITY_MISMATCH", "compensation approval identity is stale")
            _require_string(why, "approval.why", max_chars=MAX_WHY_CHARS)
            _require_plan_relocation_history(root, plan)
            blockers = _blockers_for_uncommitted(root, plan, set())
            if blockers:
                raise _error(blockers[0]["code"], blockers[0]["reason"])
            store.create_approval(
                run_id,
                plan_id=plan_id,
                fingerprint=expected_plan_fingerprint,
                why=why,
                compensation_plan_id=plan_id,
            )
        return _advance(root, run_id, plan_id, store=store, compensation=True)


__all__ = [
    "CURATION_ACTIONS",
    "READ_ONLY_ACTIONS",
    "CONTINUING_ACTIONS",
    "STEP_KINDS",
    "MAX_PLAN_BYTES",
    "MAX_SEALED_BYTES",
    "CurationError",
    "CurationStore",
    "canonical_json",
    "canonical_json_bytes",
    "normalize_target_path",
    "operation_id",
    "plan_fingerprint",
    "valid_replay_result",
    "plan_id",
    "run_id",
    "validate_forward_plan",
]
