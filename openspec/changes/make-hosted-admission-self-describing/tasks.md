## 1. Classify the admission refusal

- [ ] 1.1 Add a closure reason to the admission refusal path so the three call sites
      (`db.ts:680`, `db.ts:862`, `oauth-store.ts:1479`) are distinguishable
- [ ] 1.2 Carry the reason in the operator-facing envelope and structured log, leaving
      the public `HOSTED_ADMISSION_CLOSED` message unchanged in substance
- [ ] 1.3 Reference the bootstrap procedure only for the no-live-cohort reason
- [ ] 1.4 Test that a no-live-cohort refusal leaks no tenant, cohort, candidate, or
      fleet detail to the refused party, and leaves the invitation unconsumed
- [ ] 1.5 Test that a refusal with a different cause does not reference the bootstrap

## 2. Report a fleet nobody can join

- [ ] 2.1 Add the `admission_closed` issue to `hosted_fleet_inventory.py` when the
      reconciled fleet has zero bound cells and the Substrate observation reports no
      live cohort
- [ ] 2.2 Confirm the issue flows through the existing `status` derivation at
      `hosted_fleet_inventory.py:1549` so the result is not `empty` or `consistent`
- [ ] 2.3 Make the upgrade phase gate at `hosted_runtime_upgrade.py:363` name
      `admission_closed` as the blocking condition when it refuses
- [ ] 2.4 Test the three cases: zero bound cells with no live cohort (issue raised),
      zero bound cells with a live cohort (no issue), and a populated fleet (no issue)
- [ ] 2.5 Resolve the open question on whether an operator may acknowledge the issue for
      a deliberately empty platform, and implement whichever answer is chosen

## 3. Keep the catalogue current with the deployed runtime

- [ ] 3.1 Register a `pending` agent-contract candidate as part of publishing a signed
      runtime image candidate
- [ ] 3.2 Scope the pipeline credential to candidate registration only, with no
      promotion, live-candidate, or tenant authority
- [ ] 3.3 Make registration idempotent for a republished release
- [ ] 3.4 Ensure a registration failure surfaces its own alert and never fails the
      image publication
- [ ] 3.5 Backfill a pending candidate for the currently deployed release so the
      catalogue is truthful before anything depends on it
- [ ] 3.6 Test each scenario in the `hosted-image-candidate-publication` delta

## 4. Make the bootstrap resumable

- [ ] 4.1 Add per-step checkpoint state to the reviewer bootstrap authority record,
      with a migration
- [ ] 4.2 Record each step of `scripts/reviewer_bootstrap.py` against that authority as
      it completes
- [ ] 4.3 Ensure a failed step leaves the invitation, email alias, staged client
      release, and client record reusable
- [ ] 4.4 Make resume re-verify preconditions rather than trusting the checkpoint, and
      refuse by naming the precondition that no longer holds
- [ ] 4.5 Allow a retry to reuse or retire a tenant stranded by an earlier attempt
- [ ] 4.6 Test an interrupted attempt resuming from the first incomplete step, and a
      resume refused because the world moved
- [ ] 4.7 Update `docs/runbooks/exomem-hosted-alpha.md:357-401` to describe resumption
      and drop the guidance that a failure burns the whole set of inputs

## 5. Decouple platform selection from promotion

- [ ] 5.1 Answer the open question on whether a later pairing needs fresh runtime health
      evidence or may rely on the retained attestation
- [ ] 5.2 Retain the attested routable-cell proof past promotion, with a migration
- [ ] 5.3 Change promotion so it no longer destroys the evidence a second platform's
      precondition depends on
- [ ] 5.4 Ensure a promoted candidate is reported as promoted and is never selected or
      treated as an in-flight rollout
- [ ] 5.5 Test pairing a second platform onto an already-promoted candidate without a
      fresh candidate, reviewer tenant, or repeat evidence run for the first platform
- [ ] 5.6 Test that a promoted candidate with retained proof is not treated as in-flight
- [ ] 5.7 Update the promotion run sheet at `docs/runbooks/exomem-hosted-alpha.md:242-262`
      to remove the permanent-foreclosure warning once the behaviour is gone

## 6. Verification

- [ ] 6.1 Run `openspec validate --all --strict`
- [ ] 6.2 Exercise the whole path end to end on an empty fleet: inventory reports
      `admission_closed`, a redemption attempt is classified and points at the run
      sheet, the bootstrap runs to a bound cell, and inventory then reports clean
- [ ] 6.3 Confirm ordering held during rollout — diagnostics (groups 1-2) shipped before
      catalogue registration (3), and bootstrap resumability (4) before the promotion
      change (5)
