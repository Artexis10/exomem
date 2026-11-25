## Why

Evermatter Cloud transcribes recordings on the cell's two CPUs. A 30-minute recording takes about 70 minutes in English and 87 minutes in Japanese (`add-cloud-multimodal-processing`, ONNX Whisper large-v3-turbo int8). A cell cannot label speakers at all: the image has no PyTorch runtime, and pyannote 3.1 on a CPU runs slower than real time (RTF 1.13 to 1.26) with a 2.9 GiB peak, against a 1 GiB speech budget and a 3 GiB pod limit.

An owner who moves a media-heavy vault to Cloud loses fast transcripts and speaker labels. An owner with a GPU machine can do much better. On a desktop-class GPU, the existing personal engines transcribe with speaker labels at about 6 times real time; on a laptop-class GPU, Turbo transcribes at about 33 times real time; GPU pyannote labels a 3-hour call in under 3 minutes (benchmarks of 2026-11-01, 2026-11-21 and 2026-11-23).

This change lets the owner's own GPU machine do the speech work for their Cloud cell, and keeps the results in Cloud. It defines the speech worker contract so that a platform-operated GPU pool can later serve cells through the same contract, without a redesign.

## What Changes

- **Remote speech for a cell.** A cell setting, `speech_mode: remote`, records each recording's speech stage as a speech item for a speech worker instead of running it on the cell's CPU. Every other stage keeps running in the cell. The default stays `in_cell`. An optional fallback runs a long-waiting item in the cell, without speaker labels.
- **A speech work contract.** Behind the gateway, a speech worker claims one speech item as an expiring lease, extends it with progress, fetches the leased original, and completes with raw ASR segments, speaker turns and, when naming is on, speaker embeddings. Completion is accepted once, for the current lease and the original's SHA-256.
- **The cell keeps the transcript logic.** The cell renders the transcript with the existing merge, labelling and naming, and publishes through the existing stale-input check.
- **A narrow, revocable credential.** A new OAuth scope, `exomem.speech`, reaches only the speech routes. The gateway signs those requests with a new proof purpose, so a cell built before this change refuses them. Home lists and revokes speech authorizations.
- **`evermatter speech-worker`.** One command sets up the owner's GPU machine (Linux or WSL first) and installs a user service. The service claims, downloads with verification, transcribes, labels speakers, completes, and deletes its local copy.
- **Status.** Status shows waiting speech items, the last speech worker contact, and fallback transcripts.
- **Principal-neutral contract.** A later platform-operated speech worker would be one more principal under the same contract. This change builds none.

## Capabilities

### New Capabilities
- `remote-speech-worker`: the speech work contract, the worker credential, the owner's worker command and the worker status.

### Modified Capabilities
- `cloud-multimodal-processing` (from `add-cloud-multimodal-processing`, which archives first): a Cloud cell in remote speech hands its speech stage to the owner's authorized speech worker.

## Impact

- Cell: a speech item table with leases, `/speech/*` routes behind a new `speech` proof purpose, a pure merge function extracted from `extract._diarize`, completion receipts, status fields.
- Substrate: gateway routes, the `speech` proof purpose and scope admission for `/speech/*` only; the OAuth scope; Home's authorization list.
- cellctl and chart: per-cell `speech_mode` and `remote_speech_fallback_after` through the engine settings.
- CLI: `evermatter speech-worker setup|run|status`.
- Cloud rehearsal: a remote speech scenario with a CPU-sized model.
- No change to personal installs, which keep running speech in-process.
