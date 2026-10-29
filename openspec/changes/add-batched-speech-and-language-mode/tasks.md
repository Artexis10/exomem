## 1. Batched GPU decoding

- [ ] 1.1 Add the batched block (`batch_size`, `without_timestamps: false`) and revision `fw-policy-2` to `media_models.json`.
- [ ] 1.2 Let the faster-whisper backend choose the batched pipeline on CUDA and stay sequential on CPU.

## 2. Language mode

- [ ] 2.1 Add the language-mode column to the job store with a guarded migration.
- [ ] 2.2 Accept `language` and `multilingual` on `process` and `reprocess` across MCP, REST and CLI; refuse both together and refuse a language on non-speech media.
- [ ] 2.3 Pin the mode on the sidecar and record `asr_language_mode`.
- [ ] 2.4 Teach the media skill to ask "one language or several?" at ingest when unknown, and to say that several is slower.

## 3. Hallucination guard

- [ ] 3.1 Let the sequential policy carry `hallucination_silence_threshold`, off by default.
- [ ] 3.2 Run it on a recording with known silence loops; turn it on in the policy only if it removes loops without dropping speech, and record the result here.

## 4. Proof and rollout

- [ ] 4.1 Run the branch's `extract_text` on the desktop GPU, both arms, on a long single-language lecture, an English call and a looping lecture; record time, lines, median line duration, word overlap and the longest identical run.
- [ ] 4.2 Regenerate the tool surface and record the new digest as pending in the connector contract.
