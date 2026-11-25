## 1. Cell: ledger, contract and rendering

- [ ] 1.1 Extract the merge in `extract._diarize` into one pure function that takes timed segments and speaker turns, and call it from the in-cell path. Verify: the existing diarisation tests pass unchanged, and a test shows equal output for equal inputs from both callers.
- [ ] 1.2 Add `speech_mode` and `worker_fallback_after` to the cell's engine settings, with `local` as the default. In worker mode, the media worker skips speech jobs and status shows them as waiting for a worker. Verify: tests for the waiting state, other stages running, and reconciliation keeping one job.
- [ ] 1.3 Add the lease to the job ledger: principal, `lease_expires_at`, progress, a per-principal cap, and expiry back to pending through the existing recovery. Verify: a concurrency test with two claimers and one job, an expiry test, and the cap.
- [ ] 1.4 Add `/speech/claim` (bounded long poll that holds no database connection), `/speech/heartbeat`, `/speech/complete` and `/speech/release` behind the gateway proof, with a payload schema and duration-scaled bounds. Completion records through `record_result` and publishes through the parent path. Verify: tests for stale claim, changed original, retried completion, oversized payload and equal rendering.
- [ ] 1.5 Admit a lease-bound `/download` for a speech-scoped principal. Verify: a test shows refusal without a live lease of the same principal.
- [ ] 1.6 Run fallback jobs on the cell's CPU after `worker_fallback_after`. Verify: a test with a fake clock.

## 2. Substrate: gateway, scope and Home

- [ ] 2.1 Add the `exomem.speech` OAuth scope and admit it only on `/speech/*` and `/download`, forwarding principal and scope in the per-request proof. Verify: gateway tests refuse the scope on `/mcp`, `/upload` and search.
- [ ] 2.2 List speech authorizations with their last use in Home, with revoke. Verify: a test shows the next request after revocation refused.

## 3. cellctl and chart

- [ ] 3.1 Render `speech_mode` and `worker_fallback_after` per cell from chart values, with no cell in worker mode by default. Verify: a manifest test.

## 4. Worker command

- [ ] 4.1 Add `evermatter speech-worker setup|run|status`: OAuth sign-in with the speech scope, GPU detection, engine and diarizer preparation, a systemd user unit on Linux and WSL, the claim loop with verified download, local partial resume by SHA-256, and deletion after completion. Verify: an end-to-end test against a local cell in worker mode with a small CPU model: claim, complete, restart mid-job and resume, tampered download released.

## 5. Proof and rollout

- [ ] 5.1 Add a `speech-worker` mode to the Cloud rehearsal: a cell in worker mode, a worker on the runner with a CPU-sized model, a revoked authorization, and a fallback. Verify: the report shows every scenario of `remote-speech-worker`.
- [ ] 5.2 Run the full corpus, get one independent review with a security pass (new credential scope), merge, release.
- [ ] 5.3 Under an operator-approved live change, set the owner's cell to worker mode with a fallback, run the worker on the owner's GPU machine, and record throughput and the first transcripts. Undo: set the cell back to `local`.
