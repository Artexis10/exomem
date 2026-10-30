## Context

`add-media-reprocessing-and-language-hints` made the speech policy data, detected language on speech, recorded the engine's decisions and added an explicit `reprocess`. It kept the sequential `WhisperModel.transcribe`. Probes on an RTX 5080 with faster-whisper 1.2.1 and large-v3 (fp16), on recordings from a real 60-minute lecture, measured:

| Probe | Result |
|---|---|
| Batched, batch 16, `et`, default options | 61 s against 380 s sequential; 95 lines against 364; one line per 25–30 s window |
| Batched, batch 16, `et`, `without_timestamps: false` (shipped) | 110 s against 355 s sequential; 342 lines against 364; median line start gap 6 s |
| Sequential `multilingual=True`, same file | identical words to the single-language run; 34% slower |
| Sequential `multilingual=True`, an English call | clean English |

In faster-whisper 1.2.1, `BatchedInferencePipeline.transcribe` defaults `without_timestamps=True`, so each VAD window yields one segment. The same call documents `condition_on_previous_text` and `hallucination_silence_threshold` as unused.

## Goals / Non-Goals

**Goals:** faster local transcription on a GPU with citable lines, a declared one-or-several language mode at processing time, and a hallucination guard only if a measurement shows it pays.

**Non-Goals:** speaker labels, a second speech model, batched decoding on CPU, Cloud object-storage changes, and per-line language tags in the sidecar.

## Decisions

### Batched decoding only on CUDA, with segment timestamps

The backend chooses the batched pipeline when its device is CUDA, with `without_timestamps: false` and `batch_size` from `media_models.json`. Segment timestamps make the decoder emit timestamped segments inside each window. That gives lines of sentence-like length, decided by the model and not by punctuation rules in code. CPU stays sequential. Batching on CPU trades memory for little measured gain, and the deployments that run on CPU are the memory-constrained ones.

A GPU that is too small for the batch size fails on the first batch with CTranslate2's out-of-memory `RuntimeError`. When that happens before the pipeline yields a segment, the backend decodes the same file sequentially and records `asr_decoding: sequential`. CTranslate2 has no exception type for it, so the check reads the library's documented message. An error after the first segment stays the job's failure, because a retry decodes the file again from the start.

Alternative considered: `word_timestamps=True` and re-splitting lines on pauses. It adds a cross-attention pass and a splitting rule to maintain. Choose it only if segment timestamps still give lines too coarse to cite.

### Language mode is one declared choice

A request carries `language` (one code) or `multilingual: true`, never both. A declared mode follows the existing precedence: a request beats the sidecar's pin, the pin beats `EXOMEM_ASR_LANGUAGE`, and that beats detection. The sidecar pins the mode with the language. `multilingual` runs per-window detection in either pipeline. The agent asks the person only when the recording's languages are unknown; it does not ask for every file.

### Hallucination guard is measured before it ships

The sequential path accepts `hallucination_silence_threshold` from the policy, which needs `word_timestamps=True`. The proof run on a recording with known silence loops decides whether the policy turns it on. The policy keeps it off when the run removes no loop or drops speech. The batched path does not need it: each window decodes independently, with no previous-text conditioning to carry a loop forward.

## Risks / Trade-offs

- Batched text can differ from sequential text on the same file → the policy revision moves to `fw-policy-2` and the sidecar records `asr_decoding`, so a reader can tell them apart, and the proof compares the two arms on the same files.
- Batch 16 with large-v3 fp16 needs more VRAM than the sequential run, and the CUDA admission probe only requires 2 GiB of free memory, so it does not gate batching → a first-batch out-of-memory error falls back to sequential decoding for that file, `asr_decoding` records which path ran, and the batch size is data.
- `multilingual` costs time → the skill states the cost when it asks.

## Proof plan

Run the branch's `extract_text` on the desktop GPU on three recordings: a long single-language lecture, an English call, and a lecture with known silence loops. For each arm (sequential, batched), record:

- wall time;
- line count and median line duration;
- word overlap against the sequential run;
- the longest run of identical lines;
- the recorded `asr_*` facts.

Batched ships when its lines are citable and its loop count is no worse than sequential. The transcript text carries line starts only, and a start gap counts silence between turns. So citable means a median of 15 words per line or fewer. This criterion replaced the first draft's 15-second line duration on 29 October 2026, after the first run showed that a start gap counts silence. By the old criterion, the English call fails at 19 s.
