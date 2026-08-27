# Tasks

## 1. Regression tests (red first)

- [ ] 1.1 Pin the funnel: during `recovery_required`, a batch write whose
      graph deferral durably covers its graph-input paths currently fails
      `full_upsert_succeeded` and mints a full receipt. After the fix the
      pin inverts: report succeeds, no full receipt, telemetry counts the
      covered acceptance.
- [ ] 1.2 Fail-closed pin: a graph deferral WITHOUT covering per-path
      receipts still fails the report and mints the demand.
- [ ] 1.3 Alarm pin: recovery_required persisting beyond the bound surfaces
      in health telemetry and turns the doctor finding into a FAIL; the
      graph-disabled-while-recovery-checkpoint-exists combination FAILs
      immediately, including when the kill switch comes from an unowned
      site-packages `.pth`.
- [ ] 1.4 Startup-bound pin: a restart over a coherent durable graph
      checkpoint currently schedules a whole-vault rebuild in the watcher
      startup pass (measured: ~30 min of suspended reads per restart on a
      3.3k-file vault); after the fix it admits after O(1) validation, and
      only genuine incoherence rebuilds.
- [ ] 1.5 Drain-refusal pin: `exomem index` against a vault whose live
      service owns graph work refuses with the documented remediation
      instead of taking the claim.

- [ ] 1.6 Access fail-open pin (red first): a transient OSError reading the
      access-policy file currently caches an EMPTY policy (nothing excluded)
      under the unchanged stat signature and flips the access fingerprint,
      widening visibility and flapping recall identity with zero writes.
      After the fix: last-known-good policy retained, fingerprint stable,
      no cache poisoning; with no prior successful load, everything is
      excluded until a read succeeds.

## 2. Implementation

- [ ] 2.0 Access-policy fail-closed loading per the recall-read-path delta
      (precondition for trusting the projection-lag identity guard).
- [ ] 2.1 Extend the `epistemic_graph` clause of `full_upsert_succeeded`
      with the durable-coverage carve-out (mirror of the embeddings
      warm-up carve-out from #850).
- [ ] 2.2 Recovery-age telemetry + health surfacing + doctor FAIL findings.
- [ ] 2.3 Live-service ownership probe and refusal in the CLI index path.
- [ ] 2.4 Projection-lag tolerance: serve semantic recall from the last
      published projection during refresh; refuse only on identity change or
      absent projection (red-first pin: a single write currently flips
      semantic recall to RETRIEVAL_INDEX_WARMING until the graph republishes
      the projection - measured live as the flapping-admission duty cycle).
- [ ] 2.5 Bounded startup validation: O(1) durable-checkpoint check in the
      watcher startup pass; whole-vault rebuild only on incoherence.

## 3. Verification and delivery

- [ ] 3.1 Focused suites, lint, privacy, strict OpenSpec validation.
- [ ] 3.2 Independent adversarial review; resolve findings.
- [ ] 3.3 Live acceptance: induce recovery_required on a test vault, write
      through it, verify zero minting with covered deferrals and a firing
      alarm; verify the drain refusal against a running service.
- [ ] 3.4 Sync and archive this change after delivery.
