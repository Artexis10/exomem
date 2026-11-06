## Why

A cold corpus-context build confirms its census up to three times after the
build, and the cache keeps the context only when two walks agree. Beside a
writer that never pauses, no two walks agree. That happens when a standby warms
during an online upgrade while the serving worker keeps writing. The build was
thrown away and the standby warm-up failed with "semantic corpus warm-up did not
retain a current context". The promoted worker then paid the whole cold build
again, about a minute on a live vault, while it refused writes.

The same build loses the same way as vault size and write rate rise: each extra
walk widens the window for the next write. The canonical requirement forbids
storing a raced context at all, which is stricter than what correctness needs.
Correctness needs only that no caller receives a cached context before a census
walk confirms it.

## What Changes

- A cold build whose census never settles is kept with the last census that it
  was reconciled to, without an event caption.
- The next caller must confirm that entry with its own census walk. A matching
  walk reuses it, and a Markdown-only difference reparses only the changed pages.
  No event reuses the entry without a walk.
- Corpus warm-up accepts a kept build, so a standby that warmed under steady
  writes carries its corpus to promotion.
- The builder still returns no census for an unsettled build, so callers that pin
  a census to a context see no change. Callers that fall back to the cached census
  may now issue a validity stamp after an unsettled build; the stamp never compares
  the corpus census.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `write-path-resilience`: a raced build may be kept for confirmation by a later
  census walk, but it is never served as current without that confirmation.

## Impact

- Code: `semantic_contract._build_corpus_context_with_census` and
  `semantic_contract.warm_corpus_context`.
- Tests: `tests/test_corpus_context_cache.py`.
- No change to the flight-join bound or to event-captioned reuse. Warm-up now marks `semantic_corpus` ready after an unsettled build, because the kept entry serves the next reader.
