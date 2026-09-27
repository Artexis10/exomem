# Tasks

## 1. Visible vocabulary guidance failures

- [x] 1.1 Test first: a swallowed after-commit guidance failure and a recovery failure log a
      warning naming the exception type, without the exception message.
- [x] 1.2 Log both by exception type and raising location.
- [x] 1.3 Reproduce `guidance_unavailable` on a realistic fixture (3,000 pages, several
      thousand tags, legacy tag shapes, pages without semantic units) and identify the cause:
      an unpublished graph snapshot, reported under a collapsed reason.
- [x] 1.4 Test first, then keep closed projection reasons in `vocabulary_sync`.

## 2. Shared fold

- [x] 2.1 Test first: variant sets share one key; exceptions and short words are untouched;
      the fold is idempotent.
- [x] 2.2 `vocabulary_fold.fold_term` and `EXCEPTIONS`.

## 3. Write-time reconciliation

- [x] 3.1 Test first: `maximal` records the canonical tag; other levels keep the authored
      tag; an unavailable catalogue fails open.
- [x] 3.2 Catalogue tag-usage aggregate and a briefly cached folded index.
- [x] 3.3 Hook `note`, `edit` and `add` tag cleaning.
- [x] 3.4 One-line `tag-variant/v1` advisory in the existing slot, validated on projection,
      yielding to a relation review notice and to an `off` envelope.

## 4. Maintenance

- [x] 4.1 Test first: preview lists groups and counts and writes nothing; confirmed apply
      rewrites only the tags key, skips Sources, logs once, and converges; a stale plan is
      refused; batches are bounded.
- [x] 4.2 `maintain_memory(mode="tag-variants")`, its egress selector, and remote admission
      matching `structured-files`.

## 5. Delivery

- [x] 5.1 Regenerate derived tool-surface artifacts.
- [ ] 5.2 After merge, run the preview on a live vault, record before/after group and use
      counts, then sync this delta into the canonical specs and archive the change.
