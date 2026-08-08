## Context

The handoff brief (`docs/handoff-note-timestamps.md`, branch `worktree-bench-foundation`)
records the decisions taken before implementation: second granularity, no backfill, permanently
mixed format, timezone-aware. Those are settled. This document covers the design consequences,
including three that the brief did not anticipate and three of its hazard calls that turned out
to be wrong.

## Goals / Non-Goals

**Goals.** Record knowledge time precisely enough to order same-day writes. Keep every existing
date-only value untouched. Make the read paths honest about which orderings are actually
determined.

**Non-Goals.** No backfill or migration. No sub-day capability in `recency_days`, which is
day-scoped by construction. Not storing the author's local timezone. The benchmark-side
sub-day family is task 4.5 of `expand-memory-proof-benchmark` and lives on another branch.

## Decisions

### Precision is carried by the Python type

`datetime` subclasses `date`, and PyYAML already returns a `date` for a bare frontmatter value
and a `datetime` for a timestamped one. Using that distinction as the precision marker means one
representation works on both sides of the system:

- **Reading**: `isinstance(value, datetime)` is exactly "the instant is known".
- **Writing**: the existing injectable `today: dt.date | None` seam accepts a `datetime` with no
  signature change. A caller passing a plain `date` gets date-only output.

The consequence is that the ~394 existing `today=dt.date(...)` test call sites keep passing a
date and keep getting the day, so their assertions stay true without being rewritten. Only tests
that exercise the new path pass a `datetime`. This is what kept a change touching ten write
tools from turning into a suite-wide migration.

The alternative — adding a parallel `now: datetime | None` seam — would have meant either
threading two clocks through every leaf or a conditional contract ("date-only when `today` is
passed, otherwise an instant") that is easy to get wrong and hard to state.

### `Moment` makes the unknown explicit

`temporal.parse` returns `Moment(day, instant | None)`. The `None` is load-bearing: it is the
difference between "recorded at midnight" and "recorded that day, time unknown". Every ordering
decision reads it.

`compare` returns four values. Its rule is short — distinct days always order; within one day
only two known instants order — and it is antisymmetric, which the tests pin exhaustively
against the brief's table.

`sort_key` exists separately because UIs need a total order. Keeping it distinct from `compare`
is deliberate: a caller that wants a list gets one, and a caller that wants to know whether the
order is real has to ask a different question and cannot get a false answer by accident.

### UTC for stamps, local day for paths

Stamps are UTC so values order across machines. Paths are not, because `dt.date.today()` is
local and note filenames (`YYYY-MM-<slug>`) have always used the local day. Folding the path
date to UTC would silently move a note written at 01:30 at UTC+3 into the previous month.

So the clock seam returns a local-offset-aware `datetime`, `stamp()` converts to UTC, and
`render_date()` reads the day as given. One clock read, two correct derivations.

### The draft token freezes the instant, not just the day (token v2)

The first attempt stamped knowledge time at commit, reasoning that it should record when the
write actually happened. The full suite disproved it. `test_semantic_creation_writers.py:823`
validates on day N, commits on day N+1, and requires `created` to be **day N** — the token
deliberately freezes the authored date so that committing the same token twice produces
identical bytes, which is what the mutation-receipt replay path depends on.

So the token now freezes both: `render_date` (the authored day, which names the file and must
stay day-granular for the `[:7]` slice and the `date.fromisoformat` round-trip gate) and
`render_stamp` (the authored instant, written into `created`/`updated`). `_TOKEN_VERSION` goes
to 2 and v1 tokens are rejected — a v1 token carries no stamp, and inferring one at commit is
exactly the non-determinism the freeze exists to prevent. The cost is that a client mid
validate→commit at the release boundary gets `INVALID_DRAFT_TOKEN` and re-validates once.

`render_stamp` is declared **after** `registrations` in the dataclass and passed by keyword
everywhere. Inserting it next to `render_date`, where it belongs conceptually, silently
rebound the fifth positional argument at four existing call sites — the tuple of registrations
landed in the stamp field and only surfaced as a JSON-serialization error. Field order in a
dataclass is an API surface.

### Reproducibility is a system invariant here, not an implementation detail

The same root cause produced a third failure: `test_relation_queue_commands.py:172` compares two
governed writes byte-for-byte to prove the accept path and the studio path agree. Reading the
clock at each write made them differ whenever they straddled a second boundary — **flaky by
construction**, which is worse than a clean break because it passes most runs.

The general rule this change adopts: a recorded instant is frozen once per logical operation and
carried, never re-read per write leaf. Where two executions are genuinely being compared, the
test pins the clock — newly cheap, because consolidating on `temporal.now` gave the suite a
single seam to monkeypatch where before there were seven scattered `datetime.now` calls.

### Idempotency keys stay day-granular

`commit_edit` mixes the recorded date into a log-write `operation_token` that makes a replayed
edit idempotent. A per-second value would mint a fresh key on every attempt and defeat the dedup
entirely, so `commit_edit` receives the stamp for the heading and derives the day for the key.

## Corrections to the handoff brief

The brief's line numbers are stale throughout, and three of its five hazard calls are wrong.

**`audit._parse_fm_date` and `find_policy.parse_date` are not bugs.** The brief says their
10-character truncation makes the change "cosmetic". Both consumers are genuinely day-scoped —
staleness aging and day-window recency filtering — so collapsing to the day is correct there,
not a loss. They now delegate to the shared helper for consistency and to pick up the quoted and
space-separated spellings that prefix-slicing mangled, but their behaviour is deliberately
unchanged. `tests/test_audit_distillation.py` already pins the day-collapse including the
`datetime` case and stays green.

**`audit_fix._as_iso_date` is not "verified safe".** `datetime` subclasses `date`, so the
`isinstance(value, dt.date)` branch returned the full timestamp string, which `_backfill_value`
then copied into a neighbouring field and `_apply_frontmatter_fix` wrote unquoted. Separately,
`date.fromisoformat` raises on a timestamped `started`, silently abandoning the experiment
`duration` backfill.

**The real silent-drop bug is in `structured_filters`, which the brief never mentions** — and it
predates this feature. See the proposal.

Three couplings the brief missed: the draft-token render-date gate, the `log.md` heading regex
(which is where its own observed symptom actually lived), and the `commit_edit` idempotency key.

## Risks

**Search results change.** Pages previously dropped from date filters now appear. That is the
fix, but it is user-visible and worth saying out loud in release notes.

**Two spellings forever.** Every future reader of these fields must go through `temporal`.
The mitigation is that there is now exactly one place to go through, where before there were
three private and mutually inconsistent parsers.

**Clock skew between machines** can misorder writes seconds apart. Second granularity is the
honest ceiling on what a wall clock can back; this is why the brief rejected milliseconds.
