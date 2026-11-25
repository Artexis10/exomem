## 1. Cell: speech items, contract and rendering

- [ ] 1.1 Extract the merge in `extract._diarize` into one pure function that takes timed segments, speaker turns and optional per-label embeddings, and call it from the in-cell path. Verify: the existing diarisation tests pass unchanged, and a test shows equal output for equal inputs from both callers.
- [ ] 1.2 Add `speech_mode` (`in_cell`, `remote`) and `remote_speech_fallback_after` to the cell's engine settings, with `in_cell` as the default. In remote speech, record each recording's speech stage as a speech item and let the media worker run the row's other stages. Verify: tests for a waiting item, a video's frame vectors running without it, reconciliation keeping one item, and no item while the speech engine is off.
- [ ] 1.3 Add the speech item table: state, revision, principal, `lease_expires_at`, progress per principal, and a per-principal cap. Expire leases in the claim query, and keep `recover_interrupted` and every other recovery away from speech items. Verify: a two-claimer race test, an expiry test, a test that a media child restart keeps a live lease, and the cap.
- [ ] 1.4 Add `/speech/claim` (bounded wait, no database connection held, one open claim per principal), `/speech/heartbeat`, `/speech/complete`, `/speech/release` and `/speech/original` behind the `speech` proof purpose. Completion checks principal, revision and SHA-256, validates the payload with duration-scaled bounds, renders, records with the sidecar hash captured at claim, publishes through the parent path, and keeps a 30-day receipt. Verify: tests for `lease_lost`, `stale_claim`, `original_changed`, `already_completed`, a malformed payload, equal rendering, and the original refused to a principal without the lease.
- [ ] 1.5 Run fallback items in the media worker once `remote_speech_fallback_after` has passed since the item's creation with no live lease, without speaker labels, and mark them in status. Route a later reprocess to a speech worker. Verify: a test with a fake clock.
- [ ] 1.6 Report waiting items, the last speech worker contact and fallback transcripts in status, with an unknown state shown as unknown. Verify: a status test.

## 2. Substrate: gateway, scope and Home

- [ ] 2.1 Measure the gateway's idle limit on the claim path and set the claim's wait bound below it. Verify: the measurement is recorded in design.md, Open question 1.
- [ ] 2.2 Add the `exomem.speech` OAuth scope. The gateway admits it only on `/speech/*`, signs those requests with the `speech` purpose bound to the route, item and revision and carrying the principal, and limits one open claim per principal. Verify: gateway tests refuse the scope on `/mcp`, `/upload`, `/download` and search, and a cell test refuses the `speech` purpose when it does not implement it.
- [ ] 2.3 List speech authorizations with their last use in Home, with revoke. Verify: a test shows the next request after revocation answered `revoked`.

## 3. cellctl and chart

- [ ] 3.1 Render `speech_mode` and `remote_speech_fallback_after` per cell from chart values, with no cell in remote speech by default. Verify: a manifest test.

## 4. Speech worker command

- [ ] 4.1 Add `evermatter speech-worker setup|run|status`: OAuth sign-in with the speech scope, GPU detection, engine, diarizer and voice-embedding preparation, a systemd user unit on Linux and WSL, the claim loop with verified download, resume from its own progress, and deletion of the original and partial transcript on completion, release, `lease_lost`, `original_changed`, `revoked` and recording deletion. Verify: an end-to-end test against a local cell in remote speech with a small CPU model: claim, complete, sleep and resume, a tampered download released, a lost lease cleared.

## 5. Proof and rollout

- [ ] 5.1 Add a `remote-speech` mode to the Cloud rehearsal: a cell in remote speech, a speech worker on the runner with a CPU-sized model, a revoked authorization, an older cell refusing the purpose, and a fallback. Verify: the report shows every scenario of `remote-speech-worker`.
- [ ] 5.2 Run the full corpus, get one independent review with a second security pass (new credential scope and proof purpose), merge, release.
- [ ] 5.3 Under an operator-approved live change, set the owner's cell to `remote` with a fallback, run the speech worker on the owner's GPU machine, and record throughput against the rates in design.md. Undo: set the cell back to `in_cell`.
