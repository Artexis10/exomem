# Handoff — second-granularity timestamps for note knowledge-time

Self-contained brief for a fresh session. Everything needed is here; no prior
conversation context is assumed.

## The task

Exomem records note `created` / `updated` as bare calendar dates. Move to
**second-granularity, timezone-aware timestamps going forward**, without
rewriting history.

## Why this matters

Exomem's differentiator is bitemporal reasoning — world time × knowledge time.
It currently records its own **knowledge time to the day**. The recording
granularity sits below the capability being claimed.

Observed directly: a note created and revised twice inside ~2 hours has three
history entries all reading `2026-08-05`, with `created == updated`. The only
ordering signal is array position, not data. Questions like "has this page
changed since I read it?" and "in what order did I reach these conclusions?" are
unanswerable within a day.

Neither test suite catches this. The memory-proof benchmark models knowledge time
in *weeks* (`recorded_week`), so it is structurally blind to sub-day ordering.

KB note: `Knowledge Base/Notes/Failures/exomem-records-note-knowledge-time-to-the-day-not-to-the-second.md`

## Decisions already made — do not relitigate

1. **Second granularity.** Not milliseconds: false precision for hand-authored
   notes, and second-level already orders anything a human or agent does
   sequentially.
2. **No backfill.** Existing date-only values stay date-only. Rewriting them as
   midnight timestamps would assert a precision that was never captured — it
   manufactures data. Old notes genuinely have unknown intra-day time and the
   schema should say so.
3. **Mixed format is permanent**, not a migration window. Readers must handle
   both indefinitely.
4. **Timezone-aware.** A naive local timestamp is unorderable across machines.

## The design question that actually matters

Because of decision 2, **precision becomes part of the data**, and comparison is
no longer total. A date-only value denotes *an unknown instant within that day*;
a timestamp denotes a specific instant. So:

| a | b | result |
|---|---|---|
| `2026-08-04` | `2026-08-05T09:00:00Z` | a before b — the whole day precedes |
| `2026-08-05` | `2026-08-05T09:00:00Z` | **indeterminate** — cannot be ordered |
| `2026-08-05` | `2026-08-05` | **indeterminate** |
| `2026-08-05T09:00:00Z` | `2026-08-05T11:00:00Z` | a before b |

**Recommendation:** a comparison helper returning four-valued
`before | after | same | indeterminate`, never collapsing an unknown into a
guess. This is the same principle the benchmark's scoring layer already uses —
*unsupported-never-zero*: when the data cannot justify a verdict, report that it
cannot, rather than fabricating one. Sorting UIs may fall back to a stable
secondary key, but the ambiguity should be surfaced, not hidden.

Push back if you disagree, but argue it against the table above.

## Hazards found — audit these, they are the real work

The write path is one line. The value lives in the read and comparison paths.

- **`src/exomem/audit.py:952`** — `dt.date.fromisoformat(value.strip()[:10])`
- **`src/exomem/find_policy.py:236`** — `date.fromisoformat(str(value).strip()[:10])`

Both already *tolerate* a timestamp (no crash) but truncate to 10 characters,
collapsing every comparison back to day granularity. **If these are left as-is
the change is cosmetic** — timestamps get written and then ignored. Sweep for
other `[:10]` / `fromisoformat` sites on frontmatter values.

- **`src/exomem/adoption_run.py:736`** — `str(plan.get("created") or "")[:10]`,
  same truncation shape (adoption plans, not notes — check whether it matters).
- **`src/exomem/audit_fix.py:177` `_as_iso_date`** — verified **safe**: strings
  pass through unchanged, and `datetime` subclasses `date` so `.isoformat()`
  preserves the time component. No change needed, but pin it with a test.
- **`src/exomem/note.py:12`** — production-log filenames derive `YYYY-MM-<slug>`
  from `created`. Must keep working when `created` carries a time.

## Files to change

- **Write:** `src/exomem/note.py:1070` (`created:`), `src/exomem/edit.py:213-224`
  (`updated:`), `src/exomem/replace.py:99-100`. All currently
  `dt.date.today().isoformat()`.
- **Schema/validation:** `src/exomem/audit.py:1115-1121` lists `created`/`updated`
  as required for seven note types (`research-note`, `insight`, `failure`,
  `pattern`, `experiment`, `production-log`, `entity`). Validation must accept
  both forms.
- **History entries** carry the same date-only field — include them.
- Precedent for timestamps already in-tree: `adoption_run.py:627` `_now_iso()`,
  `review_state.py:202` timezone-aware `datetime`. Reuse rather than invent.

## Benchmark-side work (same change, do not skip)

The benchmark should be able to *measure* sub-day temporality — otherwise this
capability ships untested and we repeat the mistake that hid the gap.

- Corpus knowledge time is `recorded_week: int` (0..11) in
  `benchmarks/membench/schema.py`. Sub-day ordering needs finer recorded time on
  `Assertion` / `StatusSpan`.
- Proportionate scope: a family where two records land on the **same day** and
  their order determines the correct answer — a system that only knows the day
  cannot answer, and must abstain rather than guess.

**Timing constraint, important:** this changes generated corpus bytes. It must
land **before** the packaging lane pins v0.2 release bytes (OpenSpec tasks
4.1/4.2 in `openspec/changes/expand-memory-proof-benchmark/`). Doing it after
means reissuing the release.

## Constraints

- Work in a dedicated worktree off `origin/main`; never the shared primary
  checkout. Report the path in the first progress update.
- Conventional-commit messages. **No `Co-authored-by:` trailers for any AI tool
  and no AI session URLs** in commits, PR titles/bodies, or release notes.
- Do not push or open PRs without explicit approval.
- Lean test command:
  `EXOMEM_DISABLE_EMBEDDINGS=1 uv run pytest tests/ -q --timeout=60`
- Follow repo contribution instructions and OpenSpec conventions; validate any
  OpenSpec change with `openspec validate <name> --strict`.

## Acceptance

1. New notes carry second-granularity timezone-aware `created`/`updated`;
   existing date-only notes are **unmodified** — assert this explicitly.
2. Comparison helper implemented with the four-valued result, unit-tested against
   every row of the table above.
3. Every truncation site above either fixed or shown to be harmless with a test.
4. Round-trip test: write a timestamped note, run the audit and audit-fix paths,
   assert the time component survives.
5. Mixed-vault test: a vault containing both formats validates, sorts, and
   reports indeterminate ordering where it genuinely exists.
6. Benchmark family added, corpus regenerates deterministically, and a
   day-granularity system provably cannot pass it.
