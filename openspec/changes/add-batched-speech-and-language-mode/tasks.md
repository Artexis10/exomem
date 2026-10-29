## 1. Batched GPU decoding

- [x] 1.1 Add the batched block (`batch_size`, `without_timestamps: false`) and revision `fw-policy-2` to `media_models.json`.
- [x] 1.2 Let the faster-whisper backend choose the batched pipeline on CUDA and stay sequential on CPU.

## 2. Language mode

- [x] 2.1 Add the language-mode column to the job store with a guarded migration.
- [x] 2.2 Accept `language` and `multilingual` on `process` and `reprocess` across MCP, REST and CLI; refuse both together and refuse a language on non-speech media.
- [x] 2.3 Pin the mode on the sidecar and record `asr_language_mode`.
- [x] 2.4 Teach the media skill to ask "one language or several?" at ingest when unknown, and to say that several is slower.

## 3. Hallucination guard

- [x] 3.1 Let the sequential policy carry `hallucination_silence_threshold`, off by default.
- [x] 3.2 Run it on a recording with known silence loops; turn it on in the policy only if it removes loops without dropping speech, and record the result here.
  Result (29 Oct 2026, RTX 5080, large-v3 fp16, threshold 2.0, sequential path, a 60-minute lecture with hall murmur): longest identical run 8 against 20 without the guard, most-repeated line 11 against 30, words 3,807 against 3,993, time 340 s. It removed loops, but no listened reference shows whether the 186 fewer words were all invented text. The guard stays off.

## 4. Proof and rollout

- [x] 4.1 Run the branch's `extract_text` on the desktop GPU, both arms, on a long single-language lecture, an English call and a looping lecture; record time, lines, median line duration, word overlap and the longest identical run.
  Result (29 Oct 2026, RTX 5080, large-v3 fp16, batch 16, branch 391a5de16). The sequential arm used the same policy, from the earlier sequential run of each file. Run files: `result.json` and `metrics.py` from the acceptance run, kept privately with the recordings.
  - Long lecture, `et`: batched 110 s against 355 s sequential (3.2x). 342 lines against 364. Median words per line 11.5 against 9. Median start gap 6 s against 5 s. Word overlap 0.83. Longest identical run 4 against 2, in pre-lecture hall murmur.
  - English call, no language: detected `en` at 0.69. Batched took 5 s. 17 lines against 31. Median words per line 13 against 3. Median start gap 19 s against 3.5 s, because the call is mostly silence between turns. Word overlap 0.84.
  - Looping lecture, `et`: batched 104 s. 614 lines against 565. Median words per line 5 against 5. Longest identical run 6 against 20. Most-repeated line 9 against 30. Word overlap 0.55, from low-intelligibility audio in both arms.
  - The sidecar facts named `batched`, `single` and `fw-policy-2` correctly.
  - The transcript text carries line starts only, so the run measured words per line instead of line duration (see design.md). Every batched arm stays at a median of 15 words or fewer. Loops fell on the worst file and rose only in murmur on one other. Batched ships.
  - Word accuracy has no listened reference, so it is unverified in both arms.
- [x] 4.2 Regenerate the tool surface and record the new digest as pending in the connector contract.
- [ ] 4.3 After the release is installed, refresh the ChatGPT plugin so that it sends `multilingual`, then process one recording in each language mode on the installed service and record the sidecar facts here.
