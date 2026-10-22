"""The `episode_memory` operation: record a bounded recap, bind it, inspect it.

`record` is the only way a recap reaches the vault. The agent authors it; this
module validates it (`episode_capture`), writes it through the ordinary Source
writer as an `episode` Source, retires the episode's earlier live revision in
the same batch, and binds the committed page to the caller's own episode ledger
by the writer's receipt. `inspect` reads that ledger back.

What it never does: summarise anything itself, walk the corpus for the page it
just wrote, accept curation leaves or proposals, or answer a committed write
with an error because the recorder may not read the page back.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import add as add_module
from . import (
    call_spans,
    curation,
    episode_capture,
    episode_nudge,
    freshness,
    memory_refs,
    query_log,
    request_budget,
    source_taxonomy,
    working_set_heat,
    writer_lease,
)
from .cli_ops import OpError
from .episode_model import EpisodeError
from .episode_recovery import EpisodeInputOwner
from .governance import egress
from .governance.principal import effective_principal
from .vault import (
    BatchWriteError,
    ContentHashMismatchError,
    content_hash,
    kb_root,
    parse_frontmatter,
    read_guarded_text,
)

log = logging.getLogger(__name__)

LEDGER_FULL_WARNING = (
    "This episode's ledger is full, so this recap committed as a Source but is not bound to it, "
    "and a retry may not bind it. Later recaps keep committing as Sources. "
    "Record the next recap without `episode` to start a new key."
)

# These fixed counters describe ordered proof branches, never source content.
_PREPARATION_ATTEMPTS = "preparation_attempts"
_COMMIT_CHANGED = "commit_changed"
_EVENT_PROOF_CHANGED = "event_proof_changed"
_SNAPSHOT_CHANGED = "snapshot_changed"


@dataclass(frozen=True)
class _ReferencePreparation:
    owners: memory_refs._ReferenceOwners
    generation: int
    checkpoint: freshness.FreshnessCheckpoint
    live: bool
    external_pending_epoch: int | None

    def current(self, root: Path, *, diagnostics: dict[str, int]) -> bool:
        # A non-live checkpoint seals a fresh scan; it never proves an empty
        # or current cached corpus. Only borrowed snapshots require live proof.
        # Count only the first failed group, preserving the original read order.
        if writer_lease.read_commit_generation(root) != self.generation:
            diagnostics[_COMMIT_CHANGED] += 1
            return False
        if (
            freshness.consumer_checkpoint(root, "vault") != self.checkpoint
            or freshness.is_live(root, "vault") != self.live
            or freshness.external_pending_epoch(root) != self.external_pending_epoch
        ):
            diagnostics[_EVENT_PROOF_CHANGED] += 1
            return False
        if not self.owners.snapshot_is_current():
            diagnostics[_SNAPSHOT_CHANGED] += 1
            return False
        return True


@dataclass(frozen=True)
class _Revision:
    order: str
    path: str
    frontmatter: dict[str, Any]
    #: The content hash of the text this listing read: what a supersession of
    #: this revision is guarded by.
    hash: str

    @property
    def live(self) -> bool:
        # The atomic episode revision protocol fixes this recorder tombstone.
        return str(self.frontmatter.get("status") or "").casefold() != "superseded"


def _folder(vault_root: Path) -> Path:
    return kb_root(vault_root) / source_taxonomy.SOURCES_ROOT / source_taxonomy.EPISODE_PATH_LABEL


def _revisions(
    vault_root: Path, key: str, audience: str, *, diagnostics: dict[str, int] | None = None
) -> list[_Revision]:
    """This audience's recap pages of this episode, oldest first, from ONE
    listing of one folder.

    The filename carries the audience-scoped group and the recording time, so
    only this audience's pages of this group are read, and only their
    frontmatter matters: a group collision is ruled out by the `episode` field
    itself.
    """
    folder = _folder(vault_root)
    group = episode_capture.key_group(key, audience)
    try:
        with os.scandir(folder) as entries:
            names = [entry.name for entry in entries if entry.is_file()]
    except FileNotFoundError:
        if diagnostics is not None:
            diagnostics.update(listed_files=0, matched_files=0, failed_reads=0)
        return []
    if diagnostics is not None:
        diagnostics["listed_files"] = len(names)
    matched = sorted(
        (parts[1], name)
        for name in names
        if (parts := episode_capture.filename_parts(name)) is not None and parts[0] == group
    )
    if diagnostics is not None:
        diagnostics.update(matched_files=len(matched), failed_reads=0)
    revisions: list[_Revision] = []
    for order, name in matched:
        path = folder / name
        try:
            text, _guard = read_guarded_text(vault_root, path)
            frontmatter, _body, _raw = parse_frontmatter(text)
        except (OSError, UnicodeDecodeError, ValueError):
            if diagnostics is not None:
                diagnostics["failed_reads"] += 1
            continue
        if frontmatter.get("episode") == key:
            revisions.append(
                _Revision(
                    order,
                    path.relative_to(vault_root).as_posix(),
                    frontmatter,
                    content_hash(text),
                )
            )
    return revisions


def _source(revision_path: str, frontmatter: dict[str, Any]) -> dict[str, str]:
    return {
        "ref": memory_refs.memory_ref(str(frontmatter.get("exomem_id") or "")),
        "path": revision_path,
        "title": str(frontmatter.get("title") or ""),
    }


@call_spans.timed("episode.write")
def _write(
    vault_root: Path,
    source_schema: Any,
    recap: episode_capture.Recap,
    when: Any,
    audience: str,
) -> tuple[dict[str, str], bool]:
    """The committed recap page for `recap`, writing it only when it is new.

    Identical to this audience's newest live revision means a retry: nothing is
    written. Otherwise the new page is written, ordered after every revision
    listed, and every live revision listed is retired in the same batch,
    guarded by the hash this listing read. A revision another writer changed
    after this listing (a concurrent record retiring it, say) fails the call;
    the listing is then read again and the write retried once.
    """
    for attempt in range(2):
        # These four fixed counts measure revision discovery, never page content.
        revision_counts: dict[str, int] = {}
        with call_spans.span("episode.revisions", revision_counts):
            revisions = _revisions(vault_root, recap.key, audience, diagnostics=revision_counts)
            live = [item for item in revisions if item.live]
            revision_counts["live_revisions"] = len(live)
        if live and live[-1].frontmatter.get("episode_digest") == recap.digest:
            return _source(live[-1].path, live[-1].frontmatter), True
        # Never before the newest revision on disk, whatever the clock says.
        order = episode_capture.order_token(
            when, after=revisions[-1].order if revisions else None
        )
        try:
            result = add_module.add(
                vault_root,
                source_schema,
                content=recap.body,
                title=recap.subject,
                source_type=source_taxonomy.EPISODE_KIND,
                slug=recap.slug_at(order),
                today=when.replace(microsecond=0),
                extra_frontmatter=recap.frontmatter,
                supersede=tuple((item.path, item.hash) for item in live),
                defer_fanout_to_terminal=True,
            )
        except add_module.AddError as error:
            raise ValueError(f"{error.code}: {error.reason} (missing: {error.missing})") from error
        except (ContentHashMismatchError, BatchWriteError):
            if attempt:
                raise
            continue
        query_log.log_write_call(tool="episode_memory", written_path=result.path, cited_sources=[])
        return {"ref": result.ref, "path": result.path, "title": recap.subject}, False
    raise AssertionError("unreachable")  # pragma: no cover


def record(
    vault_root: Path,
    source_schema: Any,
    *,
    episode: str | None,
    subject: Any,
    summary: Any,
    worked_on: Any = None,
    decided: Any = None,
    open: Any = None,  # noqa: A002 - the recap's own field name
    said: Any = None,
    about: Any = None,
    client: Any = None,
) -> dict[str, Any]:
    """Write (or recognise) one recap revision and bind it to the caller's ledger."""
    vault_root = Path(vault_root)
    owner = EpisodeInputOwner(vault_root)
    # Before anything else, so an unresolved caller writes nothing at all.
    audience = owner._owner()  # noqa: SLF001 - the facade's own owner resolution
    # Microseconds kept for the filename's order token only; the Source writer
    # dates the page to the second, as it dates every page.
    when = dt.datetime.now().astimezone()
    fields = {
        "subject": subject,
        "summary": summary,
        "worked_on": worked_on,
        "decided": decided,
        "open": open,
        "said": said,
        "client": client,
    }
    original_recap = episode_capture.prepare(
        episode=episode, about=about, when=when, audience=audience, **fields
    )
    request_id = writer_lease.active_mutation_request_id()
    retry = writer_lease._active_capture_preparation_retry() if request_id is not None else None

    def retry_available() -> bool:
        return request_budget.can_afford(request_budget.DELIVERY_RESERVE_SECONDS) and (
            retry is None or retry.available()
        )

    source = None
    # These causes name the two closed preparation failures, never user meaning.
    cause = "preparation_changed"
    attempt = 0
    diagnostics = {
        _PREPARATION_ATTEMPTS: 0,
        _COMMIT_CHANGED: 0,
        _EVENT_PROOF_CHANGED: 0,
        _SNAPSHOT_CHANGED: 0,
    }
    # Covers preparation through ledger binding; nested phase times overlap.
    with call_spans.span("episode.reference_commit", diagnostics):
        while True:
            if attempt:
                if not retry_available() or (retry is None and attempt >= 2):
                    break
                # Every invalidation reaches this seam after releasing its guard.
                # The invocation retains one existing FIFO ticket and waiter slot.
                if retry is not None and not retry.admit():
                    break
            attempt += 1
            recap = original_recap
            preparation = None
            if request_id is not None and recap.about:
                generation = writer_lease.read_commit_generation(vault_root)
                if generation is None:
                    # An unreadable counter cannot seal a census. Avoid an
                    # expensive walk that no later check could admit.
                    cause = "generation_unavailable"
                    break
                checkpoint = freshness.consumer_checkpoint(vault_root, "vault")
                live = freshness.is_live(vault_root, "vault")
                external_pending_epoch = freshness.external_pending_epoch(vault_root)
                if attempt > 1 and not retry_available():
                    break
                diagnostics[_PREPARATION_ATTEMPTS] += 1
                owners = memory_refs._reference_owners_read_only(
                    vault_root, (memory_refs.parse_memory_ref(ref) for ref in recap.about)
                )
                preparation = _ReferencePreparation(
                    owners, generation, checkpoint, live, external_pending_epoch
                )
                if not preparation.current(vault_root, diagnostics=diagnostics):
                    continue
            # A census cannot be interrupted; its completion may spend the
            # allowance before this additional guarded attempt can start.
            if attempt > 1 and not retry_available():
                break
            # Direct add callers run fanout inline. Preserve their existing path
            # instead of pulling that derived work into a new outer guard.
            guard = (
                writer_lease.active_manager().mutation_guard(
                    vault_root, request_id=request_id, operation="episode_memory", holder_kind="command"
                )
                if request_id is not None else nullcontext()
            )
            with call_spans.entered("episode.guard_entry", guard):
                if preparation is not None and not preparation.current(vault_root, diagnostics=diagnostics):
                    continue
                about_skipped = 0
                about_paths: dict[str, str] = {}
                if recap.about:
                    about_paths = egress.visible_memory_ref_paths(
                        vault_root, recap.about, principal=effective_principal(),
                        _prepared_owners=preparation.owners if preparation is not None else None,
                    )
                    kept = [ref for ref in recap.about if ref in about_paths]
                    about_skipped = len(recap.about) - len(kept)
                    if about_skipped:
                        recap = episode_capture.prepare(
                            episode=recap.key, about=kept, when=when, audience=audience, **fields
                        )
                # Pending and policy reads can observe another external event.
                # Reject before the first revision read or any canonical effect.
                if preparation is not None and not preparation.current(vault_root, diagnostics=diagnostics):
                    continue
                source, idempotent = _write(vault_root, source_schema, recap, when, audience)
                revision: int | None
                ledger_warnings: list[str] = []
                try:
                    with call_spans.span("episode.journal_bind"):
                        bound = owner.bind_committed_input(
                            recap.key, path=source["path"], reference=source["ref"], about=recap.about
                        )
                except (EpisodeError, curation.CurationError, OSError) as error:
                    if getattr(error, "code", None) == "EPISODE_OWNER_UNRESOLVED":
                        raise
                    # The recap is committed and stays readable; only this caller's ledger
                    # missed it -- a refused or corrupt journal, or an unwritable one. An
                    # error here would invite a retry of a write that already happened. A
                    # transient failure binds on retry; a full ledger (EPISODE_TOO_LARGE)
                    # never does, so that one carries its cause and the way out.
                    log.warning(
                        "episode ledger bind failed after a committed recap: %s",
                        getattr(error, "code", type(error).__name__),
                    )
                    ledger, recovery, revision = "unbound", "unavailable", None
                    if getattr(error, "code", None) == "EPISODE_TOO_LARGE":
                        ledger_warnings = [LEDGER_FULL_WARNING]
                else:
                    ledger, recovery, revision = bound["ledger"], bound["recovery"], bound["input_revision"]
            break
    if source is None:
        raise OpError(
            "REFERENCE_PREPARATION_UNAVAILABLE",
            "Reference ownership could not be validated; nothing was written.",
            "Retry the unchanged request with the same idempotency key.",
            details={"status": "retryable", "committed": False, "cause": cause},
        )
    from .writer_lease import defer_housekeeping_until_terminal_persisted

    about_heat_paths = [about_paths[ref] for ref in recap.about if ref in about_paths]

    # Measure this callback separately from the acknowledgement interval.
    @call_spans.timed("episode.housekeeping")
    def record_housekeeping() -> None:
        if not idempotent:
            # A retry wrote nothing and records no new heat.
            try:
                heat_source_path = source["path"]
                with call_spans.span("episode.heat_attribution"):
                    attribution = working_set_heat.attribution_for(
                        vault_root, client=client, session=recap.key
                    )
                with call_spans.span("episode.heat_record"):
                    working_set_heat.note_episode(
                        vault_root,
                        heat_source_path,
                        about_heat_paths,
                        attribution=attribution,
                    )
            except Exception as error:  # noqa: BLE001 - the recap already committed
                log.debug("episode heat failed after commit: %s", type(error).__name__)
        with call_spans.span("episode.nudge"):
            episode_nudge.note_record(vault_root)

    if not defer_housekeeping_until_terminal_persisted(record_housekeeping):
        record_housekeeping()
    return {
        "operation": "episode_memory",
        "episode": recap.key,
        "revision": revision,
        "source": source,
        "idempotent": idempotent,
        "recovery": recovery,
        "ledger": ledger,
        "about_skipped": about_skipped,
        **({"warnings": ledger_warnings} if ledger_warnings else {}),
    }


def inspect(vault_root: Path, *, episode: Any) -> dict[str, Any]:
    """This caller's revision history of `episode`.

    An unknown key and a key only another audience holds are the same
    `EPISODE_NOT_FOUND`: the ledger is per audience and lists nothing across.
    """
    if not isinstance(episode, str) or not episode_capture.EPISODE_KEY_RE.fullmatch(episode):
        raise EpisodeError("EPISODE_KEY_INVALID", "episode must be an ep- key of 32 lowercase hex")
    history = EpisodeInputOwner(Path(vault_root)).input_history(episode)
    revisions = [dict(item) for item in history["revisions"]]
    latest = history["latest_reference"]
    # Released at bind time is not released now: a policy change since then
    # withholds the ref rather than disclosing that the page exists, and a
    # page this caller may not read is not recoverable by it either.
    if latest is not None and latest not in egress.visible_memory_refs(
        Path(vault_root), [latest], principal=effective_principal()
    ):
        latest = None
        revisions[-1]["recovery"] = "unavailable"
    return {
        "episode": episode,
        "revisions": revisions,
        "latest_source_ref": latest,
        "coverage_current": "unchecked",
    }
