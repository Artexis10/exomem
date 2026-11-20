"""When and how much media result publication takes of the vault boundary.

The media child records each finished result durably (`media_job_results`), so the supervisor
may publish it later without losing it. These budgets bound the boundary it takes and the
foreground writes it waits for. Each is the only copy; the supervisor's gate and its batch publisher
read this module, and the status read reports the queue they act on.
"""

from __future__ import annotations

#: Results the supervisor takes from the queue for one batch. It bounds the planning read and so the one
#: batch commit; the staging stops at `BATCH_HOLD_SECONDS`, so a backlog publishes at the speed of the hold.
BATCH_RESULT_LIMIT = 64

#: A batch stops staging more results after this long inside one hold, so a slow page cannot grow the hold
#: with every item behind it. The hold lasts this long plus one result's staging; the batch commits what it
#: staged and leaves the rest pending for the next batch.
BATCH_HOLD_SECONDS = 3.0

#: Foreground writes must have been quiet this long before a batch takes the boundary. A foreground
#: write is one to a few seconds; a gap longer than that is a real pause, not the next request.
QUIET_GAP_SECONDS = 2.0

#: Least time between two batches. Each batch advances the commit generation once, and every advance
#: sets off corpus-sized derived work, so this holds media-originated advances to about one a minute.
MIN_BATCH_SPACING_SECONDS = 60.0

#: A result that has waited this long publishes in the next batch even when foreground writes never
#: pause. The progress floor: waiting for a quiet gap defers publication, it never starves it.
MAX_DEFER_SECONDS = 180.0


def floor_wait_seconds(*, oldest_age_seconds: float, since_last_batch_seconds: float | None) -> float:
    """Seconds until the progress floor opens, counted from the later of the oldest result and the last batch.

    A result that cannot stage stays the oldest forever. Counting from the last batch too means it
    opens the floor once per `MAX_DEFER_SECONDS`, not on every pass.
    """
    waited = oldest_age_seconds
    if since_last_batch_seconds is not None:
        waited = min(waited, since_last_batch_seconds)
    return max(0.0, MAX_DEFER_SECONDS - waited)
