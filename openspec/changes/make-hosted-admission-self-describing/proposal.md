## Why

Exomem Hosted cannot admit its first tenant into an empty fleet, and nothing in the
running system says so. Deploying 0.68.3 to a fleet with zero cells produced a clean
green fleet inventory (`status: empty`, `issues: []`) while admission was structurally
shut: `hasLiveHostedCohortTarget` requires a candidate joined to an already-`bound`
cell, and there was none. The invited person saw only "hosted admission is temporarily
closed ... until its service catalogue is updated", which names no procedure, and the
operator saw a passing inventory. The remedy exists — the virgin-install reviewer
bootstrap in the promotion run sheet — but nothing at the point of failure points to
it, and the release that was just deployed had never been registered as a candidate at
all.

The gates themselves are sound. Admission is empirical on purpose: a hosted cell serves
a gateway contract that Claude and ChatGPT cache, so admitting a tenant onto a runtime
whose observed contract nobody has proven would break clients in a way the end user
cannot diagnose. The defect is that every invariant is phrased as "prove the new thing
against the currently-running thing", which is correct for a fleet of size >= 1 and
undefined at zero — and the system neither detects that state, explains it, nor lets
the recovery from it be retried.

## What Changes

- Make every admission refusal name its own remedy. `HOSTED_ADMISSION_CLOSED` MUST
  distinguish "no live cohort" from other closures and MUST carry an operator-facing
  reference to the bootstrap procedure, without leaking tenant or invite detail to the
  invited person.
- Make the closed state observable before a human hits it. The control plane MUST
  report whether it would currently admit a tenant, derived from the same authority the
  admission path uses, and a closed report MUST be a fleet-inventory issue rather than a
  clean `empty`. The inventory MUST NOT re-derive that fact: its substrate observation
  is a closed schema of cell-keyed fields, and every such field already forces a cell to
  count as live, so at zero bound cells there is nothing left to derive from.
- Register the release with the control plane as part of publishing it. Publishing a
  signed runtime image candidate MUST also record a `pending` agent-contract candidate
  for that release, so the service catalogue cannot silently lag the deployed runtime.
- Make the virgin-install reviewer bootstrap resumable. Each step MUST be individually
  retryable against its own recorded checkpoint, and a failure MUST NOT consume the
  invite, alias, staged release, or client record that the retry needs. Today a single
  failure burns all four and strands a tenant that blocks the retry.
- **BREAKING** Decouple platform selection from promotion. Promoting one platform MUST
  NOT foreclose adding another to the same candidate. Today promotion retires the
  rollout assignment that its own `cells` precondition depends on, so promoting Claude
  alone permanently prevents pairing OpenAI onto that candidate — a permanent product
  consequence that falls out of an implementation detail rather than a decision.

## Capabilities

### New Capabilities

- `hosted-admission-bootstrap`: Defines how Hosted reports, detects, and recovers from
  a fleet state in which no tenant can be admitted — the self-describing refusal, the
  inventory signal that precedes it, the resumable bootstrap that clears it, and the
  requirement that promotion leave a second platform pairable.

### Modified Capabilities

- `hosted-image-candidate-publication`: Publishing a signed runtime candidate also
  registers the corresponding pending agent-contract candidate, so the deployed release
  and the service catalogue cannot diverge silently.

Note: the one-shot promotion behaviour this change replaces is not specified in any
canonical spec today — it exists only as implementation in
`agent-contract-store.ts` and as prose in the promotion run sheet, and the
`hosted-runtime-upgrade` capability that would own it is still an unarchived change.
The replacement is therefore stated as a new requirement here rather than as a delta
against a requirement that does not exist.

## Impact

- Substrate: `src/lib/exomem-hosted/fleet-observation.ts` (admission-readiness field on
  the observation), `errors.ts` (refusal detail),
  `hosted-cohort-target.ts` and `db.ts:680,804-809` (closure classification),
  `agent-contract-store.ts` (promotion preconditions, assignment retirement),
  `oauth-store.ts` (bootstrap authority checkpoints), plus database migrations for
  bootstrap checkpoint state and any promotion-assignment change.
- Exomem: `infra/scripts/hosted_fleet_inventory.py:1549` (issue classification),
  `scripts/reviewer_bootstrap.py` and `scripts/promotion_evidence.py` (resumable
  steps), the release workflow that publishes image candidates, and
  `docs/runbooks/exomem-hosted-alpha.md:242-262,357-401`.
- Operators: an empty fleet reports admission as closed instead of green; the bootstrap
  can be retried without burning single-use inputs; the platform choice at promotion
  stops being irreversible.
- Tenants and invited people: no behavioural change to a working fleet. A refusal
  during a closed window stays non-leaking, and the invitation remains unconsumed and
  valid exactly as it does today.
