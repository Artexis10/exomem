## Why

The local speech stage decodes one 30-second window after another. On a GPU, faster-whisper's batched pipeline decodes many VAD-selected windows at once. On a 60-minute lecture it took 61 s against 380 s for the sequential run, on the same GPU and model. With segment timestamps kept, as shipped, it took 110 s against 355 s. The first batched run had one defect: it wrote one line per 25–30 s window, which is too coarse to cite. The cause is the pipeline's default `without_timestamps=True`, not batching itself.

Recordings can also hold more than one language, such as a lecture with an English aside or a bilingual meeting. Today a caller can pin one language only on a reprocess call, and only one. Nothing lets the agent say at processing time that a recording holds several languages. The engine supports per-window detection (`multilingual=True`), which cost 34% more time on one Estonian lecture and changed no words there.

## What Changes

- **Batched decoding on a GPU.** When the speech stage runs on CUDA, it uses the batched pipeline with a batch size from `media_models.json`. It keeps segment timestamps, so lines stay short enough to cite. CPU runs stay sequential.
- **One language or several, declared at processing time.** `process_media` accepts `language` (one code) or `multilingual: true` on the first processing request as well as on `reprocess`. A declared mode pins on the sidecar like a language does. The shipped media skill tells the agent to ask "one language or several?" at ingest when it cannot tell, and to say that several is slower.
- **The sidecar records the mode.** `asr_language_mode` is `single` or `multilingual`. With `multilingual`, `asr_language` holds the language that the engine detected on the first speech windows, and `asr_language_source` is `requested`.
- **A hallucination guard, only if it pays.** The sequential path can skip silent stretches where the engine invents text (`hallucination_silence_threshold`). The policy enables it only if a measured run on a recording with known loops removes them without losing speech. The batched pipeline ignores the option, and it decodes each window independently, so a loop cannot carry from one window into the next.
- **Policy revision `fw-policy-2`, and the decoding path.** Sidecars name the revision that produced them. A new `asr_decoding` fact (`batched` or `sequential`) tells a batched transcript from a sequential one, because both run under the same revision.

## Capabilities

### Modified Capabilities

- `automatic-media-processing`: batched GPU decoding with timestamped lines, a declared language mode at processing time, and the recorded mode.
- `command-surface`: `process_media` accepts `language` and `multilingual` on `process` and `reprocess`, with parity across MCP, REST and CLI.

## Impact

- Code: `src/exomem/extract.py` (backend mode choice and options), `media_models.json` (batched block, revision), `media_jobs.py` (mode column), `media_worker.py`, `media_processing.py`, `asr_facts.py`, `commands.py`.
- Tool surface: `process_media` gains `multilingual` and accepts `language` on `process`. The pinned digest moves again; the connector contract records it as pending.
- Data: a job row gains a language-mode column through a guarded migration. A sidecar gains `asr_language_mode`. Existing sidecars stay as they are until reprocessed.
- Behaviour: a GPU deployment's new transcripts come from the batched pipeline. Text can differ from a sequential run of the same file.
- Depends on `add-media-reprocessing-and-language-hints`.
