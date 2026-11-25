## Context

A Cloud cell runs speech in its media worker child on two CPUs (`add-cloud-multimodal-processing`). That change requires that a Cloud cell extracts media only inside the cell, and it lists diarisation, GPUs and a shared inference service as non-goals. The cell image has no PyTorch runtime, and a build gate asserts it. Cloud refuses any non-CPU speech device (`cloud_cell.py`).

What exists today, and what this design builds on:
- The job ledger (`media_jobs.py`) keeps one row per `(binary_rel, media_type)`. One row can carry several stages: a video row carries speech and frame vectors together (`media_processing.py`). The row has a `claim_revision` fence.
- `recover_interrupted` returns every `running` row to `pending` at each media child start and exit, with no time check.
- The child records a rendered payload with `record_result`. The parent publishes it only while the sidecar hash and the original's identity still match, then deletes the job and result rows.
- `TranscriptionBackend` (`extract.py`) returns timed segments. The diarizer sidecar returns `{"turns": [...]}`. `extract._diarize` merges them.
- Voiceprint naming embeds speakers with speechbrain on PyTorch (`voice_embed.py`), which a cell cannot run.
- The gateway signs a per-request proof whose claims are fixed per purpose (`cloud_origin.py`). The Cloud `/download` route gives any valid download proof the owner's read rights.
- `cloud_download_client.py` verifies `x-exomem-size` and `x-exomem-sha256` on download.

Measured rates, recorded outside this repository and re-measured in task 5.3: cell CPU Whisper about 0.43× real time; CPU pyannote RTF 1.13 to 1.26 with a 2.9 GiB peak; a desktop-class GPU running large-v3 with speaker labels about 6× real time; a laptop-class GPU running Turbo about 33× real time; GPU pyannote RTF 0.015.

The design follows Anthropic's engineering article on scaling managed agents: keep a narrow, stable interface between the place that owns the state and the place that runs the work, so either side can change.

## Goals / Non-Goals

**Goals:**
- Fast transcripts and speaker labels for a Cloud vault, computed on the owner's own GPU.
- Results land in the same sidecars, with the same rendering, as in-cell speech.
- Setup is one command on the owner's machine. The speech worker survives sleep, restarts and network loss without duplicates or lost work.
- The contract stays the same if a platform-operated GPU pool later becomes a speech worker.

**Non-Goals:**
- A platform GPU pool, rented GPUs, or any third-party speech API.
- Diarisation on cell CPUs (D10).
- Other media stages on the speech worker.

## Terms

- **Speech worker:** the process on the owner's machine (later, possibly a platform pool) that runs speech for a cell.
- **Media worker:** the cell's existing media child. Its name and its `EXOMEM_MEDIA_WORKER_MODE` setting are unchanged.
- **Remote speech:** the cell setting `speech_mode: remote`. The other value is `in_cell`, the default.
- **Speech item:** the unit of remote work, one per recording (D3).

## Decisions

### D1. The speech worker pulls; the cell never calls out

The speech worker claims work over HTTPS through the gateway. The cell never connects to the owner's machine, which sits behind NAT and sleeps.

A claim waits up to a bounded time for work, then returns `204` when none is ready. The bound is set below the gateway's idle limit on the claim's path, which task 2.1 measures. While it waits, the cell holds no database connection: it re-checks at a bounded interval and on its own enqueue signal. A principal may hold one open claim at a time.

*Alternative rejected:* the cell pushes to a registered speech worker URL. It needs inbound reachability, and a sleeping laptop turns each push into a timeout.

### D2. The speech item is a ledger row with a lease

A speech item is a row in the existing `jobs` ledger with its own `media_type`, `speech`, as offload (`binary`) and preview rows already are. Its `running` state is the lease. The row gains the leasing principal and `lease_expires_at`, and keeps the existing `claim_revision` fence. `record_result`, the stale-input check and `finalize_result` therefore apply to it unchanged, and publication has one owner.

- A claim takes one `speech` row that is `pending`, or `running` with `lease_expires_at` in the past. It sets `running`, increments `claim_revision`, records the principal, and sets the expiry ten minutes ahead. The expiry check lives in the claim query itself.
- A heartbeat by the same principal at the same revision extends the expiry and records progress. Otherwise it answers `lease_lost`.
- `recover_interrupted` skips `speech` rows, so the media child's starts and exits never take a lease back. Only the claim query expires a lease.
- A cell grants at most two live leases per principal.

### D3. The speech stage leaves the recording's media row

In remote speech, the cell keeps creating the speech stage for each recording, as it does in-cell, but as its own `speech` row instead of a stage of the recording's media row. The media row keeps its other stages, such as a video's frame vectors, and the media worker runs them as before. A lease covers only the `speech` row.

When the media worker finishes the other stages first, the sidecar shows them, and the transcript follows when the speech row completes. When the speech engine is switched off for the cell, the cell creates no speech row, as today. Reconciliation creates no second `speech` row for a recording that has one in `pending` or `running`.

### D4. Completion: the cell renders, then publishes once

A completion carries the item, its `revision` and the original's SHA-256 that the speech worker verified.

- The cell accepts it only for a `leased` item held by the same principal at the same revision. A stale revision answers `stale_claim`, and an expired-then-reclaimed lease answers `lease_lost`. In both cases the cell keeps nothing.
- A SHA-256 that differs from the sidecar's `binary_sha256` answers `original_changed`.
- The cell validates the payload against a schema with bounds. Segment count and text bytes scale with the recording's duration, every timestamp lies within it, and the body is at most 32 MiB.
- The cell renders the transcript, speaker labels and timed text with the merge from `extract._diarize`. This change extracts that merge into one pure function, so in-cell and remote results go through the same code. The cell records the rendered payload with `record_result`, together with the sidecar hash captured at claim time. The parent publishes it with the existing stale-input check.
- `finalize_result` deletes the row after publication, so the cell keeps a receipt per accepted completion: the recording, revision and result digest, for 30 days. A repeat answers `already_completed` with that digest and no second publication.

*Alternative rejected:* the speech worker uploads rendered text. That puts a second copy of the merge, labelling and timed rendering on every speech worker.

### D5. Speaker names and embeddings

Speaker naming needs an embedding per speaker, and a cell cannot compute one (no PyTorch). When the owner has turned voiceprint naming on, the payload carries one embedding per diarised label. The cell matches them against its voiceprints as it does for in-cell speech, and stores nothing new beyond what in-cell naming stores. These embeddings are biometric data: they travel only inside the completion request, over the same authenticated route. When naming is off, the payload carries none, and Cloud shows labels without names.

### D6. Credential and proof

The speech worker signs in through the existing CLI OAuth flow with a new scope, `exomem.speech`. The gateway admits that scope only on these routes:
- `POST /speech/claim`, `/speech/heartbeat`, `/speech/complete` and `/speech/release`;
- `GET /speech/original?item=&revision=`, which streams the leased original with ranges and the size and SHA-256 headers.

The gateway never admits the speech scope on `/download`, `/mcp`, `/upload` or any other route.

For these routes the gateway signs a proof with a new purpose, `speech`. Its claims bind the route, the item and the revision, and carry the principal's ID and display name. A cell built before this change refuses the unknown purpose, so an old or rolled-back cell serves nothing to a speech token. The cell checks the lease against the principal in the proof before it serves an original.

Home lists each speech authorization with its last use, and the owner revokes it there. Rate limits and concurrency slots apply per principal.

### D7. Progress and resume

`stop_offset` keeps its existing unit, audio samples. Partial segments exist only on the speech worker that produced them. A lease that returns to the same principal resumes from its recorded offset. A lease taken by another principal starts from zero.

The speech worker deletes its copy of the original and any partial transcript when a job completes or is released. It also deletes them when the cell answers `lease_lost`, `original_changed` or `revoked`, and when the recording is deleted.

Heartbeat and completion answer one of `ok`, `lease_lost`, `stale_claim`, `original_changed`, `revoked` or `already_completed`.

### D8. Mode, fallback and status

cellctl renders `speech_mode` (`in_cell` or `remote`) and an optional `remote_speech_fallback_after` per cell, through the existing per-cell engine settings. The default stays `in_cell`.

The fallback clock starts when the speech item is created. When it runs out and the item has no live lease, the media worker runs the speech in the cell, without speaker labels, and marks the item `done`. Status says that the transcript came from the fallback and has no speaker labels. The owner can request a reprocess, and the reprocess goes to the speech worker.

Status reports:
- the speech items that wait, and since when;
- the last speech worker contact, with the authorization's display name;
- each fallback transcript.

### D9. The owner's speech worker command

`evermatter speech-worker setup` runs once on the owner's machine:
1. sign in with `exomem.speech`;
2. detect a CUDA GPU (CPU works but is slow, and setup says so);
3. prepare the existing engines and the diarizer sidecar venv;
4. install a systemd user unit on Linux and WSL.

`evermatter speech-worker run` loops through these steps: claim, download the original with verification into a private temporary directory, transcribe, diarise, embed when naming is on, complete, delete. It runs one job per GPU. `evermatter speech-worker status` prints the cell's view of the queue.

The command reuses the personal install's backends (`FasterWhisperBackend`, the diarizer sidecar, `voice_embed`), so its output is the personal engine's output.

### D10. Diarisation on CPU: personal installs yes, Cloud cells no

A personal install diarises on CPU through the diarizer sidecar. Two measurements bound its cost. The `diarization-cpu-sidecar` acceptance measured about 23 times the GPU time on one recording (18 minutes against 47 seconds). The ONNX diarisation evaluation of 2026-11-01 measured RTF 1.13 to 1.26 on CPU, against RTF 0.015 on a GPU for a different, longer call, about 80 times. Both peak near 2.9 GiB.

A Cloud cell cannot run it:
- The image has no PyTorch runtime, and a build gate keeps it that way, under the no-PyTorch contract in `add-cloud-multimodal-processing` design.md. pyannote 3.1 needs PyTorch.
- The speech child's anonymous memory budget is 1 GiB. The pod's 3 GiB limit also holds the serving process.
- A cell has 2 CPUs. A 1-hour meeting would hold the cell's media slot for over 2 hours of diarisation, after about 2.3 hours of transcription.
- The ONNX alternative evaluated on 2026-11-01 (sherpa-onnx) merged speakers too often to replace pyannote.

### D11. A platform GPU pool later

The cell authorizes by principal and scope, not by machine. A platform pool would be one more principal: a platform service credential that the gateway admits for cells whose owners opted in, under the same routes, lease rules and proof. The contract stays the same.

The pool itself needs a dispatcher. Polling every opted-in cell costs a request per cell per poll, so a pool needs a platform-side index of cells with pending speech items. It also needs a privacy copy change and an operator decision. This change builds none of it.

### D12. Scale

Each claim runs one indexed query over the speech items, by state and expiry, bounded to one row. An open claim holds no database connection, and each principal may hold one at a time. A completion does bounded validation, one render and one ledger write. The existing parent path publishes in the background. Per-principal lease caps and rate limits keep one owner's speech workers from starving the cell. An idle speech worker sends about two requests a minute.

The repository records no load target for Cloud cells, and this change reports that as debt. It sizes for one cell with 10,000 pending speech items and four speech workers.

### D13. Transcription first, speaker labels optional

Transcription is the product's first job. Speaker labels are optional per cell and per upload, through the existing per-upload label choice. A speech row whose job asks for no labels skips diarisation and embeddings, and completes with segments only. The speech worker claims rows in ledger order. Each row runs diarisation only when its job asks for labels and its track holds more than one voice (D14).

### D14. Multitrack recordings

A recording can carry its voices on separate tracks. Speech handles three sources of tracks:
1. A container with several audio streams. Each stream is a track.
2. One stream whose channels are independent microphones, such as a wireless kit recording each transmitter to its own channel. Channels are treated as tracks only when the upload, the owner or the agent says so, or the device's own metadata says so. Otherwise a multi-channel stream is one mixed track, as today. No measurement of the audio decides this.
3. Several files from one session, such as two transmitters that each record their own file. An agent or the owner groups them as one session with a governed operation that names each file's track. When the files' device metadata carries a shared start time or timecode, the operation uses it to align them. Otherwise they start together.

Each track is transcribed on its own. The transcript merges the tracks' segments by time and labels each turn with its track's speaker name. That name comes from the device or track metadata, or from the owner or agent. A track that holds several voices, such as one room microphone at a wedding, is also diarised within the track when the job asks for labels, and its turns read "<track name>: Speaker A". A track with one voice is never diarised.

Multitrack applies wherever speech runs: in a personal install, in a cell, and on a speech worker. The payload of D4 carries segments and turns per track.

### D15. Naming speakers after the fact

An agent-facing operation names the speakers of one recording, for example "Speaker B is the bride". It takes the recording and a mapping from current labels to names, and rewrites that recording's transcript labels through the governed writer. It is the agent's way to help Evermatter understand who spoke, when the person tells the agent.

When the recording's per-label embedding is available, the operation can also keep the voice: it enrols or updates a voice profile with that embedding, so later recordings are named automatically. That uses the existing voice-profile store and its rules (`speaker-diarization`). On Cloud, the cell keeps a recording's per-label embeddings only while the owner has naming on, and only to support this operation.

### D16. A speech worker on a customer's own machine

The speech worker runs on Windows, macOS and Linux, with or without a GPU. Without a GPU it transcribes on CPU and skips diarisation unless the owner turns it on. Setup suits a non-technical person:
1. Home offers "Use this computer for transcription" with one command per system to copy, or a downloadable installer.
2. The command installs the worker and opens a browser to approve the device with a sign-in code. Nobody pastes a token.
3. The worker installs as a background service for the signed-in user and starts at login.

Linux and WSL ship first, then macOS and Windows native, in that order.

### D17. Yield the GPU to the owner

The owner's GPU machine is often also the machine they play games or edit video on. The speech worker yields:
- It pauses before claiming, and between chunks of a job, while another process uses the GPU above a configured share, or while a full-screen application has focus on systems that report it.
- It resumes when the machine is idle again.
- The owner can set hours when the worker runs, and pause it from its status command or the tray.
- While paused it keeps its lease for up to the lease's length. After that it releases the item, so another speech worker or the fallback can take it.

The worker runs at the lowest CPU and I/O priority the system offers.

## Risks / Trade-offs

- **The speech worker sees plaintext originals.** It runs on the owner's own machine under the owner's credential, the same trust as their local vault. A platform pool changes this and needs its own decision.
- **Embeddings are biometric.** They are sent only when the owner turned naming on, and the cell stores no more than in-cell naming does.
- **A speech worker that disappears holds an item for one lease.** The delay is ten minutes, and the fallback bounds the worst case.
- **Engine drift.** The sidecar records the engine identity from the payload, so a reader sees which model produced the text.
- **Two surfaces to keep in step.** The gateway's scope admission and the cell's lease check must agree. The rehearsal scenario exercises both.

## Migration Plan

1. Ship the cell, gateway, Substrate and CLI pieces with every cell in `in_cell` mode.
2. Archive `add-cloud-multimodal-processing` first. This change modifies its requirement "A Cloud cell extracts media inside the cell".
3. Rehearse remote speech in the Cloud rehearsal with a CPU-sized model.
4. Under an operator-approved live change, set the owner's cell to `remote` with a fallback. Run the speech worker on the owner's GPU machine.
5. Undo: set the cell back to `in_cell`. The cell ends every live lease at once, so the next heartbeat or completion answers `lease_lost`, and the speech worker deletes its copies. Pending and formerly leased `speech` rows then run in the media worker. Published transcripts stay.

## Open Questions

1. The claim's wait bound, after measuring the gateway's idle limit on the claim path (task 2.1).
2. Which recorder metadata fields identify a transmitter and a shared start time, per device family (task 5.2 records them).
