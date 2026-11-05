# Design

## Context

See `proposal.md` for the problem and scope.
The dispatcher already binds a request budget, but the audit does not consume it.
`AuditReport` already exposes coverage through `metadata.coverage` and `meta.coverage`.
The final result filter obtains fresh admission after the command body returns.
That filter can take longer than the existing delivery reserve on a large report.

## Goals / Non-Goals

**Goals:** Bound cooperative audit work and preserve the distinction between a completed check and unknown coverage.
Keep the final authorization decision fresh, including after concurrent changes.

**Non-Goals:** No new queue, model, category order, continuation protocol, timeout setting or persistent audit cache.
Do not change attention, canonical writes, local audits or reconcile exemptions.
Do not broaden this repair into the separate lifetime of diagnostic spans.

## Decisions

### Consume the existing budget explicitly

Pass the current budget from `op_audit` into `audit.audit`, with an absent default for existing callers.
Use `request_budget.leaving` to preserve the existing delivery reserve.
Check before each category and between bounded units of parsing, hashing and category work.
Expensive helpers must accept the same checkpoint; a warm-cache miss must not start an unbounded fallback build.
An interrupted preparation must not publish a partial shared cache or corpus snapshot.

Carry one request-owned interruption signal through corpus proof, resolver fallback and accepted store read batches.
Handle that signal before broad exception handlers, so it cannot become a cache miss, an empty finding list or another scan.
Bound queue waits and check the same allowance inside `collection_store.preview._Decide`; a caller timeout alone does not cancel accepted work.
Keep canonical store writes outside this cancellation path.

In shared corpus flights, an expired waiter leaves the owner's build alone.
An expired owner releases its flight as abandoned, not as a reusable failure for another caller.
Other callers retain their own allowances and may acquire a new flight; no interrupted snapshot is advertised as complete.
Coordinate exact shared resolver and store symbols with their current owners before implementation.

This reuses the maintained request context instead of adding a timer or abandoning a running worker thread.
An outer timeout alone leaves synchronous work running after the caller has gone.

### Keep completed categories and discard interrupted categories

Accumulate each category separately and merge it only after it completes.
Keep complete-category findings in the existing action-first order.
Record interrupted and skipped stages through the existing budget object.
Expose corpus completeness and completed categories through the existing coverage carrier.
Do not report zero, total absence or full-corpus counts for unevaluated categories.
If corpus preparation stops, do not run a census-dependent check against its prefix.
Propagate requested-check incompleteness into `AuditReport.as_public_dict`, `_semantic_upstream_facts` and the existing semantic batch metadata.
This includes `upstream_findings_complete`, `observation_complete` and legacy-backlog completion fields in both detail modes.
An absent interrupted semantic result must not trigger the current default that treats missing metadata as complete.
Counts from completed checks remain valid; counts for unevaluated work remain unknown.

Returning individual partial findings would require a separate meaning for every aggregate check.
Complete-category results provide a smaller contract and avoid false orphan or broken-link findings.

### Preserve fresh authorization and bound final release

Identify audited invocations from command metadata beside `_resolved_invocation_selector`.
The predicate selects direct audit commands and audit routes with their resolved selector, including omitted defaults.
It must exclude ordinary attention and every invocation without a request budget.

At the read result boundary in `writer_lease.py`, retain fresh `published_admission.deciding(own=True)`.
Pass an optional deadline checkpoint through the existing egress result traversal in `governance/egress.py`.
Check path enumeration, nested entries and final artifact scrubbing; do not replace fresh admission with an earlier proof.
Apply the same allowance inside queued admission batches, including their per-page checks.
If this pass exhausts its allowance, discard the entire report and return `AUDIT_BUDGET_EXHAUSTED` in the existing structured error shape.
That terminal is an internal fixed-type carrier with static protocol text; it contains no findings, paths, counts or reflected exception text.
Its finalization cannot enumerate vault content or construct a cold artifact census, even when governance is active.
Do not send it through the existing unbounded string-scanning error path.
Recognize the internal carrier by its type, never by a caller-controlled error-code dictionary or string.
Keep ordinary error filtering unchanged, and recommend a narrower category selection in the fixed terminal text.

Dropping individual entries in the generic filter could leave stale summaries and legacy counters.
An all-or-nothing final release preserves authorization without rebuilding audit aggregates inside the egress layer.

### Keep the plan and proof proportional

Use the existing corpus parser, category registry, request budget, error carrier and result filter.
Add no dependency or parallel runtime.
Every new checkpoint exists to stop work the caller can no longer receive.
Its cost is an explicitly incomplete report, paid by the caller of the expensive audit.
The final budget error prevents release without a completed authorization proof; it requires no human gate.

## Risks / Trade-offs

- A slow individual operation can overrun a cooperative deadline. Check inside large reads and hashes, not only between categories.
- An incomplete corpus can create false findings. Require complete prerequisites and discard interrupted aggregate work.
- A fresh filter can consume the delivery reserve. Abort its report release through the small existing error path.
- Shared helper changes can affect writers. Keep budget parameters optional and enable them only for audited reads.
- A swallowed interruption can restart work. Prove fallback, shared-flight and accepted store-read cancellation explicitly.
- Budget-error filtering can start a cold census. Prove the complete fixed terminal path performs no such enumeration.
- A tight deadline can return few findings. Expose unknown coverage and the existing narrower-category route.

## Migration Plan

1. Add focused deadline and coverage regressions in isolated temporary state; the current implementation fails them.
2. Implement the audit and release changes; complete independent review and the normal release checks.
3. Publish the ordinary release.
4. After the operator confirms the existing approved timing condition, promote the service with media still paused.
5. Run one audit through the real connector; verify its terminal, coverage and server ledger before the client times out.

No state migration is required.
Recover a failed promotion through the managed transition record; never start an older release over changed state.
