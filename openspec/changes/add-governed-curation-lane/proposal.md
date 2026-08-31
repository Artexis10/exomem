## Why

Exomem can now detect maintenance work and surface it without nudging, but the
last mile is still manual: an agent can inspect relation debt, stale or
duplicated conclusions, and structural drift, then must perform a sequence of
independent writes with no durable plan, resumable status, or crash-safe proof
of which steps committed. That leaves the highest-risk part of no-nudge
curation—multi-step restructuring—outside the governed workflow.

Adoption Studio already proves the useful shape for first-run material:
deterministic context, an agent-authored proposal, explicit review, and apply
through existing governed leaves. Ongoing curation needs the same separation of
judgment from execution, but with ordered step receipts, partial-failure
recovery, and compensation rather than a false multi-file atomicity promise.

## What Changes

- Add a governed curation run that accepts an agent-authored, immutable ordered
  plan over existing content operations, validates and fingerprints every
  target, and exposes preview/status/apply/resume/compensate actions.
- Keep the active agent as the only semantic decider. Exomem may deterministically
  assemble candidates and validate a supplied plan, but it does not rank,
  interpret, or author curation work with a server-side model.
- Execute one step at a time through existing governed leaves; persist a
  terminal receipt for every step and explicit `partial`, `failed`,
  `interrupted`, and `compensated` states instead of claiming cross-step
  atomicity.
- Make exact-plan replay idempotent. A replay verifies committed step receipts
  and live state, resumes only uncommitted work, and refuses changed plan bytes,
  stale bindings, or an uncertain outcome it cannot prove.
- Model reversal as a separately reviewed compensation plan: recover governed
  trash for move/delete effects and use a new superseding correction for
  authored-content effects. Compensation never erases the original plan,
  receipts, or history.
- Extend the existing `maintain_memory` product command rather than adding a
  parallel tool. The same registry-derived MCP, REST, CLI, and Hosted surface
  exposes curation; request-bound remote execution is allowed only for this
  exact reviewed-plan mode and remains behind the normal tenant, authority,
  mutation-boundary, receipt, and idempotency gates.
- Deliberately narrow the broad refusal introduced by the still-active
  `bound-remote-maintenance` change only for this bounded one-step protocol.
  Long-running remote `fix`, `reconcile`, and ID backfill remain refused; S8
  does not revive their synchronous request shape.
- Teach the shipped curation workflow skill to use the new run while preserving
  the `restructure_execution` confirm-required ceiling. Candidate surfacing may
  be quiet or advisory according to the existing envelope, but applying or
  compensating a plan always requires explicit in-conversation confirmation.
- Keep plan validation content-language-neutral: paths, hashes, registries, and
  operation schemas—not English text classification—determine executability, so
  agent-authored plans can curate multilingual Markdown.

This change does not automate Planning/Records lifecycle decisions or redesign
the separate Exomem/OpenSpec workflow contract.

## Capabilities

### New Capabilities

- `governed-curation-lane`: Durable agent-authored curation plans, exact review
  bindings, stepwise governed execution, per-step receipts, crash recovery,
  exact replay, partial-failure visibility, and separately reviewed
  compensation for ongoing knowledge maintenance.

### Modified Capabilities

- `command-surface`: Extend `maintain_memory` with the finite curation action
  set, correct read/write classification, generated MCP/REST/CLI parity, and
  stable review/apply error envelopes.
- `hosted-gateway-contract`: Admit the same curation actions on the current
  Hosted agent surface through the shared registry and mutation boundary,
  without a Hosted-only executor or bypass of tenant and authority checks.
- `delegation-envelope`: Bind curation plan apply and compensation explicitly
  to the existing confirm-required `restructure_execution` class while keeping
  proposal assembly under `structural_suggestions`.

## Impact

- Adds a focused curation run/plan executor and tests, reusing the existing
  governed content leaves, review-state identities, mutation terminals,
  idempotency store, writer boundary, and trash/supersession mechanisms.
- Extends `maintain_memory` arguments, selector classification, tool schema,
  generated capability documentation, bootstrap teaching, and the generic
  Hosted profile/artifacts where required by the current release pipeline.
- Adds deterministic crash-injection coverage around phase persistence, leaf
  commit, step-receipt persistence, and compensation progress, plus standalone
  and Hosted parity tests.
- Leaves Adoption Studio semantics, the Planning and Records product commands,
  OpenSpec lifecycle behavior, and legacy operator-only `fix`, `reconcile`, and
  `backfill-ids` remote refusal unchanged.
