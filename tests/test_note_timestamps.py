"""Second-granularity knowledge time on notes, and the mixed vault it creates.

Notes used to record `created`/`updated` — and every `log.md` history heading —
as a bare calendar date, so a page revised three times in one afternoon carried
three identical dates and `created == updated`. Ordering was recoverable only
from position in the file, not from the data.

Writes now stamp a second-granularity UTC instant. Nothing is backfilled: a
date-only value means the intra-day time was never captured, and rewriting it
as midnight would invent precision. Both forms are therefore permanent, which
is what these tests pin.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from exomem import audit as audit_module
from exomem import audit_fix as audit_fix_module
from exomem import edit as edit_module
from exomem import find as find_module
from exomem import temporal
from exomem import vault as vault_module

NOTE_REL = "Knowledge Base/Notes/Insights/stamped-note.md"

SOURCE_LINK = "[[Knowledge Base/Sources/Articles/2026-06-02-postgres-autovacuum-tuning]]"

# Shaped like the compliant fixture insights: a cited source and a Connections
# section, so the semantic contract is satisfied and these tests exercise the
# timestamp behaviour rather than re-testing governance.
DATE_ONLY_BODY = f"""\
---
type: insight
status: active
created: 2026-01-01
updated: 2026-01-01
sources:
  - "{SOURCE_LINK}"
tags: [legacy]
---

# Legacy Note

## Claim

Written before timestamps existed, so its intra-day time is genuinely unknown.

## Overview

Detail line.

## Connections

- {SOURCE_LINK}
"""


def _seed(vault: Path, rel: str = NOTE_REL, body: str = DATE_ONLY_BODY) -> Path:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    find_module.clear_cache()
    return path


def _frontmatter(path: Path) -> dict:
    fm, _, _ = vault_module.parse_frontmatter(path.read_text(encoding="utf-8"))
    return fm


# --- writes stamp an instant -------------------------------------------------


def test_edit_stamps_second_granularity_utc(vault: Path) -> None:
    path = _seed(vault)
    edit_module.edit(
        vault, path=NOTE_REL, why="revise",
        heading="Overview", section_position="append", new_string="A point.",
        today=dt.datetime(2026, 8, 5, 9, 12, 33, tzinfo=dt.UTC),
    )
    assert "updated: 2026-08-05T09:12:33Z" in path.read_text(encoding="utf-8")


def test_edit_leaves_created_alone(vault: Path) -> None:
    """`created` is the one date an edit must never touch."""
    path = _seed(vault)
    edit_module.edit(
        vault, path=NOTE_REL, why="revise",
        heading="Overview", section_position="append", new_string="A point.",
        today=dt.datetime(2026, 8, 5, 9, 12, 33, tzinfo=dt.UTC),
    )
    assert "created: 2026-01-01" in path.read_text(encoding="utf-8")


def test_a_date_only_clock_still_writes_a_date(vault: Path) -> None:
    """Precision follows the injected type, so day-granular callers stay day-granular."""
    path = _seed(vault)
    edit_module.edit(
        vault, path=NOTE_REL, why="revise",
        heading="Overview", section_position="append", new_string="A point.",
        today=dt.date(2026, 8, 5),
    )
    assert "updated: 2026-08-05\n" in path.read_text(encoding="utf-8")


def test_local_offset_is_stored_as_utc(vault: Path) -> None:
    """One standard on disk, so values order across machines."""
    path = _seed(vault)
    edit_module.edit(
        vault, path=NOTE_REL, why="revise",
        heading="Overview", section_position="append", new_string="A point.",
        today=dt.datetime(2026, 8, 5, 12, 12, 33, tzinfo=dt.timezone(dt.timedelta(hours=3))),
    )
    assert "updated: 2026-08-05T09:12:33Z" in path.read_text(encoding="utf-8")


# --- the symptom that motivated the change ----------------------------------


def test_same_day_writes_produce_distinct_ordered_history(vault: Path) -> None:
    """Three writes in one afternoon must be orderable from the data alone.

    Exercised at the log layer rather than through `edit()`: a second edit of
    the same page trips an unrelated relation-disposition contract, and this
    test is about whether the recorded headings can be told apart, which is
    what changed. `test_edit_stamps_second_granularity_utc` covers the tool
    writing the stamp in the first place.
    """
    rel = NOTE_REL.removesuffix(".md")
    log_path = vault / "Knowledge Base" / "log.md"
    text = log_path.read_text(encoding="utf-8")
    for when in (
        dt.datetime(2026, 8, 5, 9, 0, 0, tzinfo=dt.UTC),
        dt.datetime(2026, 8, 5, 11, 30, 0, tzinfo=dt.UTC),
        dt.datetime(2026, 8, 5, 16, 45, 12, tzinfo=dt.UTC),
    ):
        text = vault_module.prepend_log_entry(
            text, date_iso=temporal.stamp(when), op="edit",
            rel_path_no_ext=rel, body="A revision.",
        )
    log_path.write_text(text, encoding="utf-8")

    dates = [e["date"] for e in vault_module.read_log_entries(vault, rel)]
    assert len(dates) == 3
    # The defect: these used to be three identical `2026-08-05` strings.
    assert len(set(dates)) == 3
    assert dates == sorted(dates, reverse=True)
    assert all(temporal.parse(d).precise for d in dates)


def test_history_headings_still_parse_at_day_granularity(vault: Path) -> None:
    """The widened heading regex must not orphan existing date-only history.

    A header this fails to match is dropped silently rather than raising, so a
    regression here would empty `read_memory(include_history=true)` with no
    error anywhere.
    """
    _seed(vault)
    edit_module.edit(
        vault, path=NOTE_REL, why="day-granular revision",
        heading="Overview", section_position="append", new_string="A point.",
        today=dt.date(2026, 8, 5),
    )
    entries = vault_module.read_log_entries(vault, NOTE_REL.removesuffix(".md"))
    assert [e for e in entries if e["date"] == "2026-08-05"]


def test_history_mixes_both_precisions(vault: Path) -> None:
    """A log written across the change carries both forms, and both read back."""
    rel = NOTE_REL.removesuffix(".md")
    log_path = vault / "Knowledge Base" / "log.md"
    text = log_path.read_text(encoding="utf-8")
    for stamp in ("2026-08-04", "2026-08-05T09:12:33Z"):
        text = vault_module.prepend_log_entry(
            text, date_iso=stamp, op="edit", rel_path_no_ext=rel, body="A revision."
        )
    log_path.write_text(text, encoding="utf-8")

    dates = {e["date"] for e in vault_module.read_log_entries(vault, rel)}
    assert dates == {"2026-08-04", "2026-08-05T09:12:33Z"}


def test_a_malformed_heading_is_the_silent_failure_mode(vault: Path) -> None:
    """Guard the shape of the risk: unmatched headings vanish, they do not raise.

    `read_log_entries` returns only what the header pattern matches, so a
    too-narrow regex would empty a page's history with no error surfacing
    anywhere. Pinning the silence makes the widened pattern load-bearing.
    """
    rel = NOTE_REL.removesuffix(".md")
    log_path = vault / "Knowledge Base" / "log.md"
    text = vault_module.prepend_log_entry(
        log_path.read_text(encoding="utf-8"),
        date_iso="2026-08-05T09:12:33+00:00",  # offset form: not what we write
        op="edit", rel_path_no_ext=rel, body="A revision.",
    )
    log_path.write_text(text, encoding="utf-8")
    assert vault_module.read_log_entries(vault, rel) == []


# --- no backfill -------------------------------------------------------------


def test_audit_fix_leaves_date_only_notes_byte_identical(vault: Path) -> None:
    """Acceptance: existing date-only notes are not rewritten. Ever.

    Restamping them as midnight would assert an intra-day time that was never
    recorded, so the repair pass must leave a compliant legacy page untouched.
    """
    path = _seed(vault)
    before = path.read_bytes()
    audit_fix_module.audit_fix(vault, today=dt.date(2026, 8, 5))
    assert path.read_bytes() == before


def test_audit_fix_copy_forward_preserves_the_instant(vault: Path) -> None:
    """Backfilling `updated` from `created` must not coarsen a known instant."""
    path = _seed(vault, body=f"""\
---
type: insight
status: active
created: 2026-08-05T09:12:33Z
sources:
  - "{SOURCE_LINK}"
---

# Missing Updated

## Claim

A stamped page whose `updated` was never written.

## Connections

- {SOURCE_LINK}
""")
    audit_fix_module.audit_fix(vault, today=dt.date(2026, 8, 6))
    assert temporal.parse(_frontmatter(path).get("updated")) == temporal.parse(
        "2026-08-05T09:12:33Z"
    )


def test_audit_round_trip_keeps_the_time_component(vault: Path) -> None:
    """Write a stamped note, run the audit paths, assert the time survives."""
    path = _seed(vault, body=f"""\
---
type: insight
status: active
created: 2026-08-05T09:12:33Z
updated: 2026-08-05T16:45:12Z
sources:
  - "{SOURCE_LINK}"
---

# Stamped

## Claim

A page recorded at second granularity.

## Connections

- {SOURCE_LINK}
""")
    audit_module.audit(vault, today=dt.date(2026, 8, 6))
    audit_fix_module.audit_fix(vault, today=dt.date(2026, 8, 6))
    fm = _frontmatter(path)
    assert temporal.parse(fm["updated"]).instant == dt.datetime(
        2026, 8, 5, 16, 45, 12, tzinfo=dt.UTC
    )
    assert temporal.parse(fm["created"]).instant == dt.datetime(
        2026, 8, 5, 9, 12, 33, tzinfo=dt.UTC
    )


def test_audit_ages_a_stamped_note_by_whole_days() -> None:
    """Staleness is a day-granular question; a timestamp must not break it.

    The space-separated case is what `str()` of a PyYAML-loaded datetime
    produces, which the previous 10-character prefix slice silently mangled.
    """
    assert audit_module._parse_fm_date("2026-08-05T16:45:12Z") == dt.date(2026, 8, 5)
    assert audit_module._parse_fm_date("2026-08-05 16:45:12+00:00") == dt.date(2026, 8, 5)
    assert audit_module._parse_fm_date(
        dt.datetime(2026, 8, 5, 16, 45, 12, tzinfo=dt.UTC)
    ) == dt.date(2026, 8, 5)
    assert audit_module._parse_fm_date("not-a-date") is None


# --- mixed vault -------------------------------------------------------------


def test_mixed_vault_validates_and_sorts(vault: Path) -> None:
    """Both precisions coexist, audit accepts both, and ordering is stable."""
    _seed(vault, rel="Knowledge Base/Notes/Insights/legacy.md", body=DATE_ONLY_BODY)
    _seed(vault, rel="Knowledge Base/Notes/Insights/stamped.md", body=f"""\
---
type: insight
status: active
created: 2026-01-01T09:12:33Z
updated: 2026-01-01T16:45:12Z
sources:
  - "{SOURCE_LINK}"
---

# Stamped

## Claim

A page recorded at second granularity.

## Connections

- {SOURCE_LINK}
""")
    audit_module.audit(vault, today=dt.date(2026, 8, 5))

    # Both pages read as complete: every required temporal field resolves to a
    # day regardless of which precision it was written at.
    for rel in ("legacy", "stamped"):
        fm = _frontmatter(vault / f"Knowledge Base/Notes/Insights/{rel}.md")
        for required in ("created", "updated"):
            assert audit_module._parse_fm_date(fm[required]) is not None, (rel, required)

    moments = [temporal.parse("2026-01-01"), temporal.parse("2026-01-01T16:45:12Z")]
    assert temporal.compare(*moments) is temporal.Order.INDETERMINATE
    assert sorted(moments, key=temporal.sort_key) == moments


def test_production_log_filename_keeps_its_month(vault: Path) -> None:
    """Note paths slice `YYYY-MM` off the render date, which stays day-granular."""
    assert temporal.render_date(
        dt.datetime(2026, 8, 5, 23, 59, 59, tzinfo=dt.UTC)
    )[:7] == "2026-08"
