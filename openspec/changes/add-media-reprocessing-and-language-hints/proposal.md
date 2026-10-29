## Why

A long non-English recording was transcribed into text in the wrong language, with repeated lines. Three gaps caused it and kept it:

- The speech backend ran with no options and discarded the engine's language report. The engine detected the language once, on the first 30 seconds of raw audio. A quiet opening gave a low-confidence guess (0.35, below the library's 0.5 threshold), and that guess applied to the whole file.
- A completed transcript could not be replaced. Automatic retry correctly keeps a valid transcript, but no explicit request could extract again, with or without a language hint.
- The sidecar did not record what the engine decided, so a reader could not tell a wrong-language transcript from a correct one.

Two related gaps sit in the same workflow:

- PDF extracted text did not mark where a page starts, so a note could not cite a page.
- The shipped skill describes one source at a time. It has no procedure to compile many recordings and documents into notes with attribution and verification.

## What Changes

- **Speech policy is data.** `media_models.json` holds the faster-whisper options and a policy revision. The local backend decodes the audio itself, detects language on speech (VAD), stops one wrong stretch from steering the next, and keeps the engine's `info`.
- **Language has a fixed precedence.** An explicit request, then a pin kept on the sidecar, then `EXOMEM_ASR_LANGUAGE`, then detection on speech. The model's published language set validates a code. An unsupported code fails the job durably and never falls back to detection. The ONNX backend (used in Cloud) accepts the resolved language and skips detection when it has one.
- **The sidecar records the engine's decisions.** `asr_language`, `asr_language_probability`, `asr_language_source`, `asr_language_requested`, `asr_speech_seconds`, `asr_duration_seconds` and `asr_policy`.
- **`reprocess` is an explicit media operation.** It takes an optional `language`. It requeues a completed audio, video or PDF item and replaces the extraction. In the same atomic write, the prior extraction moves to a linked page next to the sidecar, marked with the existing superseded status. Automatic retry is unchanged.
- **PDF text marks pages.** A `[page N]` line precedes each page's text, including pages read by OCR.
- **The skill scaffold gains a multi-source compilation procedure** and a reprocess hint for wrong-language or looping transcripts.

## Capabilities

### Modified Capabilities

- `automatic-media-processing`: language precedence, recorded engine decisions, policy as data, explicit reprocessing that keeps the prior extraction.
- `command-surface`: `process_media` and `change_media` gain the `reprocess` operation and a `language` argument, with parity across MCP, REST and CLI.
- `multimodal-job-runtime`: PDF extracted text names each page.

## Impact

- Code: `src/exomem/extract.py`, `onnx_asr_runtime.py`, `media_models.json`, `media_jobs.py`, `media_worker.py`, `media_processing.py`, `preserve.py`, `commands.py`, plus `asr_facts.py` and `prior_extraction.py`.
- Tool surface: `change_media` gains the `reprocess` kind and `language`. The pinned tool-surface digest moves, and the generated contracts and fixtures are regenerated together. ChatGPT clients see the new kind after a plugin refresh.
- Data: a job row gains `reprocess` and `language` columns, added by a guarded migration. A sidecar gains the `asr_*` fields and, after a reprocess, `prior_extractions`. A prior-extraction page is a new page kind next to the sidecar. It is not a media item.
- Behaviour: new local transcripts use VAD and no previous-text conditioning, so text can differ from earlier runs of the same file. Existing sidecars stay as they are until someone reprocesses them.
- Cloud: no deployment or configuration change. The ONNX backend ships in the same source.
