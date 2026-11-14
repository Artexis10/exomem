# Design

## Context

See proposal.md for the acceptance conflict. The lexical freshness specification requires a busy database to decline a pass and retain work for retry. SQLite also requires WAL safety before replacement.

## Goals / Non-Goals

**Goal:** judge the target's recovered catalogue and caller availability within the existing rehearsal window.

**Non-goal:** change runtime publication, reader lifetimes, retries, migration or request budgets.

## Decisions

Use the existing publication, promotion and strict readiness evidence. Publication and authoritative admission can occur in separate repair steps. Idle repair telemetry alone does not prove currentness or an empty queue.

Retain failed attempts and earlier verdicts. A revised criterion receives a separate assessment. Reuse evidence only while its relevant artifact, runtime, workload and conditions match.

The closest maintained solution is the current retry and catch-up contract. A new publication mechanism would add runtime complexity without evidence of a violated runtime guarantee.

The proof uses a fixed rehearsal with a declined pass, later publication, later authoritative admission and successful caller operations. Unrecovered or unproved work remains an installation hold. No custom validator or repeated rehearsal is needed when retained evidence already proves these conditions.

Scoped validation covers the OpenSpec contract, public-artifact privacy and the normal PR tier. Runtime evidence uses the exact qualified artifact and matching dependencies. This prose change needs no executable test or Full dispatch.

## Risks / Trade-offs

- A recovered refusal could hide persistent failure: require current-catalogue admission before the fixed window ends.
- A partial trace could appear complete: retain unmatched events and unknown attribution separately from independently proved final state.
- A false installation hold delays a working release: assess recovery rather than requiring each attempt to publish.
- Removing the WAL refusal could lose committed data: retain the existing refusal and every runtime budget.

## Delivery

The runbook delivery precedes its managed-install acceptance check. The installed result then completes verification and enables specification synchronization and archive closure. This rollout dependency requires a bounded closure delivery after the policy delivery.

Rollback restores the prior runbook predicate. A live rollback continues through the existing managed-upgrade state checks; this policy change creates no migration or automatic downgrade.
