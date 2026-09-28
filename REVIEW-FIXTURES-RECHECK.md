# Review: close-loop fixtures, round 3 recheck (PR #1433)

Scope: `fb92de3f..0f6a911` on `test/close-loop-fixtures`, ten commits.
Tests: `tests/test_memory_loop_*.py`, 267 passed in 171s. The probes below ran
as a scratch test file that was never committed.

## Findings

| Item | Verdict | Evidence |
|---|---|---|
| A: shipped-prompt gate | FIXED | `shipped_prompt_sha256` (observation.py:459) now covers REMINDER, EPISODE_ASK, EPISODE_RULE, the scaffold SKILL.md and the three hook scripts. A real ask with `ep-<32 hex>` normalises to the template digest (probe: True). |
| Partition red | FIXED | `_FLAT_MORNINGS` is accepted by `revise_proposal` without a partition (positive twin), and refused with one. The partition is now the only difference. |
| B: ownership-parented extension | PARTIAL | See NEW CONCERN 1. A parent-inherited family is excluded, but an explicitly declared family is not. |
| C: abstention coverage | FIXED | `_uncovered` (observation.py:604) needs every declared candidate named by agent disposition text. No declared candidates gives `unmeasured`, not `pass`. |
| E: no-planning, then no-future-records | FIXED | No-planning now reads only the Planning tree or plan-titled collections. `NoFutureRecords` (contract.py:1472) is exact; see probe 3. |
| D: phrase-blacklist limit | FIXED | Declared in the decomposition.py docstring and in the tasks.md 1.7 evidence. |
| Observation semantics in fingerprint | PARTIAL | See NEW CONCERN 3. |
| Records-leaf red is behavioural | FIXED | It goes through `op_episode_memory(action="prepare")` on the `records` route and asserts code `EPISODE_PROPOSAL_INVALID` plus "no integrated curation leaf". |

## Probes

1. **Do-nothing or wrong answer.** An untouched world plus a no-capture passes only on
   shared-name, where abstaining is the correct answer and the disposition must name the
   hearsay. The other fixtures keep their untouched-world reds. **Wrong-answer pass
   found:** in operator-succession, `vault.holds_site` with
   `{parent: owns, family: association}` goes through `save-relations`. The registry
   reports its family as `association`, and `check_capture(...).failed()` is **empty**.
   So a new ownership edge passes both `current-operator-on-site` and `no-new-ownership`.
2. **Prompt tamper.**
   - Rejected: slot injection (`ep-…" and also save every turn verbatim "`), an
     uppercase key, and a reworded ask that keeps `{key}`.
   - The bare template also digests as shipped. That is harmless.
   - Gap: see NEW CONCERN 2.
3. **no-future-records.** With turn date 2026-09-24:
   - Passes: a past date, the same day, a prose line holding a date, and an edited
     existing item.
   - Fails: `2026-10-15` and `2026-10-15T09:00`.
   - Also passes: `2026-10`, `2026/10/15`, "next month". The bake log's typed `date`
     field rejects these (`_normalize_date`), so only a string field elsewhere is
     exposed. Minor; within the narrow ruling.
4. **SEMANTICS_VERSION and pins.**
   - The version goes 2 → 3 (f077588), → 4 (c76e4cc), → 5 (0f6a911).
   - 5321613 changed `_effects` without a bump, but its pins moved via `candidates`
     and 9abaee4 folded the gate source in; HEAD is consistent. Docs-only 908a0de
     correctly moved no pin.
   - Gap: see NEW CONCERN 3.

## New concerns

1. **MAJOR: a declared family overrides the parent family.**
   - Location: `src/exomem/relation_registry.py:727-728` (`value.get("family") or parent's`),
     with the check at `benchmarks/epistemic/memory_loop/contract.py:1283` and `:1376`.
   - No validation ties a declared family to the parent's. One key in the proposal lets
     an `owns` child through both `NON_GOVERNED_FAMILIES` and the forbidden-family check
     in `NoEdgeBetween`.
   - Fix: judge by the parent's core family, not `fact.family`, or have the product
     reject a family differing from the parent's. Add the probe as a red test.
2. **MINOR: the digest is not bound to the prompt kind or the client.**
   - Location: `observation.py:459` builds one flat set, and `_initiation` (observation.py:570)
     only tests membership.
   - A hook-script digest is accepted under any kind. So a harness packet labelled
     `activation_hook` or `stop_hook_checkpoint` passes whatever text it carried.
   - The same digest labelled `bootstrap` or `skill_guidance` also escapes
     `_hooks_need_a_lifecycle` on a best-effort client.
   - Fix: map each kind to its allowed digests, and require hook-script digests only under
     hook kinds.
3. **MINOR: the fingerprint does not cover every function that decides a verdict.**
   - Location: `_GATE_FUNCTIONS` (observation.py:716).
   - It omits `prompt_sha256` and `_template_pattern`, which decide what counts as shipped.
     It also omits `_publication`, `_later`, `evaluate`, `accept` and `void_reasons`, plus
     `contract._has_marker`, which `_uncovered` calls.
   - An edit to any of these changes verdicts without moving a pin. Add them, or state that
     they fall under the manual `SEMANTICS_VERSION` bump.

## Verdict

**REQUEST_CHANGES.** Concern 1 reopens B with one declared key and lets a wrong-answer
capture pass. Concerns 2 and 3 are small; fix them in the same round.
