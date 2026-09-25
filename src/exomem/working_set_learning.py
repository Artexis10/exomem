"""Learning from the agent's corrections (close-memory-loop step 5, lane B).

The sensor is the agent's pick. When the user says "no, I meant X", re-asks
about something the packet missed, or the agent chooses a page other than the
one served, the agent calls `activate_context` again with the same turn and
`anchor=X`. Nothing in the correction is parsed from language: at that seam,
after the egress guard admitted the choice, `observe_pick` classifies the pick
from facts the request already holds.

| class      | condition                                                      |
|------------|----------------------------------------------------------------|
| `naming`   | the chosen page is an anchor, the turn is not referential, and |
|            | the resolver's own rule over that one row finds no worded      |
|            | contact: the turn's words did not name it                      |
| `cue`      | the turn is not referential, none of its content words is an   |
|            | anchor title or alias term, and the chosen page is a leading   |
|            | member or in the recent top eight                              |
| `referent` | the turn is referential and the chosen page was not leading:   |
|            | the prior was wrong, the pick moves it; heat only              |

A `naming` or `cue` pick counts a miss (a path, a class, a count and times;
never a word of the turn) and may carry ONE bounded advisory naming the
existing writers that would teach the vault the user's words: `edit_memory`
for a `learned_aliases` entry, `schema_memory save-conventions` for a
referential cue. The advisory writes nothing and grants nothing; the writers'
own hash guards refuse a stale one. Its dispositions reuse the write-advisory
family mechanism (`triage_memory` dismiss, snooze, quiet, off).

`turn_terms` are the caller's own content words, returned only to that caller
and never stored or logged. Words that already name some anchor are left out,
so an advisory can never propose teaching one anchor another anchor's name.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

ADVISORY_KIND = "activation-naming"
#: Registered with `review_state.registered_families()` from this module, the
#: way `corpus_aware._WRITE_ADVISORY_KINDS` registers the write advisories.
_ADVISORY_KINDS = frozenset({ADVISORY_KIND})
_REF_PREFIX = f"exomem://review/{ADVISORY_KIND}/"
MAX_ADVISORY_CHARS = 900
MAX_TURN_TERMS = 8
#: The recent-context top the `cue` class looks in (U1's eight entries).
RECENT_TOP = 8
COOLDOWN_NS = 24 * 3600 * 1_000_000_000
CLASSES: tuple[str, ...] = ("naming", "cue", "referent")
RULE = (
    "Only if the user's own words should reach this page next time. "
    "Decline with triage_memory."
)
#: Tests move the sensor's clock with this; production never sets it.
_CLOCK_OFFSET_NS = 0


def _now_ns() -> int:
    return time.time_ns() + _CLOCK_OFFSET_NS


def _day(ts_ns: int) -> str:
    return dt.datetime.fromtimestamp(ts_ns / 1e9, tz=dt.UTC).date().isoformat()


# --------------------------------------------------------------------------- #
# Classification: pure
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Observation:
    """One pick's class, and what the advisory may say about it."""

    klass: str | None
    #: The `cue` condition also held on a `naming` pick.
    cue_also: bool = False
    #: The caller's own content words, minus any that name an anchor.
    turn_terms: tuple[str, ...] = ()


def _names_an_anchor(token: str, term_anchor_counts: Mapping[str, int]) -> bool:
    from .working_set_index import fold_plural

    return token in term_anchor_counts or fold_plural(token) in term_anchor_counts


def classify(
    analysis: Any,
    *,
    chosen_path: str,
    row: Any | None,
    term_anchor_counts: Mapping[str, int],
    leading: Iterable[str],
    recent: Iterable[str],
    stopwords: frozenset[str],
    filler: frozenset[str],
) -> Observation:
    """Classify one admitted pick. No I/O: every input is already in hand.

    `row` is the chosen anchor's resolver facts, `None` for a page that is not
    an anchor. `leading` is the referent's leading tier and `recent` the
    recent-context top, both from the profile the request compiled against.
    """
    from . import working_set_resolve

    leading = frozenset(leading)
    content = tuple(
        dict.fromkeys(
            token for token in analysis.tokens if token not in stopwords and token not in filler
        )
    )
    if analysis.referential:
        return Observation("referent" if chosen_path not in leading else None)
    if not content:
        return Observation(None)
    named = [token for token in content if _names_an_anchor(token, term_anchor_counts)]
    terms = tuple(token for token in content if token not in named)[:MAX_TURN_TERMS]
    if not terms:
        return Observation(None)
    cue = not named and (chosen_path in leading or chosen_path in frozenset(recent))
    if row is not None:
        # The resolver's own rule decides "the turn's words did not name it".
        # With one row, R2's consumption test cannot fire, which could only
        # find MORE contact, so the sensor errs toward silence.
        candidates = working_set_resolve.candidates_for(
            analysis, (row,), term_anchor_counts=term_anchor_counts, stopwords=stopwords
        )
        worded = any(
            set(candidate.evidence) & working_set_resolve.WORDED_CONTACT_KINDS
            for candidate in candidates
        )
        if not worded:
            return Observation("naming", cue_also=cue, turn_terms=terms)
        return Observation(None)
    if cue:
        return Observation("cue", turn_terms=terms)
    return Observation(None)


# --------------------------------------------------------------------------- #
# The miss counter: machine-local, beside the heat ring
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class MissRow:
    path: str
    klass: str
    count: int
    first_ns: int
    last_ns: int
    days: int
    advised_ns: int = 0
    advised_bucket: int = -1

    @property
    def bucket(self) -> int:
        return self.count // 2


_TABLE = (
    "CREATE TABLE IF NOT EXISTS learning_misses ("
    "path TEXT NOT NULL, class TEXT NOT NULL, count INTEGER NOT NULL, "
    "first_ns INTEGER NOT NULL, last_ns INTEGER NOT NULL, days INTEGER NOT NULL, "
    "last_day TEXT NOT NULL, advised_ns INTEGER NOT NULL DEFAULT 0, "
    "advised_bucket INTEGER NOT NULL DEFAULT -1, PRIMARY KEY (path, class))"
)
_COLUMNS = "path, class, count, first_ns, last_ns, days, advised_ns, advised_bucket"


def _connection(vault_root: Path) -> sqlite3.Connection | None:
    """The heat sidecar's connection, with the counter's table. The counter
    writes never bump the heat token: a miss changes no ranking."""
    from . import working_set_heat

    if working_set_heat.disabled():
        return None
    conn = working_set_heat._connect(Path(vault_root))
    if conn is None:
        return None
    try:
        conn.execute(_TABLE)
    except sqlite3.Error:
        conn.close()
        return None
    return conn


def _row(values: Sequence[Any]) -> MissRow:
    path, klass, count, first_ns, last_ns, days, advised_ns, advised_bucket = values
    return MissRow(
        str(path), str(klass), int(count), int(first_ns), int(last_ns), int(days),
        int(advised_ns), int(advised_bucket),
    )


def record_miss(vault_root: Path, path: str, klass: str, *, now_ns: int | None = None) -> MissRow | None:
    """Count one miss. Never raises: a busy sidecar loses a count, nothing else."""
    now = _now_ns() if now_ns is None else now_ns
    today = _day(now)
    conn = _connection(vault_root)
    if conn is None:
        return None
    try:
        with conn:
            found = conn.execute(
                "SELECT count, last_day FROM learning_misses WHERE path = ? AND class = ?",
                (path, klass),
            ).fetchone()
            if found is None:
                conn.execute(
                    "INSERT INTO learning_misses (path, class, count, first_ns, last_ns, days, "
                    "last_day) VALUES (?, ?, 1, ?, ?, 1, ?)",
                    (path, klass, now, now, today),
                )
            else:
                conn.execute(
                    "UPDATE learning_misses SET count = count + 1, last_ns = ?, "
                    "days = days + ?, last_day = ? WHERE path = ? AND class = ?",
                    (now, 0 if str(found[1]) == today else 1, today, path, klass),
                )
            values = conn.execute(
                f"SELECT {_COLUMNS} FROM learning_misses WHERE path = ? AND class = ?",
                (path, klass),
            ).fetchone()
        return _row(values) if values else None
    except sqlite3.Error:
        log.debug("learning miss not recorded", exc_info=True)
        return None
    finally:
        conn.close()


def mark_advised(vault_root: Path, row: MissRow, *, now_ns: int | None = None) -> None:
    now = _now_ns() if now_ns is None else now_ns
    conn = _connection(vault_root)
    if conn is None:
        return
    try:
        with conn:
            conn.execute(
                "UPDATE learning_misses SET advised_ns = ?, advised_bucket = ? "
                "WHERE path = ? AND class = ?",
                (now, row.bucket, row.path, row.klass),
            )
    except sqlite3.Error:
        log.debug("learning advisory stamp not recorded", exc_info=True)
    finally:
        conn.close()


def misses(vault_root: Path) -> tuple[MissRow, ...]:
    """Every counted miss, for the dreamer (step 6) and for tests."""
    conn = _connection(vault_root)
    if conn is None:
        return ()
    try:
        rows = conn.execute(f"SELECT {_COLUMNS} FROM learning_misses ORDER BY path, class")
        return tuple(_row(values) for values in rows)
    except sqlite3.Error:
        return ()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The advisory
# --------------------------------------------------------------------------- #


def advisory_ref(review_id: str) -> str:
    from . import review_state

    review_state.review_ref(review_id)
    return f"{_REF_PREFIX}{review_id}"


def is_advisory_ref(value: str) -> bool:
    return str(value or "").strip().lower().startswith(_REF_PREFIX)


def parse_advisory_ref(value: str) -> str:
    from . import review_state

    raw = str(value or "").strip()
    if not raw.lower().startswith(_REF_PREFIX):
        raise ValueError(f"INVALID_REVIEW_REFERENCE: expected {_REF_PREFIX}<id>")
    return review_state.parse_review_ref(f"{review_state.REVIEW_PREFIX}{raw[len(_REF_PREFIX):]}")


def identity(
    *, target_ref: str, klass: str, bucket: int, signal_version: str, turn_hash: str
) -> tuple[str, str]:
    """`(review_id, fingerprint)`, mirroring `corpus_aware.write_advisory_identity`:
    the id is stable per target; the fingerprint moves with the class, the miss
    bucket (`count // 2`), the page's signal version and the conventions' turn
    digest, so a dismissal holds until new evidence moves one of them."""
    from . import review_state
    from .vault import content_hash

    category = ADVISORY_KIND
    version = content_hash(
        f"{category}\n{target_ref}\n{klass}\n{bucket}\n{signal_version}\n{turn_hash}"
    )[:16]
    review_id = review_state.item_id(f"{category}:{target_ref}")
    fingerprint = review_state.fingerprint(
        target_ref=target_ref,
        categories=[category],
        reasons=[{"category": category, "meta": {"signal_version": version}}],
        related_refs=[],
    )
    return review_id, fingerprint


def _proactive_capture_permitted() -> bool:
    from . import capture_sweep

    return capture_sweep._proactive_capture_permitted()


def advisory_size(advisory: Mapping[str, Any]) -> int:
    """The bound is on the payload, not on how a transport spaces it: compact
    JSON, non-ASCII kept as written."""
    return len(json.dumps(advisory, ensure_ascii=False, separators=(",", ":")))


def _bounded(advisory: dict[str, Any]) -> dict[str, Any] | None:
    """At most `MAX_ADVISORY_CHARS` (`advisory_size`): drop turn words from the end,
    then shorten the title; an advisory that still does not fit is not sent."""
    def size() -> int:
        return advisory_size(advisory)

    while size() > MAX_ADVISORY_CHARS and advisory["turn_terms"]:
        advisory["turn_terms"] = advisory["turn_terms"][:-1]
    if size() > MAX_ADVISORY_CHARS:
        title = advisory["target"]["title"]
        overflow = size() - MAX_ADVISORY_CHARS
        advisory["target"]["title"] = title[: max(0, len(title) - overflow - 1)] + "…"
    return advisory if size() <= MAX_ADVISORY_CHARS else None


def build_advisory(
    vault_root: Path,
    *,
    observation: Observation,
    row: MissRow,
    target: Mapping[str, Any],
    conventions_registry: Any,
    now_ns: int | None = None,
) -> dict[str, Any] | None:
    """The one bounded review candidate for a counted miss, or None when it is
    suppressed. Reads the page (its hash) and the review state, and only
    here, where a class fired; writes nothing to the vault and never stamps
    the first-surfaced ledger."""
    from . import review_state
    from . import vault as vault_module

    now = _now_ns() if now_ns is None else now_ns
    if not _proactive_capture_permitted():
        return None
    if row.advised_bucket == row.bucket and now - row.advised_ns < COOLDOWN_NS:
        return None
    path = str(target.get("path") or "")
    target_ref = str(target.get("ref") or path)
    try:
        text = (Path(vault_root) / path).read_text(encoding="utf-8")
    except OSError:
        return None
    page_hash = vault_module.content_hash(text)
    review_id, fingerprint = identity(
        target_ref=target_ref,
        klass=row.klass,
        bucket=row.bucket,
        signal_version=page_hash[:16],
        turn_hash=conventions_registry.turn_hash,
    )
    try:
        store = review_state.ReviewStateStore(Path(vault_root))
        payload = store.load()
    except Exception:  # noqa: BLE001 - an ungovernable advisory is not delivered
        log.debug("review state unreadable; no learning advisory", exc_info=True)
        return None
    if review_state.disposition_for(ADVISORY_KIND, payload=payload) in {"quiet", "off"}:
        return None
    state, _decision = store.effective_state(review_id, fingerprint, payload=payload)
    if state in {"dismissed", "snoozed"}:
        return None

    options: list[dict[str, Any]] = []
    if row.klass == "naming":
        frontmatter, _body, _raw = vault_module.parse_frontmatter(text)
        current = frontmatter.get("learned_aliases") if isinstance(frontmatter, dict) else None
        options.append(
            {
                "family": "name",
                "tool": "edit_memory",
                "path": path,
                "field": "learned_aliases",
                "current": [str(item) for item in current] if isinstance(current, list) else [],
                "expected_hash": page_hash,
            }
        )
    if row.klass == "cue" or observation.cue_also:
        options.append(
            {
                "family": "referential_cue",
                "tool": "schema_memory",
                "subject": "activation-conventions",
                "operation": "save-conventions",
                "expected_hash": conventions_registry.content_hash,
            }
        )
    advisory = _bounded(
        {
            "kind": ADVISORY_KIND,
            "review": advisory_ref(review_id),
            "fingerprint": fingerprint,
            "target": {
                "ref": target_ref,
                "title": str(target.get("title") or ""),
                "kind": str(target.get("kind") or ""),
            },
            "observed": {
                "class": row.klass,
                "misses": row.count,
                "since": _day(row.first_ns),
                "days": row.days,
                "turn_reached": "nothing",
            },
            "turn_terms": list(observation.turn_terms),
            "options": options,
            "rule": RULE,
        }
    )
    if advisory is not None:
        mark_advised(vault_root, row, now_ns=now)
    return advisory


def observe_pick(
    vault_root: Path,
    *,
    turn: str,
    packet: Mapping[str, Any],
    profile: Any,
    attribution: Any = None,
) -> dict[str, Any] | None:
    """The pick seam: classify, count, and maybe advise. Called after the
    guard admitted the choice and BEFORE the pick itself is recorded as heat,
    so "was it leading" is asked of the profile the request compiled against.
    Never raises; returns the advisory or None."""
    from . import (
        activation_conventions,
        working_set,
        working_set_heat,
        working_set_index,
        working_set_resolve,
    )

    try:
        chosen = next(
            (
                item
                for item in packet.get("anchors") or ()
                if isinstance(item, Mapping) and item.get("status") == "resolved"
            ),
            None,
        )
        if chosen is None or not chosen.get("path"):
            return None
        chosen_path = str(chosen["path"])
        registry = activation_conventions.load_conventions(Path(vault_root))
        conventions = registry.conventions
        analysis = working_set_resolve.analyze_turn(turn, vocabulary=conventions.referential)
        index = working_set_index.WorkingSetIndex(Path(vault_root))
        rows = working_set_resolve.facts_from_rows(index.anchors())
        row = next((item for item in rows if item.path == chosen_path), None)
        leads = working_set_heat.leading(profile, attribution=attribution)
        leading = working_set_heat.members(leads, limit=working_set.HOT_PROFILE_K).paths
        recent = [
            contact.path
            for contact in working_set_heat.recent(
                profile, attribution=attribution, limit=RECENT_TOP
            )
        ]
        observation = classify(
            analysis,
            chosen_path=chosen_path,
            row=row,
            term_anchor_counts=index.term_anchor_counts() if not analysis.referential else {},
            leading=leading,
            recent=recent,
            stopwords=conventions.stopwords,
            filler=conventions.referential_filler,
        )
        if observation.klass not in {"naming", "cue"}:
            return None
        counted = record_miss(Path(vault_root), chosen_path, observation.klass)
        if counted is None:
            return None
        return build_advisory(
            vault_root,
            observation=observation,
            row=counted,
            target={
                "path": chosen_path,
                "ref": chosen.get("ref") or chosen_path,
                "title": chosen.get("title"),
                "kind": chosen.get("kind"),
            },
            conventions_registry=registry,
        )
    except Exception:  # noqa: BLE001 - learning is advice; it never fails a pick
        log.debug("pick observation failed; no learning advisory", exc_info=True)
        return None


# --------------------------------------------------------------------------- #
# Dispositions
# --------------------------------------------------------------------------- #


def triage(
    vault_root: Path,
    *,
    ref: str,
    action: str,
    until: str | None = None,
    why: str | None = None,
    expected_fingerprint: str | None = None,
) -> dict:
    """Dismiss, snooze or reopen one activation-naming advisory, beside the
    write-advisory branch and with the same rules."""
    from . import review_state

    normalized = str(action or "").strip().lower()
    if normalized not in {"dismiss", "snooze", "reopen"}:
        raise ValueError(
            "INVALID_REVIEW_ACTION: activation-naming advisories accept dismiss, snooze, or reopen"
        )
    if normalized == "dismiss" and not str(why or "").strip():
        raise ValueError("INVALID_REVIEW_ACTION: activation-naming dismiss requires `why`")
    review_id = parse_advisory_ref(ref)
    store = review_state.ReviewStateStore(vault_root)
    if normalized != "reopen" and not expected_fingerprint:
        raise ValueError(
            "INVALID_REVIEW_ACTION: activation-naming dismiss/snooze requires the "
            "surfaced fingerprint"
        )
    result = store.apply(
        review_id,
        expected_fingerprint or "",
        action=normalized,
        until=until,
        why=why,
        family=None if normalized == "reopen" else ADVISORY_KIND,
    )
    result["ref"] = advisory_ref(review_id)
    return result
