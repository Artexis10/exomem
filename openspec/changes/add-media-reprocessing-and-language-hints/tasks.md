## 1. Speech policy and language

- [x] 1.1 Add the `speech.faster_whisper` policy and revision to `media_models.json`.
- [x] 1.2 Decode audio once through the shared PyAV reader, pass the array, the policy and the resolved language to the local backend, and keep the engine's `info`.
- [x] 1.3 Resolve the language by request, pin, environment, then detection. Validate a code against the model's published set, and fail unsupported codes durably without falling back.
- [x] 1.4 Let the ONNX backend accept the language, skip detection when it has one, report the detected language with a softmax probability, and key its resume record on the requested language.
- [x] 1.5 Record the engine's decisions in the sidecar frontmatter through `render_sidecar_extraction`.

## 2. Explicit reprocess

- [x] 2.1 Add the `reprocess` and `language` columns to the job store, with a guarded migration, and `request_reprocess`.
- [x] 2.2 Let only a reprocess row bypass the completed-transcript check. Write the prior extraction as a superseded companion page in the same atomic batch as the new sidecar.
- [x] 2.3 Narrow the stale check for reprocess to the extraction digest and binary identity. Record a failed reprocess on the job row only.
- [x] 2.4 Add the `reprocess` operation and `language` argument to `process_media` / `change_media`, with parity across MCP, REST and CLI, and the writer-lease and egress classification of a mutation.

- [x] 2.5 Block a pending reprocess on the job row alone when the runtime is marked unavailable, and refuse a new request then. Skip reprocess rows in the automatic retry pass.
- [x] 2.6 Give a prior page the `type`, `source_type` and `tags` of its sidecar, never reuse a page number, and leave Sources of the superseded class out of the compile backlog.

## 3. PDF and skill

- [x] 3.1 Emit `[page N]` before each page's text in `_extract_pdf`, including OCR pages.
- [x] 3.2 Extend the source compilation pass in the shipped scaffold for several recordings and documents, and update the media skill for reprocess.

## 4. Proof

- [x] 4.1 Add a worker-level test on a real sidecar: reprocess commits new text with `asr_*` fields and a linked superseded prior page, and automatic retry leaves a completed sidecar alone.
- [x] 4.2 Add unit cases where the logic branches: language precedence, unsupported code, ONNX requested language, PDF page markers.
- [x] 4.3 Regenerate the tool-surface contracts, `docs/capabilities.md` and the skill-contract stamps, and run `openspec validate --all --strict`.

## 5. Release

- [x] 5.1 After the release is installed, reprocess one long non-English recording with a language hint on the real speech models. Compare the new transcript with the prior page, and record the result here.
  Result (30 Oct 2026, release 0.150.0, desktop GPU, large-v3). The recording is a 21-minute Estonian lecture, reprocessed with `language: et`.
  - The sidecar recorded `asr_language: et`, `asr_language_source: requested`, `asr_language_mode: single`, `asr_decoding: batched`, `asr_policy: fw-policy-2`, and 1212 s of speech in 1284 s.
  - The prior text was kept as a linked `.prior-1` page.
  - The prior page had 236 timed lines, of which 34 were repeats, and 2907 words. The new page has 156 timed lines with no repeats and 2574 words. The same Estonian content sits in longer segments.
  - Word accuracy has no listened reference, so it is unverified in both versions.
- [ ] 5.2 Record the new tool-surface digest as pending in the connector contract, and ask the operator to refresh the ChatGPT plugin so that ChatGPT can send `reprocess`.
