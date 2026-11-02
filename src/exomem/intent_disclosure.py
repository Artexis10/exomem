"""What a read says about the reader's own accepted writes (design D7).

An accepted write has no canonical bytes yet, so a read never shows its content. It reports the pending intent
beside the bytes it does hold. The journal read is best effort: a vault with no journal, an unreadable journal
or any fault here leaves the read as it was, and a read never waits on the journal's writers.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import Any

from . import intent_review, state_paths, write_intents

log = logging.getLogger(__name__)

KEY = "pending_writes"
#: How many intent references a status block names; the counts always cover every open intent.
STATUS_REFS = 5
#: The most intents one direct read inspects for its path. The journal bound keeps the real number far lower.
READ_SCAN_LIMIT = 200


def _reader_journal(vault_root: Path) -> tuple[write_intents.WriteIntentJournal, str | None] | None:
    """The journal and the audience to filter by (None for the owner), or None when there is nothing to read."""
    if not (state_paths.vault_state_dir(vault_root) / write_intents.JOURNAL_FILENAME).exists():
        return None
    audience, owner = intent_review._reader()
    _, journal = intent_review._manager_and_journal(vault_root)
    return journal, None if owner else audience


def _view(intent: write_intents.Intent) -> dict[str, Any]:
    return {
        "ref": f"{intent_review.REF_PREFIX}{intent.intent_id}",
        "command": intent.command,
        "state": intent.state,
    }


def for_path(vault_root: Path, path: str) -> list[dict[str, Any]]:
    """The reader's open intents that target `path`; empty when none or when the journal cannot be read."""
    try:
        found = _reader_journal(vault_root)
        if found is None:
            return []
        journal, audience = found
        rows = journal.open_intents(audience=audience, limit=READ_SCAN_LIMIT)
        return [
            _view(row)
            for row in rows
            if path in (row.planned_paths or ()) and intent_review._admitted(vault_root, row)
        ]
    except Exception:  # noqa: BLE001 - disclosure is best effort; the read must not fail on the journal
        log.warning("pending writes of %r were not read", path, exc_info=True)
        return []


def status(vault_root: Path) -> dict[str, Any] | None:
    """The reader's counts of accepted and conflicted writes with a few references; None when there are none."""
    try:
        found = _reader_journal(vault_root)
        if found is None:
            return None
        journal, audience = found
        # The counts follow the review rule, so they and the refs cover only what this reader may see.
        rows = [
            row
            for row in journal.open_intents(audience=audience)
            if intent_review._admitted(vault_root, row)
        ]
        counts = {
            "accepted": sum(row.state in write_intents.ACTIVE_STATES for row in rows),
            "conflicted": sum(row.state == "conflicted" for row in rows),
        }
        if not any(counts.values()):
            return None
        refs = [f"{intent_review.REF_PREFIX}{row.intent_id}" for row in rows[:STATUS_REFS]]
        return {**counts, "refs": refs}
    except Exception:  # noqa: BLE001 - disclosure is best effort; the read must not fail on the journal
        log.warning("pending write counts were not read", exc_info=True)
        return None


def discloses_path(read: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a direct read so its result names the reader's open intents on the path it returned."""

    @wraps(read)
    def wrapped(vault_root: Path, *args: Any, **kwargs: Any) -> Any:
        result = read(vault_root, *args, **kwargs)
        path = result.get("path") if isinstance(result, dict) else None
        if isinstance(path, str):
            pending = for_path(Path(vault_root), path)
            if pending:
                result[KEY] = pending
        return result

    return wrapped


def discloses_status(read: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap recall, search, browse or bootstrap so its result carries the reader's open-write counts."""

    @wraps(read)
    def wrapped(vault_root: Path, *args: Any, **kwargs: Any) -> Any:
        result = read(vault_root, *args, **kwargs)
        if isinstance(result, dict):
            counts = status(Path(vault_root))
            if counts is not None:
                result[KEY] = counts
        return result

    return wrapped
