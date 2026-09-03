## Context

Hosted admission is empirical by design. `hasLiveHostedCohortTarget`
(`src/lib/exomem-hosted/hosted-cohort-target.ts`) admits a provision only when exactly
one candidate for `EXOMEM_HOSTED_PROFILE` is `live` **and** every `bound` cell serving
that candidate's release agrees on a single `observed_gateway_contract_digest`. The
same shape appears as the `live_target` CTE in `redeemInviteAtomic`
(`db.ts:777-795`), which is required only under wire protocol v2 (`db.ts:804-809`).

That invariant is correct and should not be weakened. A hosted cell serves a gateway
contract that Claude and ChatGPT cache; admitting a tenant onto a runtime whose
observed contract nobody has proven breaks clients in a way the end user cannot
diagnose. Deriving the catalogue from observation rather than declaration is the point.

The failure is at the boundary. Every gate is phrased as "prove the new thing against
the currently-running thing", which is well-defined for a fleet of size >= 1 and
undefined at zero. The system currently handles zero through a privileged bootstrap
(`docs/runbooks/exomem-hosted-alpha.md:357-401`), which is the right structural answer
— the alternative, letting the first cell in unproven, is a gate anyone can bypass by
draining to zero. But that bootstrap is undiscoverable from the failure, invisible in
the inventory, and non-resumable when it goes wrong.

Observed on 2026-09-02 while deploying 0.68.3: fleet inventory reported
`status: empty, issues: []`; `liveCohortCandidateId` was `None`; no candidate existed
for 0.68.3 at all (newest `hosted-alpha-agent-v4` were 0.63.1 and 0.66.0); and the
invited person received "not admitting new accounts until its service catalogue is
updated" with no route to the remedy.

## Goals / Non-Goals

**Goals:**

- A refusal names its own remedy without leaking tenant, invite, or cohort detail to
  the refused party.
- The closed state is visible in fleet inventory before a human encounters it.
- The service catalogue cannot silently lag the deployed runtime.
- The bootstrap can be retried without burning single-use inputs.
- Promotion stops permanently foreclosing a platform.

**Non-Goals:**

- Weakening the bound-cell requirement, or adding a flag that admits tenants without
  proven contract observation. The empirical gate stays.
- Automating the reviewer bootstrap end to end. It should be resumable and
  discoverable, not unattended.
- Auto-promoting a candidate. Registering a `pending` candidate is not promotion, and
  promotion stays an explicit operator act.
- Changing wire protocol v1 behaviour or its admission rules.

## Decisions

**Classify the closure rather than adding a new error code.** `HOSTED_ADMISSION_CLOSED`
already reaches the invited person with correct, non-leaking copy, and its three call
sites (`db.ts:680`, `db.ts:862`, `oauth-store.ts:1479`) mean different things. Add a
closure *reason* carried in the operator-facing envelope and structured log while the
public message stays as it is. Alternative considered: a distinct
`HOSTED_NO_LIVE_COHORT` code — rejected because it changes a user-visible contract for
an audience that cannot act on the distinction, and every client would have to learn it.

**Derive the inventory signal, do not store it.** `hosted_fleet_inventory.py:1549`
computes `status` as `"inconsistent" if issues else "empty" if live_count == 0 else
"consistent"`. Add `admission_closed` to the issue set when the reconciled fleet has
zero bound cells and the Substrate observation reports no live cohort. This reuses the
existing issue machinery, so it automatically blocks the upgrade phase gate at
`hosted_runtime_upgrade.py:363` — which is correct: an upgrade should not advance into
a fleet nobody can join. Alternative considered: a separate readiness endpoint —
rejected as a second source of truth for the same fact.

**Register the candidate from the publication pipeline, not the deployment.** The
signed candidate already exists as a release artifact with a verified digest, and
`hosted-image-candidate-publication` already owns its provenance. Registering the
`pending` agent-contract candidate there keeps one producer for the identity.
Registering at deploy time was considered and rejected: the deploy path is the
provisioner's, has no Substrate write authority, and would make an operator-driven
rollback silently mutate the catalogue.

**Checkpoint the bootstrap on the authority record, not in the harness.** The
`exomem_marketplace_reviewer_oauth_bootstrap_authorities` row already scopes one
attempt and already has a lifecycle (`consumed`, revoked, expired). Recording per-step
progress there makes resumption a server-side fact rather than local harness state,
which survives a lost terminal — the case that actually strands attempts today.
Alternative considered: a state file next to `scripts/reviewer_bootstrap.py` — rejected
because the state that matters (invite consumed, client registered, tenant created)
lives in the database, and a local file can disagree with it.

**Keep promotion one-shot per *platform*, not per *candidate*.** The current trap is
that promotion retires the rollout assignment that the `cells` precondition depends on
(`agent-contract-store.ts`), so the bound proof a second platform needs is gone the
moment the first is promoted. Preserve that proof — retain the assignment, or persist
the routable-cell digest it attested — so a later pairing can verify against the same
evidence. This is the largest change here and the only **BREAKING** one; it alters what
promotion leaves behind.

## Risks / Trade-offs

- **Retaining the rollout assignment past promotion weakens a cleanup invariant.** →
  Retain the attested digest rather than an `active` assignment where possible, so
  nothing downstream mistakes a promoted candidate for one still rolling out. Whichever
  is chosen must be proven by a test that fails when a promoted candidate is treated as
  in-flight.
- **`admission_closed` will make the upgrade gate refuse on an empty fleet.** → That is
  intended, and it is also a behaviour change for anyone deliberately upgrading an
  empty platform. The bootstrap must therefore be runnable while the execution is held,
  and the gate must state which issue is blocking it.
- **Auto-registering a pending candidate adds a Substrate write to the release
  pipeline.** → Scope the credential to candidate registration only. A failure to
  register must not fail the image publication, and must surface as its own alert;
  registration is recoverable by hand, an unpublished image is not.
- **Bootstrap checkpoints could resume into a half-built tenant.** → Each checkpoint
  records what is provably true server-side. Resume must re-verify preconditions rather
  than trusting the checkpoint, and must refuse when the world has moved.

## Migration Plan

1. Ship the inventory issue and the classified refusal first. Both are read-only
   diagnostics and independently useful; neither changes admission behaviour.
2. Ship candidate auto-registration next, backfilling a `pending` candidate for the
   currently deployed release so the catalogue is truthful before anything depends on it.
3. Ship bootstrap checkpointing before the promotion change, so an attempt at the
   promotion work is itself retryable.
4. Ship the promotion decoupling last, behind its own migration, since it is the only
   breaking change and the only one that alters retained evidence.

Rollback: steps 1-3 are additive and revert cleanly. Step 4 requires a migration that
restores prior assignment retirement; a candidate promoted under the new behaviour must
be treated as live and not re-promoted.

## Open Questions

- Does a second platform paired onto an already-promoted candidate require fresh
  runtime health evidence, or does the retained attestation suffice? This decides
  whether step 4 is a storage change or also a re-verification change.
- Should `admission_closed` block the upgrade phase gate outright, or report as an
  issue the operator can acknowledge for a deliberately empty platform?
- Is there any legitimate operational state with zero bound cells where admission is
  expected to stay open, which would make the inventory signal a false positive?
