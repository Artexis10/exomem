"""The `episode_memory` operation: record a bounded recap, bind it, inspect it.

`record` is the only way a recap reaches the vault. The agent authors it; this
module validates it (`episode_capture`), writes it through the ordinary Source
writer as an `episode` Source, and binds the committed page to the caller's own
episode ledger by the writer's receipt. An episode keeps one page: a later,
different recap rewrites that page in place, and the same atomic batch keeps the
page's previous bytes as a compiled-history entry. `inspect` reads that ledger back.

This is the only writer that rewrites a Source (`revise_page`), and only a recap
page of the caller's own episode: every other writer keeps refusing Sources.

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
    compiled_history,
    curation,
    due_state,
    episode_capture,
    episode_compile,
    episode_coverage,
    episode_nudge,
    freshness,
    memory_refs,
    query_log,
    recap_folder,
    request_budget,
    reserved_paths,
    source_coverage,
    source_taxonomy,
    vocabulary_auxiliaries,
    working_set_heat,
    writer_lease,
)
from .cli_ops import OpError
from .episode_model import EpisodeError
from .episode_recovery import EpisodeInputOwner
from .find import RetrievalIndexWarming
from .governance import egress
from .governance.principal import effective_principal
from .vault import (
    _FM_PATTERN,
    BatchWriteError,
    ContentHashMismatchError,
    FrontmatterError,
    PlannedWrite,
    batch_atomic_write,
    content_hash,
    document_newline,
    parse_frontmatter,
    read_guarded_text,
    with_newline,
    yaml_scalar,
)

log = logging.getLogger(__name__)

LEDGER_FULL_WARNING = (
    "This episode's ledger is full, so this recap committed as a Source but is not bound to it, "
    "and a retry may not bind it. Later recaps keep committing as Sources. "
    "Record the next recap without `episode` to start a new key."
)

ABOUT_UNSETTLED_WARNING = (
    "This recap committed without its `about` links because the pending index could not yet "
    "prove those memories' identities. Record the links again once indexing has caught up."
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
    return Path(vault_root) / recap_folder.recap_folder(vault_root)


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


def _not_revisable(reason: str) -> EpisodeError:
    return EpisodeError("EPISODE_PAGE_NOT_REVISABLE", f"{reason}; nothing was written")


def _revised_text(
    before: str, current: dict[str, Any], body_before: str, recap: episode_capture.Recap
) -> str:
    """`before` with the title, summary, digest, client and body of `recap`, every other byte kept.

    The previous Decided lines decide whether the page's `ingested_into` is
    cleared: a note that cited other Decided lines has not ingested these.
    """
    match = _FM_PATTERN.match(before)
    if match is None:
        raise _not_revisable("the episode page has no frontmatter")
    newline = document_newline(before)
    changed = episode_coverage.decided_lines(with_newline(body_before, "\n")) != recap.decided
    lines: dict[str, str | None] = {"title": f"title: {yaml_scalar(recap.subject)}"}
    expected: dict[str, Any] = {**current, "title": recap.subject}
    # The episode key is the page's identity and never changes; every other recap field follows.
    for name, _required in add_module.EPISODE_FRONTMATTER_FIELDS:
        if name == "episode":
            continue
        value = recap.frontmatter.get(name)
        lines[name] = None if value is None else f"{name}: {yaml_scalar(value)}"
        if value is None:
            expected.pop(name, None)
        else:
            expected[name] = value
    if changed and current.get("ingested_into"):
        lines["ingested_into"] = "ingested_into: []"
        expected["ingested_into"] = []
    block = episode_capture.rewrite_fields(
        match.group(1), lines, newline=newline, after="episode_digest"
    )
    if block is None:
        raise _not_revisable("the episode page's frontmatter is not in the recorded shape")
    # The recorder wrote this layout (`add._render_source`): heading, then the capture.
    tail = with_newline(
        f"\n# {recap.subject}\n\n{source_coverage.CAPTURE_HEADING}\n\n{recap.body}\n", newline
    )
    candidate = before[: match.start(1)] + block + before[match.end(1) : match.start(2)] + tail
    try:
        after, body, _raw = parse_frontmatter(candidate, strict=True)
    except FrontmatterError:
        after, body = {}, ""
    # A recap is parsed in its own LF form, whatever line ending the page keeps.
    if after != expected or episode_coverage.decided_lines(with_newline(body, "\n")) != recap.decided:
        raise _not_revisable("the episode page could not be rewritten exactly")
    return candidate


def revise_page(
    vault_root: Path, recap: episode_capture.Recap, *, path: str, expected_hash: str
) -> None:
    """Rewrite the episode's recap page at `path` to `recap`, keeping its previous bytes in compiled history.

    The page and its history entry commit in one atomic batch, so the page never
    changes without its previous text retained, and a failed history write
    leaves the page as it was. `expected_hash` is the page's content hash as the
    caller read it; a page another writer changed since fails the call.

    This is the one place a Source is rewritten. The target check is closed
    (Source tokens here are the recorder's protocol, not vault vocabulary): a recap Source in
    the episode folder whose `episode` key and filename group are this recap's.
    """
    # The auxiliary seal reads paths against the resolved root, as the semantic writer's callers do.
    root = Path(vault_root).resolve()
    target = root / path
    parts = episode_capture.filename_parts(target.name)
    if target.parent.resolve() != _folder(root).resolve() or parts is None or parts[0] != recap.group:
        raise _not_revisable("only this episode's own recap page can be revised")
    before, guard = read_guarded_text(root, target)
    actual = content_hash(before)
    if actual != expected_hash:
        raise ContentHashMismatchError(target, expected_hash, actual)
    try:
        current, body, _raw = parse_frontmatter(before, strict=True)
    except FrontmatterError as error:
        raise _not_revisable("the episode page's frontmatter is not valid") from error
    if (
        current.get("type") != "source"
        or current.get("source_type") != source_taxonomy.EPISODE_KIND
        or current.get("episode") != recap.key
    ):
        raise _not_revisable("only this episode's own recap page can be revised")
    after = _revised_text(before, current, body, recap)
    primary = PlannedWrite(path=target, content=after, expected_hash=expected_hash, guard=guard)
    history = compiled_history.plan_episode_entry(root, path=path, before_source=before, after_source=after)
    manifest = vocabulary_auxiliaries.seal(
        root, primary=primary, derived=(("compiled-history", history),)
    )
    intents: list[object] = []
    with reserved_paths._subsystem_authority_scope("compiled_history"):  # noqa: SLF001 - the store's own scope
        committed = batch_atomic_write(
            [primary, history],
            vault_root=root,
            post_commit_fanout=False,
            publication_intents_out=intents,
            _vocabulary_auxiliaries=manifest,
        )
    assert isinstance(committed, list)
    add_module.deferred_fanout(root, committed, created_paths=[history.path], publication_intents=intents)
    due_state.refresh_after_writes(root, [due_state.PageWrite(path, prior_markdown=before)])


@call_spans.timed("episode.write")
def _write(
    vault_root: Path,
    source_schema: Any,
    recap: episode_capture.Recap,
    when: Any,
    audience: str,
) -> tuple[dict[str, str], bool, list[str]]:
    """The committed recap page for `recap`, writing it only when it is new, and the episode's recap paths.

    An episode has one live page. The same digest as that page means a retry:
    nothing is written. A different recap rewrites the page in place
    (`revise_page`), whatever superseded files an older layout left beside it.
    An episode with no live page gets a new one, ordered after every file
    listed. A page another writer changed after this listing fails the call;
    the listing is then read again and the write retried once.
    """
    for attempt in range(2):
        # These four fixed counts measure revision discovery, never page content.
        revision_counts: dict[str, int] = {}
        with call_spans.span("episode.revisions", revision_counts):
            revisions = _revisions(vault_root, recap.key, audience, diagnostics=revision_counts)
            live = [item for item in revisions if item.live]
            revision_counts["live_revisions"] = len(live)
        paths = [item.path for item in revisions]
        head = live[-1] if live else None
        if head is not None and head.frontmatter.get("episode_digest") == recap.digest:
            return _source(head.path, head.frontmatter), True, paths
        try:
            if head is not None:
                revise_page(vault_root, recap, path=head.path, expected_hash=head.hash)
                source = {**_source(head.path, head.frontmatter), "title": recap.subject}
            else:
                # Never before the newest file on disk, whatever the clock says.
                order = episode_capture.order_token(
                    when, after=revisions[-1].order if revisions else None
                )
                result = add_module.add(
                    vault_root,
                    source_schema,
                    content=recap.body,
                    title=recap.subject,
                    source_type=source_taxonomy.EPISODE_KIND,
                    slug=recap.slug_at(order),
                    today=when.replace(microsecond=0),
                    extra_frontmatter=recap.frontmatter,
                    defer_fanout_to_terminal=True,
                )
                source = {"ref": result.ref, "path": result.path, "title": recap.subject}
                paths.append(result.path)
        except add_module.AddError as error:
            raise ValueError(f"{error.code}: {error.reason} (missing: {error.missing})") from error
        except (ContentHashMismatchError, BatchWriteError):
            if attempt:
                raise
            continue
        query_log.log_write_call(tool="episode_memory", written_path=source["path"], cited_sources=[])
        return source, False, paths
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
                about_warnings: list[str] = []
                if recap.about:
                    try:
                        about_paths = egress.visible_memory_ref_paths(
                            vault_root, recap.about, principal=effective_principal(),
                            _prepared_owners=preparation.owners if preparation is not None else None,
                        )
                    except RetrievalIndexWarming:
                        # The links are optional; the recap is the durable record, so
                        # unprovable pending identity omits them instead of refusing it.
                        about_warnings = [ABOUT_UNSETTLED_WARNING]
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
                source, idempotent, revision_paths = _write(vault_root, source_schema, recap, when, audience)
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
    warnings = [*about_warnings, *ledger_warnings]
    compiled = episode_compile.block(
        vault_root, ref=source["ref"], revisions=revision_paths, decided=len(recap.decided)
    )
    return {
        "operation": "episode_memory",
        "episode": recap.key,
        "revision": revision,
        "source": source,
        "idempotent": idempotent,
        "recovery": recovery,
        "ledger": ledger,
        "about_skipped": about_skipped,
        **({"compile": compiled} if compiled else {}),
        **({"warnings": warnings} if warnings else {}),
    }


def inspect(vault_root: Path, *, episode: Any) -> dict[str, Any]:
    """This caller's revision history of `episode`.

    An unknown key and a key only another audience holds are the same
    `EPISODE_NOT_FOUND`: the ledger is per audience and lists nothing across.
    Recovery classes were recorded at binding; current recovery is unchecked.
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
        "recovery_current": "unchecked",
        "coverage_current": "unchecked",
        **({"materials": history["materials"]} if "materials" in history else {}),
    }
