## Why

Exomem's retrieval, lifecycle and activation checks do not yet answer whether the same agent performs better with memory than without it. Successful recall can coexist with worse decisions, unnecessary context or expensive maintenance. Development needs a cheap instrument that can expose those losses.

Direct inspection of main at `ae02dba7` finds substantial reusable infrastructure. This is a medium extension to `membench`, not a new benchmark framework. The first useful implementation requires a real agent, isolated paired episodes and an action grader; adding a score to a scripted product journey would not establish downstream utility.

## What Changes

- Add a seeded, three-session action scenario with helpful-history, self-contained-task and stale-distractor variants.
- Compare the same actor without Exomem and with a pinned Exomem revision. Score resulting environment state with deterministic assertions.
- Keep task utility, paired harm, lifecycle diagnostics, cost and latency separate. Include failures and incomplete coverage explicitly.
- Reuse generation/oracles, isolated cells, trace witnesses and the budget ledger. Consume the native actor seam being developed in PR #1126 through a thin adapter rather than copying its LongMemEval scheduler or requiring a general runtime refactor.
- Add context-policy and oracle controls after the basic instrument works. Separate agent-authored lifecycle experiments from checkpoint-based context diagnostics.
- Keep paid execution opt-in, with preflight pair reservation and a hard spend limit. Ordinary tests remain model-free. Runtime failure produces explicit outcome data; it never silently passes or falls back to a different model.

Models run only in the external benchmark actor. The product remains pure substrate: no server-side reasoning model or automatic knowledge authorship is introduced.

## Capabilities

### New Capabilities

- `epistemic-utility-regression`: paired downstream action evaluation with lifecycle, context, harm and efficiency accounting.

### Modified Capabilities

- `benchmark-protocol`: register a deterministic action-outcome family and enforce its existing amendment release gate at utility execution and report entry points. Existing track scores retain their meanings.

## Impact

Implementation belongs under `benchmarks/membench/`, with shared actor support under `benchmarks/` and tests in `tests/`. No production defaults change. The design and tasks identify integration dependencies, acceptance gates and the smallest complete vertical slice. This proposal does not authorize restarting LongMemEval replay or publishing comparative product claims.
