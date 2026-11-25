## Context

A Cloud cell runs speech in its media worker on two CPUs (`add-cloud-multimodal-processing`). That design lists diarisation, GPUs and a shared inference service as non-goals. The cell image has no PyTorch runtime, and a build gate asserts it. Cloud refuses any non-CPU speech device (`cloud_cell.py`).

Speech already has the seams a remote worker needs:
- the job ledger (`media_jobs.py`): one job per `(binary_rel, media_type)`, with states, attempts and a `claim_revision` fence;
- the result boundary: the media child writes a validated payload with `record_result`, and the parent publishes it only while the sidecar and the original's identity still match;
- the ASR backend protocol (`TranscriptionBackend`, `extract.py`), which returns timed segments;
- the diarizer sidecar contract: audio in, `{"turns": [{start, end, label}]}` out;
- the verified original download (`cloud_download_client.py`), which checks `x-exomem-size` and `x-exomem-sha256`;
- the pending sidecar, which carries `binary_sha256` and `binary_size`.

Measured rates (sources in the proposal): cell CPU Whisper 0.43× real time; CPU pyannote RTF 1.13 to 1.26 at 2.9 GiB; a desktop-class GPU running large-v3 with speaker labels about 6× real time; a laptop-class GPU running Turbo about 33× real time; GPU pyannote RTF 0.015.

Owners asked to use their own GPU machines for Cloud speech, and for an architecture that admits a shared Cloud GPU worker later.

The design follows the "Scaling managed agents" entry of the agentic-engineering source roster: keep a narrow, stable interface between the place that owns the state and the place that runs the work, so either side can be replaced.

## Goals / Non-Goals

**Goals:**
- Fast transcripts and speaker labels for a Cloud vault, computed on the owner's own GPU.
- Results land in the same sidecars, with the same rendering, as in-cell speech.
- Setup is one command on the owner's machine. The worker survives sleep, restarts and network loss without duplicates or lost work.
- A later platform GPU pool uses the same contract.

**Non-Goals:**
- A platform GPU pool, rented GPUs, or any third-party speech API.
- Diarisation on cell CPUs (D9).
- Windows-native and macOS service installers (the command runs; the user service is Linux and WSL first).
- Other media stages (OCR, CLIP, frames) on the worker.

## Decisions

### D1. The worker pulls; the cell never calls out

The worker claims work over HTTPS through the gateway. The cell never opens a connection to the owner's machine, which sits behind NAT and sleeps.

A claim long-polls for up to 25 seconds, inside the gateway's idle limit, and returns `204` when no job is ready. The cell holds no database connection while it waits: it re-checks the ledger at a bounded interval and on its own enqueue signal. An idle worker therefore costs about two requests a minute.

*Alternative rejected:* the cell pushes jobs to a registered worker URL. It needs inbound reachability, and a sleeping laptop turns every push into a timeout.

### D2. A lease on the existing job ledger

A claim moves one pending speech job to `running` for the claiming principal. It bumps `claim_revision` and sets `lease_expires_at` to ten minutes ahead. A heartbeat every 60 seconds extends the lease and records progress (`stop_offset`, the seconds of media done). When a lease expires, the job returns to `pending` through the same recovery that already returns abandoned in-cell claims.

Claim order is the ledger's existing order. A cell grants at most two live leases per principal, so one owner's workers cannot hold the whole backlog.

### D3. Completion is idempotent and bound to the original

`complete` carries the job ID, the `claim_revision` and the original's SHA-256 that the worker verified on download.

- A stale revision returns `409 stale_claim`, and the cell keeps nothing.
- A SHA-256 that differs from the sidecar's `binary_sha256` returns `409 original_changed`.
- A repeat of an accepted completion returns `200 already_completed` with the first result's digest.
- An accepted completion is recorded with `record_result` before the response. The parent publishes it as it does any in-cell result, including the stale-sidecar check.

### D4. The worker returns raw results; the cell renders

The payload holds:
- ASR segments: `{start, end, text}`, with the `asr_*` fields that the in-cell engine records (language, probability, source, mode, decoding, speech and duration seconds);
- speaker turns: `{start, end, label}`, when the job asked for labels;
- per-label speaker embeddings, when the job asked for labels and the cell holds voiceprints (open question 1);
- the engine identity: model ID, model revision, backend and device class.

The cell validates the payload against a schema with bounds: segment count and text bytes scale with the original's duration, and the body is at most 32 MiB. The cell then runs the merge that `extract._diarize` runs today. This change extracts it into one pure function that takes segments and turns, so in-cell and worker results go through the same code.

*Alternative rejected:* the worker uploads rendered text. That would put a second copy of the merge, labelling and timed rendering on every worker, and voiceprint naming would need the cell's voiceprints on the worker.

### D5. A narrow, revocable credential

The worker signs in through the existing CLI OAuth flow with a new scope, `exomem.speech`. That scope admits exactly these requests:
- `POST /speech/claim`, `/speech/heartbeat`, `/speech/complete` and `/speech/release`;
- `GET /download` for a path with a live lease that this principal holds.

The gateway admits the scope on those routes only and forwards the principal and scope in its per-request proof (media store D8). The cell checks the lease for `/download`, because only the cell knows it. A speech-scoped token cannot read notes, search, call MCP tools or upload.

Home lists the authorization with its last use, and the owner revokes it there. Rate limits and concurrency slots apply per principal, as on `/download` today.

### D6. Mode, fallback and status

cellctl renders a per-cell `speech_mode` (`local` or `worker`) and an optional `worker_fallback_after` duration, through the existing per-cell engine settings. The default stays `local`.

In `worker` mode the cell's media worker skips speech jobs, so documents and OCR run as before. When `worker_fallback_after` is set and a job has waited that long with no live lease, the cell runs it on its CPU (speech without labels), if the cell has the speech engine.

Status (`process_media status` and the tenant tools) reports:
- the speech jobs that wait for a worker, and since when;
- the last worker contact and its principal's display name;
- each job in fallback.

### D7. The owner's worker command

`evermatter speech-worker setup` runs once on the owner's machine:
1. sign in with `exomem.speech`;
2. detect a CUDA GPU (CPU works but is slow, and setup says so);
3. prepare the existing engines and the diarizer sidecar venv;
4. install a systemd user unit on Linux and WSL.

`evermatter speech-worker run` loops: claim, verified download into a private temporary directory, transcribe, label speakers, complete, delete. It runs one job per GPU. It keeps partial segments in a local file keyed by the original's SHA-256, so a restart resumes from `stop_offset` instead of starting over. `evermatter speech-worker status` prints the cell's view of the worker queue.

The command reuses the personal install's backends (`FasterWhisperBackend`, the diarizer sidecar), so its output is the personal engine's output.

### D8. A platform GPU pool later, without a redesign

The cell authorizes by principal and scope, not by the machine. A platform-operated pool would be one more principal: a platform service credential that the gateway admits for cells whose owners opted in, under the same routes and the same lease rules. It needs a privacy copy change and an operator decision before it exists. This change builds none of it.

### D9. Why a cell does not run diarisation on its CPU

- The image has no PyTorch runtime, and a build gate keeps it that way (the no-PyTorch contract in `add-cloud-multimodal-processing` design.md). pyannote 3.1 needs PyTorch.
- pyannote 3.1 on a CPU peaks at 2.9 GiB. The speech child's budget is 1 GiB of anonymous memory, and the pod's limit of 3 GiB also holds the serving process.
- It runs at 1.13 to 1.26 times real time on 4 CPU threads, and a cell has 2. A 1-hour meeting would hold the cell's media slot for over 2 hours, after a 2.3-hour transcription.
- The ONNX alternative evaluated on 2026-11-01 (sherpa-onnx) merged speakers too often to replace pyannote.

A cell could run it only with a larger, PyTorch-bearing image and memory budget per cell. That costs every cell and is slower than the owner's GPU by two orders of magnitude.

### D10. Scale

The claim is one indexed query on the ledger by state and order, bounded to one row. The long poll holds no connection while it waits. `complete` does bounded validation and one ledger write; the existing parent path publishes in the background. Per-principal lease caps and rate limits keep one owner's workers from starving the cell. The cost of an idle worker is two requests a minute per worker.

## Risks / Trade-offs

- **The worker sees plaintext originals.** It runs on the owner's own machine under the owner's credential, the same trust as their local vault. A platform pool (D8) changes this and needs its own decision.
- **A worker that disappears holds a job for one lease.** The cost is a ten-minute delay; the fallback bounds the worst case.
- **Engine drift between worker and cell.** The engine identity in the payload is recorded in the sidecar, so a reader sees which model produced the text.
- **Two surfaces to keep in step.** The gateway's scope admission and the cell's lease check must agree. The rehearsal scenario exercises both.

## Migration Plan

1. Ship the cell, gateway, Substrate and CLI pieces with every cell in `local` mode.
2. Rehearse worker mode in the Cloud rehearsal with a CPU-sized model.
3. Under an operator-approved live change, switch the owner's cell to `worker` with a fallback. Run the worker on the owner's GPU machine.
4. Undo: set the cell back to `local`. Pending jobs then run in the cell; worker results already published stay.

## Open Questions

1. Does voiceprint naming need per-label embeddings from the diarizer, or can the cell compute them from the original? The answer decides whether the payload carries embeddings.
2. Is the 25-second long poll within the gateway's idle timer on every path, or should the claim return at once and the worker back off?
3. When do Windows-native and macOS service installers matter?
