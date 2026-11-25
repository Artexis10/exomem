## Why

Evermatter Cloud transcribes recordings on the cell's two CPUs. A 30-minute recording takes about 70 minutes in English and 87 minutes in Japanese (`add-cloud-multimodal-processing`, ONNX Whisper large-v3-turbo int8). A cell cannot label speakers at all: the image has no PyTorch runtime, and pyannote 3.1 on a CPU runs slower than real time (RTF 1.13 to 1.26) with a 2.9 GiB peak, against a 1 GiB speech budget and a 3 GiB pod limit.

An owner who moves a media-heavy vault to Cloud loses fast transcripts and speaker labels. The same owner often has a GPU at home. On a desktop-class GPU, the existing personal engines transcribe with speaker labels at about 6 times real time; on a laptop-class GPU, Turbo transcribes at about 33 times real time; GPU pyannote labels a 3-hour call in under 3 minutes (benchmarks of 2026-11-01, 2026-11-21 and 2026-11-23).

This change lets the owner's own GPU machine do the speech work for their Cloud cell, and keeps the results in Cloud. It defines the worker contract so that a platform-operated GPU pool can later serve cells through the same contract, without a redesign.

## What Changes

- **Speech worker mode for a cell.** A cell can leave speech jobs for an outside worker instead of running them on its CPU. Documents, OCR and every other stage keep running in the cell. The mode is per-cell configuration; the default stays in-cell processing. An optional fallback runs a job on the cell's CPU after it has waited a configured time.
- **A speech work contract on the cell.** Behind the gateway, a worker claims one speech job as an expiring lease, extends the lease with progress, and completes it with raw ASR segments and speaker turns, or releases it with a reason. Completion is idempotent and bound to the original's SHA-256.
- **The cell keeps the transcript logic.** The cell merges segments and speaker turns, applies voiceprint naming and publishes the sidecar through the existing claim-fenced result path. A worker never writes rendered text.
- **A narrow, revocable worker credential.** The owner authorizes a worker once with a new OAuth scope that reads only originals under a live lease that the worker holds, and writes only speech results. Home lists the authorization, and the owner can revoke it there.
- **`evermatter speech-worker`.** One CLI command sets up the owner's GPU machine (Linux or WSL first): it signs in with the speech scope, prepares the existing GPU engines and diarizer, and installs a user service. The service claims, downloads with verification, transcribes, labels speakers, completes, and deletes the local copy.
- **Status the owner can read.** Status shows speech jobs that wait for a worker, the last worker contact, and jobs in fallback.
- **Principal-neutral contract.** The cell authorizes a claim by principal and scope, not by the kind of machine. A platform GPU pool is a later principal; this change builds none.

## Capabilities

### New Capabilities
- `remote-speech-worker`: the speech work contract, the worker credential, the owner's worker command and the worker status.

### Modified Capabilities
- `automatic-media-processing`: a cell in worker mode keeps speech jobs pending for a worker, and its fallback runs them in the cell.

## Impact

- Cell: media job ledger (lease columns), new `/speech/*` routes behind the gateway proof, a pure merge function extracted from `extract._diarize`, status fields.
- Substrate: gateway routes and scope admission for `/speech/*` and lease-bound `/download`; the OAuth scope; Home's authorization list.
- cellctl and chart: per-cell speech mode and fallback in the store of engine settings.
- CLI: `evermatter speech-worker setup|run|status`.
- Cloud rehearsal: a worker-mode scenario with a CPU-sized model.
- No change to personal installs, which keep running speech in-process.
