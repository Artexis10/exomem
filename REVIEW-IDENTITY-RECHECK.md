# Identity batch 1: correction-round recheck (PR #1430)

This review covers the commits after `e498a976`, up to head `709d11cb`. I read every file those commits touched and did not modify the reviewed branch.

## Findings

**F1 (FIXED).** The maximal block is now 1,483 B and balanced is 1,494 B. No capture class was dropped. `tests/test_capture_sweep.py:567` pins both sizes and requires at least 16 B of headroom; it passes.

**F2 (FIXED).** `entity_candidate` now filters by `writer_link_visibility` (`capture_sweep.py:400`) in three places:
- context resolution (`:419`);
- the registry, which drives both `resolves` and `near_matches` (`:401`);
- the dependency sources, filtered before the row cap (`:447`).

`hints()` shares the filtered registry. `dependency_sources_for_bare_name` has no cap, so withheld rows cannot change its status.

**F3 (FIXED).** `writer_lease.py:4371` refuses an uncommitted vocabulary-bound leaf that carries an `identity_preparation`. The error is `IDENTITY_DECISION_REQUIRED` with the fingerprint. A retry with the same key repeats the refusal, and the item stays `applying`, never `applied`.

**F4 (FIXED).** `envelope.py:130` now carries "on a personal vault". The supersession clause still says the gate is absent and is future work.

## F2 attack

I wrote my own A/B/C harness, separate from the PR's test, and ran it for both restricted audiences. In each case the restricted writer runs `remember` with a `[[Zed Corp]]` link, and I compared the serialized `entity_candidate`:

| Scenario | Result |
|---|---|
| Withheld page is the only prior linker | A = B = C = `null` |
| Withheld second linker beside a visible one | A = B = C = block |
| Withheld Entity titled `Zed Corp` | A = B = C = block |
| Withheld note titled `Zed Corp` | A = B = C = block |

All 12 cases were byte-identical. When I reverted the registry filter, the Entity case went red for both audiences, so the check is not vacuous.

Harness caveats:
- **Owner writes canonicalise links.** If the owner writes the visible linker after a same-title withheld page exists, the owner's write rewrites the link to the withheld path. A's visible bytes then differ from B's, so that world is not a valid twin. I wrote the visible linker first.
- **Hand-seeded pages produce no block.** Real writes are required to build the worlds.
- **`near_matches` is never exercised.** Neither harness made it non-empty, even with the filter reverted.

## Commands

The requested pytest command, plus `tests/test_vocabulary_application.py`:
- 308 passed, 1 failed.
- The failure is `test_the_compact_bootstrap_budget_is_not_raised`, which raises `No module named 'tests'`. It is an import-path artifact.
- With `PYTHONPATH=.`, that test and `tests/test_vocabulary_application_arguments.py` pass: 14 passed.

## New concerns (non-blocking)

1. **Pre-existing: `capture_sweep.py:438`.** `_attachment_exists` probes the filesystem without `visible`. If a restricted writer links a guessed file path inside a withheld scope, and a visible page also links the name, the block disappears only when the file exists. That reveals whether a withheld file exists at that path.
2. **`vocabulary_application.py:416`.** A refused shared-name item stays `applying`, which the review index still treats as actionable, and it has no reviewed route to success. The operator must defer or reject it. Worth documenting.
3. **`tests/test_vocabulary_application.py:664`.** The assertion `!= "applied"` is weaker than the claim that the item stays retryable. Assert `== "applying"` and add a same-key retry.
4. **`tests/test_derived_identifier_egress.py:612`.** Add a twin with a non-empty `near_matches`.

## Verdict

**APPROVE.** F1–F4 are fixed, and the F2 twin holds in both directions for both audiences.
