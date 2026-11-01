"""What the write journal reports about itself (design D10): log rows, metrics, and the health the doctor reads.

Every function here is best effort: observability never changes what a write returns or how an intent ends.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from . import metrics, mutation_journal, write_intents

log = logging.getLogger(__name__)

#: A write that waited this long as `accepted` with no applier alive is stuck (design D10).
STALE_ACCEPTED_SECONDS = 5 * 60.0


def refresh_gauges(journal: write_intents.WriteIntentJournal) -> None:
    """Set the queue depth and the age of the oldest accepted intent from the journal now."""
    try:
        status = journal.queue_status()
        metrics.set_gauge(
            "exomem_write_intent_queue_depth", status["accepted"] + status["applying"]
        )
        oldest = status["oldest_accepted_at"]
        metrics.set_gauge(
            "exomem_write_intent_oldest_age_seconds",
            0.0 if oldest is None else max(0.0, journal.clock() - oldest),
        )
    except Exception:  # noqa: BLE001 - observability must never break the write
        log.debug("write intent gauges were not refreshed", exc_info=True)


def accepted(
    journal: write_intents.WriteIntentJournal,
    intent: write_intents.Intent,
    *,
    waited_ms: float | None = None,
    log_row: bool = False,
) -> None:
    """An intent was recorded: count it, time the request's wait, and refresh the gauges.

    A write that accept recorded has its `accepted` row from the dispatcher, which knows the request; a retry
    has none, so it passes `log_row`.
    """
    if log_row:
        mutation_journal.record_mutation(
            request_id=intent.request_id,
            tool=intent.command,
            command=intent.command,
            receipt_id=None,
            outcome="accepted",
            error_code=None,
            duration_ms=None,
            targets=list(intent.planned_paths or ()),
            intent_id=intent.intent_id,
        )
    metrics.inc_counter("exomem_write_intents_total", {"outcome": "accepted"})
    if waited_ms is not None:
        metrics.observe_duration_ms("exomem_write_accept_wait_ms", waited_ms)
    refresh_gauges(journal)


def accept_result(result: str) -> None:
    """How an eligible write inside an accept window ended: `inline`, `committed` (by the applier, inside
    the window) or `accepted`. The first two over the three is the share committed before the deadline."""
    metrics.inc_counter("exomem_write_accept_total", {"result": result})


def ended(
    journal: write_intents.WriteIntentJournal,
    intent: write_intents.Intent,
    *,
    cause: str | None = None,
) -> None:
    """An intent ended: log its row, count it by outcome, time accept to end, and refresh the gauges."""
    try:
        mutation_journal.record_intent_end(intent, cause=cause)
        outcome = mutation_journal.INTENT_END_OUTCOMES.get(intent.state)
        if outcome is not None:
            metrics.inc_counter("exomem_write_intents_total", {"outcome": outcome})
            end = intent.terminal_at or intent.updated_at
            metrics.observe_duration_ms(
                "exomem_write_intent_accept_to_end_ms",
                (end - intent.accepted_at) * 1000,
                {"outcome": outcome},
            )
        refresh_gauges(journal)
    except Exception:  # noqa: BLE001 - observability must never break the write
        log.debug("write intent end was not observed", exc_info=True)


def waited(cause: str) -> None:
    """The applier requeued an intent for `cause`; `cause` comes from the closed outcome table."""
    metrics.inc_counter("exomem_write_intent_waits_total", {"cause": cause})


def health(vault_root: Path) -> dict[str, Any] | None:
    """The journal's health for the doctor: depth, oldest age, conflicts, format and applier liveness.

    None when the vault has no journal. Raises `JournalRefusal` when the journal cannot be read.
    """
    from . import intent_applier, state_paths, writer_lease

    if not (state_paths.vault_state_dir(vault_root) / write_intents.JOURNAL_FILENAME).exists():
        return None
    journal = writer_lease.active_manager()._intent_journal(vault_root)
    if journal is None:
        return None
    status = journal.queue_status()
    oldest = status.pop("oldest_accepted_at")
    status["oldest_accepted_age_seconds"] = (
        None if oldest is None else round(max(0.0, time.time() - oldest), 1)
    )
    status["applier_alive"] = intent_applier.applier_alive(journal)
    return status
