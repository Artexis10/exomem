# Review: PR #1429, context-activation round 2 (benchmark integrity)

Reviewed `53ac15a2..e9b4fad`. The reviewed branch is not modified.

## Verdict: REQUEST_CHANGES

Frozen inputs hold, the raw score is honest, and the recorded reports reproduce. A4 goes beyond its ruling, and the continuity gold lets wrong packets pass. No recorded number moves today (raw = amended = 5/18; K1–K4 4/4).

## Findings

**F1 (high): A4 also waives the status check, so adding poison can turn a fail into a pass.**
`benchmarks/membench/utility/context_activation.py:717`: `hedged = (...) or bool(hedged_poison)`. `hedged` suppresses the status-mismatch failure. B3 (comment :700–709) says a twin with its own gold is never credited as hedging, and A4 applies only to such twins. Reproduction with `score_case`:
- T7, abstained, partial gold `c7_hub_feature` + partial poison `c7_hub_market`: **passes under A4** with no failure reasons, though T7 expects `resolved`.
- T8, abstained, partial gold only: fails on status. Add a partial poison `c8_superseded_ancestor_1` and the status failure disappears.

`tests/test_context_activation_audit.py:1409` pins `amended.hedged is True`.
Fix: A4 removes only the poison hit, and `hedged` stays exactly as B3 defines it. Add tests that the T7 packet fails on status under A4, and that adding poison never removes a failure reason.

**F2 (medium): K3 ignores partial anchors and ambiguity.**
`benchmarks/membench/utility/context_activation_continuity.py:247,286,291`: `served` counts only `resolved`/`retrieval_carried` anchors, and `material` omits `ambiguity`. A keyless packet can name the other conversation's page as a partial anchor **and** an ambiguity candidate, and it passes K3 (`passed=True`).
Fix: count every anchor status plus `ambiguity` in `must_not_resolve` and `serves_nothing`. This is a scorer change, so pre-register it as group `-v2` with a new pinned digest, and keep v1's result as run.

**F3 (medium): K1/K2/K4 never penalise extra pages.**
The scorer checks that the referent is present and that `must_not_resolve` is absent, and `must_not_resolve` is empty for K2 and K4. A K2 packet resolving the right entity plus three unrelated pages on `recency` (and a stray unit) passes. "Right page plus wrong pages" is never caught.
Fix (v2): served anchors must be a subset of `referents`, or set a precision floor.

**F4 (low): the digest pins check names, not check logic.**
`continuity_digest()` (:186) hashes the cases and `CHECKS` names only. The `score()` body and the `_perform` payloads can drift without the digest moving. It is unchanged since `bffeb49` (`git diff bffeb49 HEAD` on the module is empty). Fix: pin a sha256 of the module source too.

**F5 (low, needs a ruling): T6's red rests on one distractor written with the turn in view.**
`benchmarks/epistemic/corpora/context_activation.py:766`: "Oven temperature conversions" carries T6's three distinctive words (temperature, Fahrenheit, Celsius). The only comparable overlap is "Quarterly roadmap retro" with C3, which is already red for the Planning reason. Harsh, not flattering, but the runbook should say it was built to match T6's words.

## Hypotheses cleared

- **Frozen bytes.** `fixture_set_digest()` = `a49d85f4…` at both base and HEAD. All numeric threshold constants are identical at base and HEAD. `threshold_digest()` = `7b2785cf…`. The corpus diff deletes one line (`CORPUS_ID`). Gold categories are untouched, and registration goes through `schema_memory infer/save-roles`.
- **Ordinary notes don't help any case.** No fixture's must-include or must-exclude fact appears in them. They are off-topic for every gold page, and every gold hit comes through the gold page itself. Their categories are shipped core vocabulary. Their measured effect is only harmful: T6 turns red, and C6/T2/T9 become load-bearing.
- **Order.** `bffeb49` (10:40:59) adds only the module, the digest pin and pure scorer tests: no report and no real-compiler test. `b97c7e9` (10:48:08) adds the JSON and the run tests, and leaves the module untouched. Git cannot rule out an uncommitted local run.
- **Stale block.** A globally ordered block (the newer other-workspace pick first) fails K1. A K4 block listing the page as `activated` instead of `edited` fails. An empty abstained packet fails all four cases.
- **A2 is recall-only.** Precision and twin false activation still use unit refs. Probe: a C5 gold-page unit moves gold_hit 0→1 and leaves precision at 0.0. `unit_parents` covers bound pages only.
- **A4 keeps resolved and lone partial poison as poison** (existing tests hold).
- **Raw untouched.** With no amendments, `recall_mentions == identity_mentions` and `hedged_poison = ∅`. `per_case` stays raw, and `amended` is a separate block, identical to raw on v4.
- **Kill switch.** Sound. Removing the naming gate (activating on shared words, a plausible wrong behaviour) fails C6/T1/T2/T9, and the live carry already fails T1/T6. The controls can fail on wrong behaviour. Note that the disabled compiler beats the product on T1 and T6.

## Reproduction

`CUDA_VISIBLE_DEVICES= XDG_STATE_HOME=$(mktemp -d) uv run pytest -q -p no:cacheprovider tests/test_context_activation_real_compiler.py tests/test_context_activation_continuity.py` gave **61 passed** (47 + 14) in 144 s at `e9b4fad`. Both recorded-report equality tests pass, so the v4 report and the continuity JSON reproduce. The sandbox blocks the tiktoken download, so `TIKTOKEN_CACHE_DIR` held the same `o200k_base` file from a PyPI wheel (sha256 checked by tiktoken).
