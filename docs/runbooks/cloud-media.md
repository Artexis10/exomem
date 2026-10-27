<!-- authority:non-specification -->

# Cloud media: documents, OCR and speech

**Status:** the Cloud image ships documents, OCR and speech behind the existing engine switches.
It also carries the aligned multilingual image query artifact.
The document and OCR measurements below used a local container with 2 CPUs and 3 GiB.
Speech budgets remain provisional until integrated qualification and cell acceptance.
This runbook records no completed owner-sized speech backlog or two-cell sharing acceptance.

Media extraction makes uploaded files searchable. The Cloud image carries three engines:

- `documents` reads PDF, Word, Excel, PowerPoint, HTML, EPUB, OpenDocument (text, spreadsheet, presentation) and RTF files.
- `ocr` reads the text in images, and in PDF pages that carry no text layer.
- `speech` transcribes audio and video through ONNX Runtime Whisper `large-v3-turbo`, using the published int8 weights.

Plain text, email and calendar files need no engine and are always read.
The Cloud image does not ship the CLIP image encoder.

HEIC images use the maintained Pillow decoder shared by OCR and personal image embeddings.
The Cloud image gate includes a real HEIC sample with known text and runs without networking.
The decoder stays inside the existing media worker and its resource limits.
The measurements below predate HEIC support; they do not qualify an owner-sized HEIC backlog.

## Speech and image query artifacts

With `speech` enabled, ingestion queues transcription through the existing serialized media worker.
The worker publishes a transcript sidecar that search can read.
No agent, per-file tool call or manual transcription step is required.
Cloud sets `EXOMEM_ASR_BACKEND=onnx`; inference uses CPU execution providers without PyTorch or CUDA.
The backend detects the language automatically and decodes audio in bounded chunks through PyAV.
The `media-cpu` extra pins `onnx-asr[cpu]==0.12.0` and uses `av>=11,<19`.

Whisper `large-v3-turbo` declares 100 language tokens, including Cantonese.
That inventory is declared coverage, not a measured quality result across 100 languages.
English, Japanese and Estonian supplied the current diagnostic verification set.
The 30-case comparison preserved the upstream int8 outputs after conversion to external weight files.
It establishes file-layout agreement, not fp32-to-int8 agreement.
Further candidate selection uses a fresh held-out run and does not block the selected default.

Image queries use `sentence-transformers/clip-ViT-B-32-multilingual-v1` at fp32.
Its complete ONNX graph includes the transformer, masked mean and learned 512-dimensional projection.
The runtime normalizes the result and searches the existing OpenAI ViT-B/32 image space.
Image vectors come from `sentence-transformers/clip-ViT-B-32`, the model that wrote the stored vectors.
Cloud runs it as a pinned fp32 ONNX build, with the vision tower and its projection in one graph, on CPU.
The build reproduces the PyTorch vectors at cosine 0.9999 or above, so stored vectors stay valid and nothing is re-encoded.
The image vector sidecar records the vector space it holds and refuses vectors from another space.
Image tags need the PyTorch text tower, so they stay unavailable in the Cloud image.
Cloud keeps image search disabled with `EXOMEM_DISABLE_CLIP` until its separate acceptance passes; a cell enables it by unsetting that variable.

The [media model manifest](../../src/exomem/media_models.json) records source revisions, recipe versions and artifact digests.
The Cloud build fetches those pinned release artifacts before its offline load gates.
Runtime loads file-backed weights with prepacking disabled and network access off.
The speech decoder ships with its constants already folded, so a load folds nothing large into private memory and concurrent jobs share the folded tables.
Rebuilding that artifact needs `onnxruntime>=1.31` in the build environment; the runtime pin does not change.
New media artifacts never build on a serving host when published bytes are missing or invalid.
The existing `EXOMEM_MODEL_ARTIFACT_URL` selects the artifact publication or mirror.
The existing `EXOMEM_MODEL_ARTIFACT_DIR` selects its local cache root.

## Check disk space before backfill

Enabling an engine can process the existing media backlog. Memory brakes do not reserve disk space.
A full volume can stop catalogue publication and leave the cell unready, even when the media child stays within its memory budget.

The cell stores `/data` on its PVC and `/tmp` on a separate `emptyDir`. A free PVC does not prove scratch capacity.

1. Resolve the vault, state, scratch and writable cache paths from runtime configuration. Expect each path's owning filesystem.
2. Record each filesystem with `df -B1` and its resolved paths. Expect dated capacity and available-byte readings.
3. Record the media backlog from resource status. Expect separate pending, blocked and failed counts.
4. Record retained restore copies and current index sizes. Expect both in their filesystem's used-space baseline.
5. Compare each filesystem's free bytes with its measured backfill, scratch, cache and catalogue replacement needs. Expect sourced estimates.
6. If an estimate is unknown, record that gap in isolated qualification. Expect no estimate from upload size alone.
7. Record disk usage during qualification and after backfill. Expect separate temporary peak use and retained growth.

Model downloads and caches also use space when the selected engine needs them. Include those bytes in its qualification.
Use the existing rollout procedure to select one cell first. Reuse its storage evidence only while its engine, backlog and volume conditions match.
Removing a retained restore copy is a separate operator action under the restore runbook. A preflight does not authorize deletion or a volume change.

## Switch an engine on for a cell

Every engine is off in a Cloud cell unless the platform values switch it on, for one cell or for the fleet. A switched-off engine queues no job: the file stays searchable by its name and metadata, and its sidecar stays pending. Runtime status (`exomem status --resources`) and `exomem doctor` show each engine as `enabled`, `disabled` or `unavailable`. A tenant sees no message about a disabled engine.

cellctl renders `EXOMEM_MEDIA_ENGINES` into the cells that `cellctl.mediaEngineCellIds` names, and into every cell for an engine in `cellctl.mediaEngineFleetDefault`. A change restarts only the cells whose engine set changed.

1. Choose a time outside 02:00-05:00 UTC. Expect no overlap with the nightly backup window.
2. Set the chosen engines under `cellctl.mediaEngineCellIds`, including `speech` for transcripts. Expect the intended per-cell selection.
3. Deploy the platform chart through its normal release path. Expect only changed cells to restart.
4. Wait for the selected cell's pod to restart. Expect its new engine selection.
5. Run `exomem status --resources --json` in the cell. Expect the chosen engines under `media_engines` as `enabled`.
6. Check `media_brakes`. Expect `"state": "on"`.
7. Add public fixtures for each chosen engine. Expect the worker to publish searchable text automatically.
8. For speech, search a known transcript phrase. Expect matching audio and video sidecars.
9. Check `media.memory_blocked_count` and `media.over_budget_count` after the first day. Expect counts within the recorded acceptance limits.

To switch an engine off, remove the cell ID from its list and deploy. At the next start the media supervisor drops each job that is blocked because that engine could not load. It first shows the job's sidecar as pending again, so no old install instruction stays. A queued job of a switched-off engine runs no extraction when its turn comes, and its sidecar stays pending. Switching the engine on again queues these files.

Switch-off and image rollback preserve completed transcript sidecars and stored image vectors.
A new model version alone does not rewrite completed transcripts.
Use the existing platform release and rollback path for the engine selection and image.

An engine in the fleet default has no per-cell opt-out. To switch it off in one cell, remove it from `mediaEngineFleetDefault` and name it in `mediaEngineCellIds` for every other cell, in the same deploy. Those cells keep their engine set and restart nothing.

A job that waited on a missing engine returns to the queue at the next start, once the engine is switched on and its tools are present.
For speech, restore the pinned artifact and runtime through the image release before that restart.
Recovery uses the same queue and needs no per-file agent call.

With `documents` on and `ocr` off, a PDF that has no text layer at all waits for OCR instead of completing with no text. Runtime status shows it as pending and counts it in `media.engine_waiting_count`. When OCR is switched on, the next start returns it to the queue. A PDF with some text pages completes with that text.

## Make an engine the fleet default

A fleet default runs an engine in every cell, including a cell created later, so a new tenant needs no per-cell edit. Make an engine the default only after it passed its acceptance and runs in every existing cell through `mediaEngineCellIds`. The default then changes no existing cell.

Warning: a cell that `mediaEngineCellIds` does not name restarts at the deploy and starts the engine's backfill. Several such cells start backfill together. cellctl does not check this, so check every cell first.

1. Record the live cells: `psql "$EXOMEM_CELLCTL_DSN" -At -c "SELECT cell_id FROM exomem_cloud_cells WHERE desired_state <> 'deleted'" | LC_ALL=C sort > /tmp/cells-before.txt`.
2. Name the cells that the per-cell list misses, once for each engine you will add: `for ENGINE in documents ocr; do LC_ALL=C comm -23 /tmp/cells-before.txt <(helm -n exomem-platform get values exomem-platform -o json | jq -r --arg e "$ENGINE" '.cellctl.mediaEngineCellIds[$e][]?' | LC_ALL=C sort) | sed "s/^/$ENGINE: /"; done`. Expect no output.
3. If a line appears, add that cell to the engine's per-cell list first, one cell at a time, as in "Switch an engine on for a cell". Then repeat steps 1 and 2.
4. Record each cell pod's start time: `kubectl get pods --all-namespaces --no-headers -o custom-columns=NS:.metadata.namespace,POD:.metadata.name,START:.status.startTime | grep '^exo-cell-' > /tmp/pods-before.txt`.
5. Choose a time outside the nightly backup window, 02:00-05:00 UTC.
6. Add the engines to the platform values, for example `mediaEngineFleetDefault: ["documents", "ocr"]`. Keep the per-cell lists. The chart refuses a name that is not an engine.
7. Deploy the platform chart through its normal release path.
8. Repeat the command of step 1 into `/tmp/cells-after.txt`. Run `LC_ALL=C comm -13 /tmp/cells-before.txt /tmp/cells-after.txt`. Each line is a cell created since step 1.
9. Repeat the command of step 4 into `/tmp/pods-after.txt`, then run `diff /tmp/pods-before.txt /tmp/pods-after.txt`. Expect a changed line only for a cell from step 8: no other cell restarted.
10. Run `exomem status --resources --json` in a cell from step 8, or in a cell you create, and check that `media_engines` shows each engine as `enabled`.

If a cell restarted at step 9, step 2 missed it. That cell now runs the engine and backfills its media. Wait for the backfill, then check `media.memory_blocked_count` and `media.over_budget_count` as for any switch-on.

Leave the engine's entries in `mediaEngineCellIds` after the default holds it: a later switch-off in one cell needs them.

## Memory brakes

The brakes stop media from taking the serving process down. They run only in Cloud cells. Each brake reads the cell's own cgroup v2 files at run time. No brake reads `memory.current`: it counts reclaimable page cache and model weights charged to whichever cell read them first.

- **Admission.** The supervisor starts a media child only when pressure is low and the next job fits: the cell's anonymous memory (`memory.stat` anon) plus the budget of the engines the job loads must stay under the admission ceiling. The ceiling is the lower of `memory.high` and the admission fraction of `memory.max`. Otherwise it checks again after 15 seconds. The child checks once more before it claims the job.
- **One job per child.** Each media child runs one job and exits. Every job, and every counted hard-limit failure, starts in a fresh child with no growth from an earlier job. A child start costs about 1-2 seconds.
- **Hard limit.** The child runs its job under a data-segment limit (`RLIMIT_DATA`): the largest VmData budget among the engines it loads, plus the margin. Tesseract and the other native tools inherit it. An allocation past the limit fails inside the child, not in the serving process.
- **Pressure stop.** While a child runs, the supervisor reads the `memory.pressure` 10-second averages. Above the threshold, it kills the child and the tools it started, and returns the job to the queue. Where pressure is unreadable, the supervisor stops the child when anonymous memory reaches the admission ceiling.
- **Watchdog.** The supervisor kills a child that runs longer than the job timeout. A watchdog stop spends the file's time budget, not memory, so it counts apart from memory stops. After the stop limit of watchdog stops, each in a fresh child, the file is over budget. A file that hangs or outruns the timeout holds the queue for at most the stop limit times the job timeout, 45 minutes with the defaults. The watchdog cannot tell a long job that makes progress from a hang; a progress signal from the child would let such a job finish.

The brakes fail closed. When the cgroup does not read as a Cloud cell's (cgroup v1, a missing file, no `anon` entry, an unreadable `memory.max`, or no memory limit), no media child starts. Runtime status shows `media_brakes` as `unavailable` with the reason, and `exomem doctor` warns "media brakes unavailable: <reason>". Media waits until the cgroup reads correctly again. Personal installs have no brakes, and runtime status shows them as `off`.

Each value is deployment configuration. Set it in the cell's environment.

| Variable | Default | Reason for the default |
|---|---|---|
| `EXOMEM_MEDIA_BUDGETS` | Fallback: 640 MiB anon, 1024 MiB VmData. Cloud overrides: speech 1024 MiB anon, 4096 MiB VmData; `clip` 256 MiB anon, 1536 MiB VmData. | Documents and OCR retain the earlier small-sample margin. Speech uses a provisional budget for its file-backed model; cell qualification remains open. The `clip` entry covers the image tower measured alone at 98 MiB anon and a 1403.5 MiB VmData peak. |
| `EXOMEM_MEDIA_VMDATA_MARGIN_MIB` | 128 | Covers the allocator's own arenas above the engine's budget. |
| `EXOMEM_MEDIA_ADMISSION_FRACTION` | 0.80 | The service-v1 profile's 80% cgroup peak gate. |
| `EXOMEM_MEDIA_PRESSURE_AVG10` | 10.0 | Sustained reclaim, well above an idle cell and below the stalls that come before an OOM kill. |
| `EXOMEM_MEDIA_STOP_LIMIT` | 3 | Consecutive memory stops before a job leaves the stop cycle. Watchdog stops count apart, against the same limit. |
| `EXOMEM_MEDIA_JOB_TIMEOUT_SECONDS` | 900 | Far above the samples' sub-second times, with room for a long scanned PDF. A file costs at most the stop limit times this before it is over budget. |

`EXOMEM_MEDIA_BUDGETS` maps each engine to its budget, for example `{"speech": {"anon_mib": 1024, "vmdata_mib": 4096}, "default": {"anon_mib": 640, "vmdata_mib": 1024}}`.
The `default` entry covers kinds without an engine.
The `clip` entry covers the CLIP image tower, which a child loads when it embeds an image or video and image search is on.
A child keeps each engine it loads until it exits, so its anonymous-memory budget is the sum of their entries.
Its VmData budget is the largest of their entries, not the sum: weight mappings dominate VmData.
A measured process that holds the image tower peaks at 3.89 GiB (3983 MiB) of VmData, inside the speech entry's 4096 MiB.
The sum would grant 5632 MiB to a child that loads both.
An anonymous-memory budget above the cell's `memory.max`, or a nonpositive budget, keeps the default budget.
VmData includes clean file mappings and can exceed the cell's physical limit.
The 4096 MiB speech VmData budget therefore does not allocate 4 GiB of physical memory in a 3 GiB cell.
Unknown engine names and malformed values retain the existing warning and fallback behavior.

The speech override is a starting value, not completed capacity evidence.
Qualification must measure anonymous memory, VmData, memory pressure and search behavior with speech active.
Two cells on one node must also demonstrate shared weight mappings through Pss.
The rollout still needs its owner-sized backlog and existing service capacity checks.

Do not set a `documents` VmData budget below 384 MiB. MarkItDown loads an ONNX Runtime model to detect file types, and that session hangs, without failing, when the limit blocks its thread stacks. Probes hung at 203 and 265 MiB. The watchdog stops such a hang, and the file ends over budget without extracting.

## Document and OCR measurements

Measured on 2026-10-10 with the Cloud image, the media child's `OPENBLAS_NUM_THREADS=1`, and the samples in `tests/fixtures/media-samples/`.

The smallest VmData limit under which one small sample still extracts:

| Engine path | Smallest limit |
|---|---|
| OCR (English, Estonian) | 266-267 MiB |
| OCR (Japanese, horizontal and vertical) | 204 MiB |
| PDF text layer (PyMuPDF) | 236 MiB |
| Word, Excel, PowerPoint, HTML, EPUB (MarkItDown) | 327-329 MiB |
| OpenDocument, RTF | 111-112 MiB |

A local cell container with 2 CPUs and 3 GiB ran with the default budgets in this table:

- With `documents` and `ocr` on, the vault held one file of each format and a 10,000 x 10,000 pixel image. Search found all 17 sample files at rank 1 by a phrase inside them.
- The 100 MP image ended over budget after 3 hard-limit failures: Tesseract could not allocate its page under the 1152 MiB limit.
- During that run, a query every 2 seconds got 70 answers and no failures. The median was 0.219 s and the slowest was 2.731 s, while the first index build ran.
- The cell's peak anonymous memory was 1855 MiB in that run, against 690 MiB for the same samples with every engine off. The cgroup recorded no OOM event.
- With simulated memory pressure from other work, an OCR job became memory-blocked after 3 pressure stops, 48.7 s into the run. It showed as pending, with no error. The pressure then ended. The job returned to the queue by itself and had completed by the next status sample, 76.1 s into the run.
- A 300 MB single-paragraph HTML file (314,560,545 bytes) ended over budget after 3 hard-limit failures, 40.0 s into the run, and never as a failed file.

## Job states

Memory stops never count as attempts. A job that the brakes stop moves through these states:

- **Pending.** A pressure stop, a watchdog stop or a single hard-limit failure returns the job to the queue.
- **Memory-blocked.** The job reached the stop limit of memory stops and at least one was a pressure stop, so other work caused it. The tenant sees the job as pending. The supervisor returns it to the queue by itself: at its start, and then on a timer. The timer starts at 60 seconds and doubles to 30 minutes while pressure stays high. It goes back to 60 seconds as soon as pressure clears or a job completes.
- **Over budget.** The job reached the stop limit of memory stops and every one was a hard-limit failure, so the file itself needs more memory than the engine's budget. Or the job reached the stop limit of watchdog stops, so the file needs more time than the job timeout. The tenant sees "the file exceeds this deployment's processing budget" with no next action, and the sidecar offers no retry. A memory verdict returns to the queue only after the cell's limit or the engine's budget changes. A time verdict returns only after the job timeout grows past the one it exceeded. A manual retry returns neither.

The ledger stores each reason in its `blocked_reason` column. Runtime status reports `media.memory_blocked_count`, `media.over_budget_count` and `media.engine_waiting_count`. `media.counts` counts each job as its own row shows it, so memory-blocked and waiting jobs count as pending. `exomem doctor` reports memory-blocked and over-budget work to the operator, never to the tenant.

## Tenant tools

Cloud cells serve `process_media`, the same tool as the personal service. A tenant's assistant can call it with these operations:

- `status` reads the media job counts and each job's state.
- `process` queues one file, or a list of files, now instead of waiting for automatic ingestion. Without a path it does bounded work over all media.
- `retry` queues a failed file again after the cause is fixed.

Every operation shows memory-blocked and engine-waiting work only as pending. The reasons, the worker process and the store path stay in `exomem doctor`.

All three use this cell's queue, memory brakes and engine selection. A file whose engine is off waits, and no operation runs it on an engine the cell does not select. A retry never returns over-budget work (see Job states). A new OCR pack or OCR fix leaves completed text unchanged, and no operation re-extracts it. A cell serves the tool only from the first image release that includes it, so roll that image before you tell a tenant about it.

## Concurrent sidecar updates

A backlink added during extraction no longer invalidates computed output.
The worker uses the existing evidence version, which binds the body and typed metadata except `ingested_into`.
It renders against the latest validated sidecar and preserves that sidecar's backlinks.
A full-byte publication guard still refuses a concurrent edit.
Body changes, meaningful metadata changes and binary replacement keep their existing invalidation behavior.

The result ledger retains separate input and target evidence versions for crash recovery.
Old rows without those versions keep exact-byte protection.
This changes neither completed OCR text nor the existing explicit retry and resource rules.

## OCR languages

OCR first detects each image's script with Tesseract's OSD model. It then reads the image with the installed script models for that script and the installed language packs written in it. Each pack's script comes from the pack's own unicharset, so no code lists languages. A Japanese page reads with both a horizontal and a vertical pass, and OCR keeps the pass with the higher word confidence.

Reading each pack's script means reading every model file, 353 MB in the Cloud image. The image build writes the result to `EXOMEM_OCR_INVENTORY` (`/opt/exomem-ocr/inventory.json`), so a fresh media child reads one small file instead. A personal install keeps the same inventory in `~/.cache/exomem/ocr-inventory.json` and reads the models again when their names, sizes or dates change. Tesseract 4.x does not name its model directory; there OCR reads each image once in Tesseract's default language.

The Cloud image installs every Tesseract script model and the language packs that the `EXOMEM_OCR_LANGS` build argument names. The default is `eng+jpn+jpn_vert+est`. The image sets `EXOMEM_OCR_DEFAULT_LANGS` from the same argument; OCR uses it for an image with too little text to detect a script.

To add a language:

1. Find its Debian package name: `tesseract-ocr-<code>`, with `_` written as `-`.
2. Build the Cloud image with `--build-arg EXOMEM_OCR_LANGS=eng+jpn+jpn_vert+est+<code>`.
3. Check that the build's media sample gate passes.

Expect the new pack to read pages whose script its unicharset names.

A script that OSD names differently from every script model has no route, and its pages read with `EXOMEM_OCR_DEFAULT_LANGS`. Korean is one: OSD reports `Korean`, and the script model is `Hangul`.

## Build-time gate

The Cloud stage runs `scripts/check-media-samples.py` with the network off.
The `EXOMEM_MEDIA_SHIPPED_ENGINES` build argument defaults to `documents,ocr,speech` and names the engines the image claims.
The gate needs a public fixture for each media kind served by those engines, plus kinds that need no engine.
It extracts each fixture and checks the phrase in `expected.json`.
A missing fixture or phrase fails the build, as does an installed CUDA wheel (`nvidia-*`) or PyTorch.

Speech coverage includes a public WAV recording and an MKV video containing the same lossless audio.
[Fixture provenance](../../tests/MEDIA-SOURCES.md) records the source, licence and expected phrase lineage.
These exposed integration fixtures prove offline audio and video extraction, not held-out speech quality.
A separate offline load gate checks that the selected image query encoder returns one 512-dimensional vector.
That gate does not claim that the Cloud image encoder or image-search rollout has passed acceptance.

The stage installs the `media-cpu` extra at the versions in `uv.lock`.
The earlier document and OCR layers added 697 MB uncompressed: 460 MB of system packages and 237 MB of Python packages.
That measurement excludes the speech runtime and the speech and image query artifacts.
Record their image growth during qualification instead of reusing the earlier total.
`scripts/make-media-samples.py` regenerates the document and OCR samples; preserve the attributed speech fixtures separately.

## HEIC photos

The `media-cpu` and `embeddings` extras share the pinned `pillow-heif` decoder through `image-codecs`.
The decoder registers only when a HEIC image opens, with one decoding thread.
OCR still requires the selected OCR engine; image embeddings keep their existing switch.
Distributed packages retain upstream notices and corresponding-source links under `exomem/_licenses/pillow-heif-1.8.1/`.
Those software licence terms do not establish patent clearance.

## Notes for a new engine

- The media child sets `OPENBLAS_NUM_THREADS=1` under the brakes. Without it, numpy's OpenBLAS reserves data segment for one thread per host CPU (about 40 MiB each), so the child's VmData follows the node instead of the engine. On a 20-CPU host, the child's baseline VmData was 855 MiB instead of 97 MiB.
- A pressure or watchdog stop kills the media child's whole session and then reaps the tools that were reparented to the server. In a Cloud cell the server is PID 1, so a killed tool would otherwise stay a zombie.
- An engine that fails an allocation must raise a Python `MemoryError` at its boundary, or the child records an artifact failure. The existing engines show the ways to recognise one without reading a file name:
  - MarkItDown wraps every converter's exception; the extractor reads the wrapped exception types.
  - PyMuPDF raises `FzErrorSystem` with MuPDF's own allocator message, such as `malloc (N bytes) failed`.
  - Tesseract reports a failed allocation only on standard error, and can still exit 0 with partial text. OCR checks standard error for the allocator names. Tesseract echoes only its own temporary file names there.
