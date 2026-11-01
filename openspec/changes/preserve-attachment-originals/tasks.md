## 1. Specification

- [x] 1.1 Create the change with the `client-artifact-preservation` additions and pass strict validation.

## 2. File parameters

- [x] 2.1 Add failing coverage that `capture_source` declares `openai/fileParams` and that no schema takes a local path for a file.
- [x] 2.2 Declare the metadata on `capture_source`.

## 3. Held uploads

- [x] 3.1 Add failing coverage for holding on local ingress, byte-identical redemption, wrong-session, public-path and no-grant refusal, reuse, expiry, lane mismatch and integrity refusal.
- [x] 3.2 Add the hold store, the additive `hold=1` branch on `/upload`, and redemption in `stage_artifact` for both lanes.
- [x] 3.3 Hold from `exomem attach` when no destination is given, with `--lane`.
- [x] 3.4 Security review: put a claimed hold back when its file is not stored (both lanes), sweep expired holds on redemption, cap live holds and held bytes per local session, make an existing hold directory private, and redeem a hold over the MCP transport in a test.

## 4. Transcriptions

- [x] 4.1 Add failing coverage that a transcription is written with its stored original and bound to its SHA-256, size and content type, is refused when it names no file, and is written nowhere when the original fails.
- [x] 4.2 Add `transcriptions` to `preserve_artifacts`, the `client-transcription` marker, and the compact terminal projection.
- [x] 4.3 Security review: refuse repeated `file_id` values before staging when transcriptions are supplied, and bind each transcription to its file's position rather than its label.

## 5. Guidance and delivery

- [x] 5.1 Update tool descriptions, bootstrap and the scaffold skill with custody-first guidance per client, within the compact byte budget.
- [x] 5.2 Regenerate the tool-schema baseline, plugin skills, hosted candidates and capabilities; run the scoped suites, lint, the privacy gate and strict OpenSpec validation.

## 6. Live acceptance

- [ ] 6.1 After release and connector refresh, accept each client path live: a ChatGPT conversation preserves attached images with `transcriptions`, a local client redeems an `exomem attach` handle over its local token, and claude.ai code execution uploads an attached file after the user's opt-in; each stored artifact's SHA-256 matches the original.

## 7. Recover originals across clients

- [ ] 7.1 Verify each supported client's documented file-output interface and actual consumption; distinguish native files, resource references and HTTPS fallback.
- [ ] 7.2 Extend existing transfer owners to retrieve an exact stored artifact version through existing identity, session and full-byte disclosure checks.
- [ ] 7.2a Thread the computed identity through all disclosure owners; prove bounded original reads and unchanged scope decisions. Downloads hold the original open instead of copying it (D11, amended 2026-11-01).
- [ ] 7.3 Bind new short-lived download grants to one artifact version; verify absent, expired, revoked, foreign and changed-version authority without content or existence leakage.
- [ ] 7.3a Match native caller bindings, recheck authority after staging, preserve header-only legacy tokens, and verify interruption cleanup plus capability-free logs.
- [ ] 7.4 Prove isolated preservation, external-source removal, original retrieval, client consumption, derivative provenance and re-preservation with matching original hashes.
- [ ] 7.4a Verify human browser download without hidden headers and bounded recovery of larger audio/video originals; keep unsupported client payloads explicit.
- [ ] 7.5 Deliver the cohesive outbound batch after independent security review and required CI; verify each released client path before closing its acceptance.
- [ ] 7.6 Deliver the compatible Cloud gateway/runtime bridge through its existing deployment owners; verify originating OAuth binding, cell isolation and the human browser route before claiming Cloud recovery.

Outbound delivery has its own security and deployment boundary. It does not delay
the already-reviewed inbound preservation work or another accepted release base.
