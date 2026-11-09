## Why

A release waits for a session to run the guarded merge after its evidence and checks pass. Over six releases on 2026-11-08 and 2026-11-09, that wait between the required gate passing and the merge was 1.3 to 6.0 minutes, median about 4.2. It is the largest part of release time that no test or runner change touches. The maintainer chose to remove it while keeping every gate and a simple off switch.

## What Changes

- The release intent dispatch takes an optional reviewed head (`expected_head`). With it, the intent job validates the head and keeps a short-lived `release-intent` artifact naming the release PR, its source commit and that head.
- A new `land` step runs after the evidence refresh, and in the intent job when evidence was reused. It merges only when an intent names the release PR's current source and head exactly, and only after every required check passes. It then runs the same guarded preflight as the manual merge command.
- The merge uses the release App token, so the merge push starts Release Please and the tag follows as before.
- The repository variable `RELEASE_AUTO_MERGE=off` turns landing off. An intent without a head also leaves the merge to the release session.
- A Full run that no intent named, a newer release head, a moved main, a failed required check or missing evidence never merges.

GitHub's native auto-merge is not used: it persists across head and base changes and skips the source and evidence preflight.

## Impact

- `.github/workflows/release-evidence-automation.yml`, `scripts/release_evidence.py`, `tests/test_release_evidence.py`, `docs/release.md`.
- The release runbook's manual merge step becomes the fallback for an intent without a head or with landing off.
