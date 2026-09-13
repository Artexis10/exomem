## 1. Red tests

- [x] 1.1 Add a failing test that a hook-capable client's `engagement` block carries the hook-cadence statement — through `bootstrap` and through inspect, set and clear on the agent-accessible operation — and that no other surface carries one.
- [x] 1.2 Add a failing test that serves `bootstrap` through a descriptor without `configure_memory` and asserts `change_with` names the custom-instructions route, never the CLI string.
- [x] 1.3 Add a failing test that an unreadable preference record for an identity that could have saved `off` resolves to `balanced` with source `preference:unavailable`, keeps the diagnostic, and grants no proactive capture, while an explicit override or machine configuration still wins.

## 2. Implementation

- [x] 2.1 Serve `engagement.hook_cadence` to Claude Code and Codex only, from `prominence.hook_cadence`, so both `bootstrap` and every arm of `configure_memory` carry it.
- [x] 2.2 Seed `change_with` from the served command set: the `configure_memory` route when present, otherwise the custom-instructions path documented in `docs/prominence.md`.
- [x] 2.3 Resolve an unreadable preference record to `balanced`, report `preference:unavailable`, keep the `unavailable` diagnostic, and withhold proactive capture in `capture_gate` while leaving the detached level table alone.
- [x] 2.4 Update `docs/prominence.md` and the scaffold engagement reference; re-stamp the skill-contract digest and repackage the plugin tree. `docs/capabilities.md` and the hosted candidate artifacts regenerated to no diff — the served command set did not change.

## 3. Verification and delivery

- [x] 3.1 Run the prominence, bootstrap-contract, byte-budget, hosted-fidelity and scaffold suites scoped to the change; record the compact byte position for a hook-capable client against both pins.
- [ ] 3.2 Obtain an author-independent review of the diff covering the hook-cadence wording, the hookless `change_with` text, and the interaction between the unreadable-record floor and `capture_gate`.
- [ ] 3.3 Deliver under the standing merge authority, then synchronize and archive this change.

## 4. Known residual

- [ ] 4.1 `schema_memory(subject="workflow-contracts", operation="resolve")` builds `effective_capture` from `prominence.resolve()` alone, so under an unreadable record it reports the `balanced` gate while `capture_gate` withholds proactive capture. Decide whether that projection takes the same floor.
