# VERIFY #1444: integration/wave-bcd at d0cc8483

Read-only, against `origin/main` (8747d049, one release bump ahead; merges cleanly).

## Verdict: HOLD

Two defects exist only where lanes meet. Both are reproduced at d0cc8483.

## Blockers

**B1. The identity distinct decision and the CJK alias guard block each other.**
In `src/exomem/link.py:908-983`, a single
`identity_decision.candidate_fingerprint` has to decide either the title or one
claimed alias, never both.

- Creating an entity whose title and an alias are both already claimed (a
  homonym reusing its short alias, which is the identity lane's own case) can
  never succeed:
  - the title fingerprint is refused at `link.py:977` with the alias's fingerprint;
  - that alias fingerprint is refused at `link.py:947` with `ENTITY_EXISTS`.
- On main, the same create succeeds.
- An `aliases` patch adding two claimed aliases alternates between
  `ENTITY_EXISTS` and `STALE_IDENTITY_DECISION` (`commands.py:7361-7405`).
- Fix: accept one decision that covers the title and every claimed alias.

**B2. Narrowing drops a candidate the turn named, when a CJK word is glued to a Latin one.**
- `name_contact` includes CJK embedded words, but `_name_span` counts only
  `analysis.tokens` (`working_set_resolve.py:1404-1408`).
- `_narrowed_by_qualifier` (`:2192`) therefore compares spans that miss the
  glued word.
- Example with hubs "Tide model rollout" and "Alpha tide model":
  - "check alpha tide model rollout please" → ambiguous (correct);
  - "check alpha tide model rolloutの計画" → **resolved to "Alpha tide model"**.
- Fix: compute spans over word positions, or skip narrowing when the contact came
  from `analysis.words`.

## Checks

**1. Merges.** I reproduced all 19 merges.
- Ten are conflict-free and their trees equal the automatic merge.
- The conflicted ones keep both sides:
  - 5a3f2ee4, ba7aec29: the latency proof now runs on main's 2-worker executor;
  - cbb61231, 8d2eae52, f3bfe7be;
  - the others touch only stamps, locks or task renumbering.
- No lane content was lost.
- Merged heads match approved heads, except:
  - identity: +1 commit, which only changes a test count;
  - sensed: 6e9ba338 landed after approval;
  - resolver: d4a80497 landed after approval.
- I reviewed both post-approval commits here and they are sound:
  - the redrain is bounded to 8 probes, and a new publication restarts it;
  - the new zero-count status counts only visible partners (`sensed_model.py:1375`).

**2. Cross-lane.**
- Resolver × CJK × narrowing: B2.
- Identity × CJK alias: B1.
- Episode slice 2 × fixtures: OK. Validation reads the live `_ROUTES` and
  `_DISPOSITIONS`, and the repins pass.
- Warm-up × writer lease: OK. `_warm_request_path` is read-only and its handles close.
- Sensed × withheld: OK for `epistemic_status`, `vocabulary_resolution` and
  `distinct_from`, which all use visible-filtered sets.
- Combined lane tests: 2446 passed, 15 skipped.

**3. Bootstrap trim 59783f7f.** Wording only; no rule was dropped.
- `envelope.py:126`: "the confirm-required surfaces are" → "confirm-required:", and
  one sentence was split.
- `prominence.py:188`: preserves→keeps, requires→needs, and "No handle means",
  "Missing schema uses" and "relations use" became colon forms.
- `prominence.py:392`: "not only the ones"→"not only those", "Only skip for
  pure"→"Skip only pure".
- Every pinned token is still present, and 677 tests pass.

**4. Hosted.**
- v1, v2, v3 (Claude), v4 and v4-command-binding have identical tree ids on main
  and d0cc8483.
- Only the unreleased v5 compatibility and lock files change.
- The tool-surface contract and `pending_tool_surface_sha256` move together
  (13e96087→d133db5e). The registered sha is unchanged.
- The capabilities, harness-modules and privacy-fixture `--check` scripts pass,
  and 528 tests pass.

**5. OpenSpec.**
- `openspec@1.10.0 validate --all --strict`: 224 passed, 0 failed.
- Archive discipline: 93 active changes, none task-complete.

**6. Benchmarks.**
- The o200k table was rebuilt from `js-tiktoken@1.0.21`, sha256 `446a9538…1a2d` ✓.
- Real-compiler and continuity suites: 76 passed. The recorded reports equal
  the fresh runs.
- Raw score: 9/18 (C5 C6 C7 T2 T3 T4 T5 T7 T9). A2+A4, A7, A8 and A9 are each
  9/18. Continuity v3: 4/4. Both as expected.

**7. CI.**
- 37 runs on d0cc8483: 26 success, 11 conditionally skipped, 0 failed.
- `required CI gate` passed. Local privacy gate and ruff are clean.

## Non-blocking follow-ups

- `learned_aliases` bypasses the claim guard, which runs only when the field is
  `aliases` (`commands.py:4373`). A registry facet named `learned_aliases` does the
  same at create time. Afterwards, a turn resolves both entities by `exact_alias`.
  This is a gap in the new guard, not a regression; fix it with B1.
- A decided alias is not remembered: a later patch that keeps it is refused again.
- `capture-identities-at-write-time` tasks 5.1-5.3 say to archive in the same
  delivery. Tick them and archive at merge.
- `fa5311ee wip:`: the attachments review asked for a squash; squash-merging
  #1444 satisfies it.
- Bootstrap headroom: 19 bytes above its floor (63,025 of 63,300).
- A CJK name reached only by containment has no `name_contact`, so
  "山田さんから連絡" never asks between the two 山田 entities.
- The bare-name ambiguity groups count unfiltered neighbourhoods, which might hint
  at a withheld anchor. Not verified.

## Recheck at efe52086

Scope: B1 and B2 only, rerun with the original reproductions, plus review of
eb4b227d and efe52086.

**Verdict: SHIP.**

**B1: fixed** (eb4b227d).
- `entity_candidates.claim_set_fingerprint` hashes the union of the title's
  candidates and each claimed alias's claimants. `link.py` and
  `_refuse_claimed_aliases` both bind to it.
- Create with title and alias both claimed: CREATED (was never creatable).
- Patch adding two claimed aliases: accepted with one decision (previously
  alternated with `STALE_IDENTITY_DECISION`).
- A decision made for only some of the names, or before a claimant changed, no
  longer matches and is refused as stale.
- The memory-loop contract and the spec delta follow the new binding.

**B2: fixed** (efe52086).
- A contact resting on an embedded-only term gets no `name_span`, so
  `_narrowed_by_qualifier` never narrows it.
- "check alpha tide model rolloutの計画" and "alpha tide modelのrolloutの計画" are
  now ambiguous and keep both hubs (previously wrongly resolved).
- The all-Latin turn is unchanged, and the 山田 cases are unchanged.

**Regression checks**
- The same 72 combined lane files, plus the real-compiler, continuity and audit
  suites: 2466 passed, 15 skipped, 0 failed.
- The recorded reports equal the fresh runs: raw 9/18 and continuity 4/4 are
  unchanged.
- Gates clean: ruff F, capabilities `--check`, harness-modules `--check`, the
  privacy gate, and `openspec@1.10.0 validate --all --strict` (224/224).
- Archive discipline: OK.
- CI on efe52086: `required CI gate` success, all other runs success or
  conditionally skipped.

**Still open (non-blocking, unchanged from above)**
- The `learned_aliases` bypass of the claim guard.
- A decided alias is refused again on a later patch that keeps it.
