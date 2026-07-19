# Disable the premature hosted black-box schedule

## Context

The hosted private beta is merged and statically verified but not deployed. The
Hosted infrastructure workflow nevertheless starts an external black-box run
every five minutes. Its three target URL secrets do not exist, so each scheduled
run correctly fails closed with `black-box target is not configured` before any
network request.

## Design

Remove only the `schedule` trigger. Preserve pull-request and push validation,
manual dispatch, the external black-box job, and its fail-closed treatment of
missing targets. Document the deployment gate: configure all three repository
secrets, prove one manual run returns three healthy observations, and only then
restore the five-minute schedule. The cadence remains coupled to the
observability contract's 300-second poll interval.

## Verification

- Parse the edited workflow and confirm it retains pull request, push, and manual
  dispatch triggers but has no schedule trigger.
- Confirm the external black-box job and all three secret bindings are unchanged.
- Run the hosted workflow's normal static validation in CI.
- After merge, confirm no new scheduled run appears beyond the previous
  five-minute cadence.

