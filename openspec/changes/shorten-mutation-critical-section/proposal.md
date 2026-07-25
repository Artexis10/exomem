## Why

One governed `remember` currently performs FOUR full corpus validations and up
to two embedding-model touchpoints INSIDE the vault mutation boundary,
against a 5s acquire budget for waiters. A cold model load inside the lock
held the live boundary ~3 minutes on 2026-07-25. `eliminate-mutation-busy-failure-modes`
(R1) added the hold/wait telemetry that surfaces this distribution but
deliberately deferred fixing it. This change moves validation and model
loading before the boundary; the boundary covers only the commit.

## What Changes

- **Narrow guard per command** (`writer_lease.py`): a configurable
  `_NARROW_BOUNDARY_COMMANDS` set makes `remember`, `replace_memory`, and the
  existing-page family (`edit_memory`, `observe_memory`, `append_to_file`,
  `set_frontmatter_field`, `create_file`) acquire `writer_authority_guard`
  instead of the full mutation boundary as their `operation_guard`, mirroring
  the existing `narrow_media_commit` precedent. An `EXOMEM_WIDE_MUTATION_BOUNDARY`
  environment variable is the kill switch back to today's behavior.
- **Boundary moves into the commit seam** (`semantic_writes.py`):
  `commit_creation` and `commit_existing` acquire the vault mutation boundary
  around their entire bodies (including `ensure_manifest` and
  `vault_creation_lock`), so corpus validation, relation-review evaluation,
  and embedding-model touchpoints that currently run pre-commit inside the
  old wide boundary now run before any boundary is held at all.
- **Census validity token** (`semantic_contract.py`): a new
  `corpus_validity_token(root)` extends the existing corpus census
  (`_corpus_census`) with a scandir-census of the relation-review artifact
  and lifecycle sidecar directories, closing the one input class the plain
  corpus census does not cover. `None` on anything that cannot be censused
  cheaply, which forces full in-boundary revalidation rather than reuse.
- **Bounded in-boundary revalidation**: preflight results captured before the
  boundary carry a `census_token`. On commit, a fresh token is compared; an
  exact match reuses the pre-boundary corpus build/evaluate/validation
  results (identity, artifact-reservation, and destination-occupancy checks
  always re-run fresh, token match or not); a mismatch falls back to a full
  warm revalidation, bounded at today's worst-case cost.
- **Model pre-warm**: best-effort embedding-model load moves before the
  boundary in both commit paths, so a cold load can no longer hold the
  boundary for the multi-minute duration observed in the 2026-07-25 incident.
- **Telemetry**: `exomem_prevalidated_commit_total{outcome="reused"|"revalidated"|"unavailable"}`
  plus a log event; `exomem_boundary_hold_ms` and the mutation journal's hold
  fields (from `eliminate-mutation-busy-failure-modes` R1) are the acceptance
  measurement for the narrowed hold.

## Capabilities

### Modified Capabilities

- `hosted-mutation-safety`: the mutation boundary for narrowed commands now
  covers only the commit seam — canonical write, index/log updates, and
  fencing — not corpus validation or model loading; reuse of pre-boundary
  validation is permitted only under an exact census-token match; a mismatch
  falls back to bounded in-boundary revalidation with unchanged verdicts;
  pre-boundary validation failure never acquires the boundary at all;
  `MUTATION_BUSY` wire shape and fencing guarantees are unchanged.

### New Capabilities

- `write-latency`: bounded boundary-hold semantics for governed semantic
  writes, the census-token validity contract that governs safe reuse, and
  revalidation-outcome telemetry.

## Impact

- `src/exomem/writer_lease.py` — `_NARROW_BOUNDARY_COMMANDS`, narrow-guard
  predicate, `EXOMEM_WIDE_MUTATION_BOUNDARY` kill switch.
- `src/exomem/semantic_contract.py` — `corpus_validity_token(root)`.
- `src/exomem/semantic_writes.py` — `census_token` on `CreationPreflight`/
  `ExistingPreflight`; narrowed guard, token check, and revalidate-on-mismatch
  in `commit_creation`/`commit_existing`; embeddings pre-warm.
- `src/exomem/relation_review.py` — `prepare_commit_creation_draft` split out
  of `commit_creation_draft`; `_attempt(reuse=...)`; `_PrevalidatedAttempt`.
- Metrics/log events — the new prevalidated-commit counter, using existing
  observability helpers only.
- No changes to `mutation_lock.py`, `vault.py`, `tests/golden/`, `.github/`,
  or `tests/test_latency_gate.py`.
- No new runtime dependency. No change to what the semantic contract accepts
  or rejects — only when validation runs relative to the mutation boundary.
- This change lands in independently gated commits (docs, pure refactor,
  narrow `remember`, extend to the existing-page family, cleanup); see
  `tasks.md`. Turn 1 covers only the docs and the pure refactor — the guard
  narrowing itself (and therefore any observable boundary-hold change) lands
  in a later commit.
