## 1. Ledger and lease

- [ ] 1.1 Add the lease table and migration to `media_jobs.py`: worker identity, expiry and the grant snapshot (sidecar hash, evidence version, binary identity, language pin, replaced digest).
- [ ] 1.2 Add the remote claim on top of `claim_next`: `skip_media_types` for unrouted or uncovered kinds, no `do_offload` jobs, the release-gate eligibility check, and the claim order and hydration cap of `add-cloud-media-store`.
- [ ] 1.3 Add heartbeat (capped at the job timeout), the B6 hand-back (`returns`, `retry_at`, return reason in status) and the expiry sweep, which clears pin and `hydrated` in one write and counts against the timeout-stop budget.
- [ ] 1.4 Record a remote result through `record_result` with the stored snapshot.

## 2. Service routes and credential

- [ ] 2.1 Add the worker credential store (hashed, machine-local) and `exomem media-worker credential issue|list|revoke`.
- [ ] 2.2 Add the five `/media-worker/v1/` routes, with admission that accepts only a worker credential and checks lease holding.
- [ ] 2.3 Stream the original from a held read handle with `Range`, under the lease's pin and the release gate.
- [ ] 2.4 Render a remote extraction from its structure with the local renderer, matching speaker clusters against the local voice profiles.

## 3. Routing

- [ ] 3.1 Read the `remote_workers` block of the per-vault media configuration and compute worker presence.
- [ ] 3.2 Make the local child skip routed kinds while a worker is present or local fallback is off.
- [ ] 3.3 Show remote leases and worker last-seen times in media status.

## 4. Worker

- [ ] 4.1 Add `exomem media-worker run --service URL --credential-file PATH`: claim, verify the original, compute with `extract`, heartbeat, hand in, release on shutdown.
- [ ] 4.2 Return anonymous diarisation turns and one embedding per raw cluster when diarisation is enabled on the worker.

## 5. Proof

- [ ] 5.1 Test the boundaries with the real service and the worker client: lease expiry returns the job and a late result is refused; a changed sidecar makes the result stale; a worker credential cannot reach a tool route or another worker's lease; the local child skips routed kinds while a worker is present.
- [ ] 5.2 On the installed service, route speech to a worker on a second machine with local fallback off. Process one real recording, and record its sidecar facts and the vault host's GPU use during the job here.
