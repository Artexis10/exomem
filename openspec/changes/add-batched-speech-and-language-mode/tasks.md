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
  Result (29 Oct 2026, RTX 5080, large-v3 fp16, batch 16, branch 391a5de16; sequential arm from the same policy):
  - Long lecture, `et`: batched 110 s against 355 s (3.2x); 342 lines against 364; median line start gap 6 s against 5 s; word overlap 0.83; longest identical run 4 against 2, in pre-lecture hall murmur.
  - English call, no language: detected `en` at 0.69; batched 5 s; 17 lines against 31; median start gap 19 s, because the call is mostly silence between turns; word overlap 0.84.
  - Looping lecture, `et`: batched 104 s; longest identical run 6 against 20; most-repeated line 9 against 30; word overlap 0.55 (low-intelligibility audio in both arms).
  - The sidecar facts named `batched`, `single` and `fw-policy-2` correctly. Batched ships: lines on the lectures are citable, and loops fell on the worst file.
- [x] 4.2 Regenerate the tool surface and record the new digest as pending in the connector contract.
