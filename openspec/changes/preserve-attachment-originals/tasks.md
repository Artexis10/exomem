## 1. Specification

- [x] 1.1 Create the change with the `client-artifact-preservation` additions and pass strict validation.

## 2. File parameters

- [x] 2.1 Add failing coverage that `capture_source` declares `openai/fileParams` and that no schema takes a local path for a file.
- [x] 2.2 Declare the metadata on `capture_source`.

## 3. Held uploads

- [x] 3.1 Add failing coverage for holding on local ingress, byte-identical redemption, wrong-session, public-path and no-grant refusal, reuse, expiry, lane mismatch and integrity refusal.
- [x] 3.2 Add the hold store, the additive `hold=1` branch on `/upload`, and redemption in `stage_artifact` for both lanes.
- [x] 3.3 Hold from `exomem attach` when no destination is given, with `--lane`.

## 4. Transcriptions

- [x] 4.1 Add failing coverage that a transcription is written with its stored original and bound to its SHA-256, size and content type, is refused when it names no file, and is written nowhere when the original fails.
- [x] 4.2 Add `transcriptions` to `preserve_artifacts`, the `client-transcription` marker, and the compact terminal projection.

## 5. Guidance and delivery

- [x] 5.1 Update tool descriptions, bootstrap and the scaffold skill with custody-first guidance per client, within the compact byte budget.
- [x] 5.2 Regenerate the tool-schema baseline, plugin skills, hosted candidates and capabilities; run the scoped suites, lint, the privacy gate and strict OpenSpec validation.

## 6. Live acceptance

- [ ] 6.1 After release and connector refresh, accept each client path live: a ChatGPT conversation preserves attached images with `transcriptions`, a local client redeems an `exomem attach` handle over its local token, and claude.ai code execution uploads an attached file after the user's opt-in; each stored artifact's SHA-256 matches the original.
