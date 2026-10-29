## Context

The local speech backend called `model.transcribe(path)` with library defaults and dropped the returned `info`. With those defaults, `vad_filter` is off, so the library detects the language once on the first 30 seconds of unfiltered audio and applies it to the file. A silent or noisy opening gives a wrong, low-confidence language, and the decoder then produces text in that language for speech in another. With `condition_on_previous_text` on, a wrong or repeated stretch feeds the next window and the output loops.

The job runtime already separates a child that computes from a parent that publishes. The child writes a result row. The parent publishes it under the mutation guard, and a stale-sidecar check can reject the commit. One earlier incident lost a completed 60-minute transcript this way: the check rejected the commit and the stored result was deleted. Reprocessing must not repeat that.

## Goals / Non-Goals

**Goals:**
- Make the language of a transcript a recorded, controllable fact.
- Let a caller replace a bad completed extraction without losing it.
- Keep every existing guarantee of the automatic path.

**Non-Goals:**
- Detecting a wrong-language or looping transcript from its text. No word list, phrase list or regex decides what text means.
- Audio gain, normalization or other signal changes.
- A language default in code. The default is configuration (`EXOMEM_ASR_LANGUAGE`) or detection.
- Changing the MLX loader's audio decoding.

## Decisions

1. **The decoding policy is data.** `media_models.json` `speech.faster_whisper` holds `revision` and `options` (`vad_filter: true`, `condition_on_previous_text: false`, `beam_size: 5`, `language_detection_segments: 3`, `language_detection_threshold: 0.5`). The backend passes `options` through unchanged, so tuning is a data edit that a reviewer can read. The sidecar records `revision` as `asr_policy`, so a transcript says which policy produced it. The options are nested so that the backend never selects keys by name.
2. **Decode once, in one place.** The local backend decodes the file to mono float32 at 16 kHz with PyAV, through the reader that the ONNX backend already uses (`onnx_asr_runtime.decode_audio`). The library's own decoder goes through 16-bit integers and clips samples above full scale. The array also lets the backend pass the same input to every engine.
3. **Language precedence is request, pin, environment, detection.**
   - A request names a language on one `reprocess` call.
   - A pin is the language that an earlier request left on the sidecar (`asr_language_requested`). It keeps later runs on the same recording stable.
   - `EXOMEM_ASR_LANGUAGE` is the deployment default. Unset means none.
   - Detection runs on speech only when nothing else decides.
4. **The model decides which codes exist.** The local backend checks a code against `supported_languages` of the loaded model. The ONNX backend checks it against the `lang_to_id` table of the model's generation config. No list lives in code. An unsupported code fails the job with a message that lists the supported codes. The next action is to choose another code or change the configuration. The run never falls back to detection, because that would return the wrong-language text that the caller wanted to avoid.
5. **Facts travel as a typed mapping.** The backend fills an `AsrFacts` record. The job result carries it as validated data, and the sidecar renders it as frontmatter with correct YAML types: numbers stay numbers, and a probability that was never measured is `null`. A language that a caller or the configuration set has no probability, because nothing measured it.
6. **Reprocess is a flag on the job.** The job row gains `reprocess` and `language`. Only a row with the flag bypasses the "completed transcript" check. Automatic retry and reconciliation never set it, so the existing scenario "a valid completed transcript is not overwritten" holds. A published extraction clears the flag, so one request replaces one extraction.
7. **The prior extraction is a companion page in the same atomic write.**
   - The page is `<sidecar name>.prior-<n>.md` next to the sidecar. It holds the old extraction fields and text and is marked with the existing superseded convention: the pack's status of class `superseded`, and `superseded_by` naming the sidecar. Retrieval therefore demotes it with no new marker.
   - The page names no binary, so it is never a media item and no pass picks it up.
   - The sidecar lists the pages in `prior_extractions`.
   - The page and the sidecar are written in one batch. A reader sees the old text, or the new text with its prior page, and never a sidecar without text.
8. **Stale handling is narrowed for reprocess, and failure never touches the sidecar.**
   - An ordinary extraction commits only if the sidecar still matches the content the child read. A reprocess result commits if the sidecar still holds the same extraction (a digest of engine and text, named `replaces`) and the binary is unchanged. An edit elsewhere on the page, such as preserved notes, no longer rejects a long run. The write itself still compares exact bytes, so no edit is lost.
   - A reprocess that fails records the failure on the job row only. A completed sidecar keeps its text and its state, so no failure path leaves the sidecar without a transcript.
   - If the extraction itself changed during the run, the job fails with an actionable message and the prior text stays. The new text is lost in that case. The cost is one repeated run, and the case needs a person to edit the machine-owned text during the run.
   - If the binary is replaced during the run, the request is dropped, and reconciliation processes the new file.
9. **Reprocess uses the existing runtime.** It goes through the same job store, worker, brakes and per-artifact commit guard as `process` and `retry`. Cloud's memory brakes apply unchanged.
10. **PDF page markers are a plain line.** `[page N]` precedes each page's text. The timed-line pattern requires `m:ss`, so a page marker is not a timed line and the chunker and citation code read it as text.
11. **The skill procedure is generic.** The scaffold names classes of claims through the open observation category and the `(context)` slot. The user's own positions come only from the user, and a missing one stays a marked slot.

## Risks / Trade-offs

- [New transcripts differ from older ones for the same file] → Expected: VAD and no previous-text conditioning change the text. `asr_policy` records the revision. Old sidecars change only when someone reprocesses them.
- [A pin has no way to be cleared] → A new `reprocess` with another language replaces it. A clear-pin operation can follow if a case needs it.
- [A prior page is one more page in the vault] → It is demoted by status, linked from the sidecar, and numbered per sidecar. Repeated reprocessing grows the list by one page per run.
- [ONNX records no speech seconds] → The Silero VAD windows do not map to seconds in the current runtime, so `asr_speech_seconds` is null there rather than a guess.
- [MLX cannot check a code before it runs] → The MLX library rejects an unknown code itself. The job then fails with that message. The local and Cloud engines validate first.
- [A crash after the commit but before fanout] → The existing recovery publishes the sidecar's derived state. The companion page is indexed by the receipt drain that follows every batch write.

## Migration Plan

1. Ship the code. The job-store migration adds the two columns when the store opens and keeps old rows valid.
2. Regenerate the tool-surface contracts. Record the new digest as pending, and refresh the ChatGPT plugin after deployment.
3. No data migration. Existing sidecars have no `asr_*` fields until they are reprocessed.

## Open Questions

- Whether a pin needs an explicit clear operation.
- Whether `asr_speech_seconds` should come from the ONNX VAD once its window length is verified in seconds.
