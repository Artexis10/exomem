"""When and how much media result publication takes of the vault boundary.

The media child records each finished result durably (`media_job_results`), so the supervisor
may publish it later without losing it. These budgets bound the boundary it takes and the
foreground writes it waits for. Each is the only copy; the supervisor and the status read both
use this module.
"""

from __future__ import annotations

#: Results published under one boundary hold. One hold carries one commit, one commit-generation
#: advance and one fan-out, so a larger batch costs fewer advances; a smaller one keeps the hold short.
BATCH_RESULT_LIMIT = 16

#: Staging stops for the batch after this long inside one hold, so a slow page cannot grow the hold
#: with every result behind it. The staged results commit; the rest stay pending for the next batch.
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


def publication_due(
    *,
    idle_seconds: float,
    oldest_age_seconds: float,
    since_last_batch_seconds: float | None,
) -> bool:
    """Whether a batch may take the boundary now.

    `idle_seconds` is the foreground activity signal (`foreground_activity.idle_seconds`),
    `oldest_age_seconds` the age of the oldest waiting result, and `since_last_batch_seconds` the time
    since this process last committed a batch, None before the first. A batch needs the spacing, then
    either a quiet gap or a result past its progress floor.
    """
    if since_last_batch_seconds is not None and since_last_batch_seconds < MIN_BATCH_SPACING_SECONDS:
        return False
    return idle_seconds >= QUIET_GAP_SECONDS or oldest_age_seconds >= MAX_DEFER_SECONDS
