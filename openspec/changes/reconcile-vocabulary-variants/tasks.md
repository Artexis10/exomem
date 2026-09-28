# Tasks

## 1. Visible vocabulary guidance failures

- [x] 1.1 Test first: a swallowed after-commit guidance failure and a recovery failure log a
      warning naming the exception type, without the exception message.
- [x] 1.2 Log both by exception type and raising location.
- [ ] 1.3 Identify the live `guidance_unavailable` cause from the warning logs and the
      closed reason now named in `vocabulary_sync`. Steps 1.1, 1.2 and 1.4 added logging and
      reason naming only; no committed test reproduces the live cause.
      Observed 2026-09-28 on the live personal worker, read-only: in 0.93 the reason came only
      from a raised exception, since projection states then reported `projection_unavailable`.
      The retained log starts 2026-09-27 22:14Z, after that write. The 0.96.0 worker started
      06:12Z; 30 committed writes followed, including `remember`, `edit_memory` and
      `record_memory`. None logged `vocabulary guidance unavailable` or
      `vocabulary tag advisory unavailable`. The recovery queue drained nine jobs at 06:22Z.
      The cause is not reproduced and no defect is shown. Close this at the 5.2 live preview
      if the worker log still names no exception class; a logged class reopens it as a
      red-first fix.
- [x] 1.4 Test first, then keep closed projection reasons in `vocabulary_sync`.

## 2. Shared fold

- [x] 2.1 Test first: variant sets share one key; exceptions and short words are untouched;
      the fold is idempotent.
- [x] 2.2 `vocabulary_fold.fold_term` and `EXCEPTIONS`.
- [x] 2.3 Test first: a must-not-merge table (`training`/`trains`, `recording`/`records`,
      `embedded`/`embeddings`, …) and a must-merge table; then drop `-ing`/`-ed` folding and
      add NFKC and hyphen runs.

## 3. Write-time reconciliation

- [x] 3.1 Test first: every level keeps the authored tag; `maximal` warns with the
      canonical tag; an unavailable catalogue fails open.
- [x] 3.2 One count source: per-page catalogue tags filtered by visibility and owned trees,
      counted per writer normal form, ties undecided; a briefly cached index for unrestricted
      readers, including an empty one.
- [x] 3.3 Hook `note`, `edit`, `add` and `link` tag cleaning; `add` surfaces its warnings.
- [x] 3.4 One-line `tag-variant/v1` advisory in the existing slot, validated on projection,
      yielding to a relation review notice and to an `off` envelope.

## 4. Maintenance

- [x] 4.1 Test first: preview lists groups and counts and writes nothing; confirmed apply
      rewrites only the tags key, skips Sources, logs once, and converges; a stale plan is
      refused; batches are bounded.
- [x] 4.2 `maintain_memory(mode="tag-variants")`, its egress selector, and remote admission
      matching `structured-files`.
- [x] 4.3 Test first: owned trees (Records, Planning, `_Adoption`, workflow contracts, …) are
      never counted or rewritten; withheld pages count exactly like absent ones; 1:1 and 3:3
      ties are never rewritten; the canonical is a written form.
- [x] 4.4 Test first: apply plans outside the mutation guard and re-verifies batch hashes,
      visibility and group decisions under it; the log keeps per-page before/after tags and
      the inverse mapping, and apply refuses when it cannot be written; the splice quotes
      YAML-ambiguous tags, keeps a `tags:` line comment, and refuses to drop a block comment or
      change another key.
- [x] 4.5 Test first, through the dispatcher: tag-variants apply plans without the served
      writer lock; `exclude_groups` is hashed into `plan_id` and leaves other groups applied;
      unrestricted counts come from one SQL aggregate matching the row path, the cache holds
      counts only, and the guard re-verifies only rewritten keys; maximal advice is emitted
      once and a restricted write builds its index once; post-commit advice runs with the
      request principal bound.

## 5. Delivery

- [x] 5.1 Regenerate derived tool-surface artifacts.
- [ ] 5.2 After merge, run the preview on a live vault, record before/after group and use
      counts, then sync this delta into the canonical specs and archive the change.
