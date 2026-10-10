<!-- authority:non-specification -->

# Cloud media: documents and OCR

**Status:** written with `add-cloud-multimodal-processing` slice 1 (documents, OCR, memory brakes, switches). The measurements below come from a local cell container built from the Cloud image and run with 2 CPUs and 3 GiB. No live cell has run media yet.

Media extraction makes uploaded files searchable. The Cloud image carries two engines:

- `documents` reads PDF, Word, Excel, PowerPoint, HTML, EPUB, OpenDocument (text, spreadsheet, presentation) and RTF files.
- `ocr` reads the text in images, and in PDF pages that carry no text layer.

Plain text, email and calendar files need no engine and are always read. Image vectors (CLIP) and speech are not in the Cloud image.

## Switch an engine on for a cell

Every engine is off in a Cloud cell by default. A switched-off engine queues no job: the file stays searchable by its name and metadata, and its sidecar stays pending. Runtime status (`exomem status --resources`) and `exomem doctor` show each engine as `enabled`, `disabled` or `unavailable`. A tenant sees no message about a disabled engine.

cellctl renders `EXOMEM_MEDIA_ENGINES` into the cells that `cellctl.mediaEngineCellIds` names. A change restarts only the cells whose selection changed.

1. Choose a time outside the nightly backup window, 02:00-05:00 UTC.
2. Set the selection in the platform values: `mediaEngineCellIds: {documents: ["<cell id>"], ocr: ["<cell id>"]}`.
3. Deploy the platform chart through its normal release path.
4. Wait for the cell's pod to restart.
5. Run `exomem status --resources --json` in the cell and check that `media_engines` shows `documents` and `ocr` as `enabled`.
6. Check that `media_brakes` shows `"state": "on"`.
7. Add a PDF and an image with text to the cell's vault, then search for a phrase from each.
8. Check `media.memory_blocked_count` and `media.over_budget_count` in runtime status after the first day.

Expect the search to find both files within a few minutes.

To switch an engine off, remove the cell ID from its list and deploy. At the next start the media supervisor drops each job that is blocked because that engine could not load. It first shows the job's sidecar as pending again, so no old install instruction stays. A queued job of a switched-off engine runs no extraction when its turn comes, and its sidecar stays pending. Switching the engine on again queues these files.

A job that waited on a missing engine returns to the queue at the next start, once the engine is switched on and its tools are present.

With `documents` on and `ocr` off, a PDF that has no text layer at all waits for OCR instead of completing with no text. Runtime status shows it as pending and counts it in `media.engine_waiting_count`. When OCR is switched on, the next start returns it to the queue. A PDF with some text pages completes with that text.

## Memory brakes

The brakes stop media from taking the serving process down. They run only in Cloud cells. Each brake reads the cell's own cgroup v2 files at run time. No brake reads `memory.current`: it counts reclaimable page cache and model weights charged to whichever cell read them first.

- **Admission.** The supervisor starts a media child only when pressure is low and the next job fits: the cell's anonymous memory (`memory.stat` anon) plus the job's engine budget must stay under the admission ceiling. The ceiling is the lower of `memory.high` and the admission fraction of `memory.max`. Otherwise it checks again after 15 seconds. The child checks once more before it claims the job.
- **One job per child.** Each media child runs one job and exits. Every job, and every counted hard-limit failure, starts in a fresh child with no growth from an earlier job. A child start costs about 1-2 seconds.
- **Hard limit.** The child runs its job under a data-segment limit (`RLIMIT_DATA`): the engine's VmData budget plus the margin. Tesseract and the other native tools inherit it. An allocation past the limit fails inside the child, not in the serving process.
- **Pressure stop.** While a child runs, the supervisor reads the `memory.pressure` 10-second averages. Above the threshold, it kills the child and the tools it started, and returns the job to the queue. Where pressure is unreadable, the supervisor stops the child when anonymous memory reaches the admission ceiling.
- **Watchdog.** The supervisor kills a child that runs longer than the job timeout. A watchdog stop spends the file's time budget, not memory, so it counts apart from memory stops. After the stop limit of watchdog stops, each in a fresh child, the file is over budget. A file that hangs or outruns the timeout holds the queue for at most the stop limit times the job timeout, 45 minutes with the defaults. The watchdog cannot tell a long job that makes progress from a hang; a progress signal from the child would let such a job finish.

The brakes fail closed. When the cgroup does not read as a Cloud cell's (cgroup v1, a missing file, no `anon` entry, an unreadable `memory.max`, or no memory limit), no media child starts. Runtime status shows `media_brakes` as `unavailable` with the reason, and `exomem doctor` warns "media brakes unavailable: <reason>". Media waits until the cgroup reads correctly again. Personal installs have no brakes, and runtime status shows them as `off`.

Each value is deployment configuration. Set it in the cell's environment.

| Variable | Default | Reason for the default |
|---|---|---|
| `EXOMEM_MEDIA_BUDGETS` | 640 MiB anon, 1024 MiB VmData for every engine | VmData: about 3 times the largest measured need of a small sample (329 MiB). Anon: a child admitted at the ceiling that grows to its hard limit stays under a 3 GiB `memory.max` (0.80 × 3072 − 640 + 1024 + 128 = 2970 MiB). |
| `EXOMEM_MEDIA_VMDATA_MARGIN_MIB` | 128 | Covers the allocator's own arenas above the engine's budget. |
| `EXOMEM_MEDIA_ADMISSION_FRACTION` | 0.80 | The service-v1 profile's 80% cgroup peak gate. |
| `EXOMEM_MEDIA_PRESSURE_AVG10` | 10.0 | Sustained reclaim, well above an idle cell and below the stalls that come before an OOM kill. |
| `EXOMEM_MEDIA_MEMORY_STOP_LIMIT` | 3 | Consecutive memory stops before a job leaves the stop cycle. Watchdog stops count apart, against the same limit. |
| `EXOMEM_MEDIA_JOB_TIMEOUT_SECONDS` | 900 | Far above the samples' sub-second times, with room for a long scanned PDF. A file costs at most the stop limit times this before it is over budget. |

`EXOMEM_MEDIA_BUDGETS` is a JSON object from engine to budget, for example `{"ocr": {"anon_mib": 640, "vmdata_mib": 1024}, "default": {"anon_mib": 512, "vmdata_mib": 768}}`. The `default` entry covers the kinds without an engine. An entry at or below zero, or above the cell's `memory.max`, keeps the default budget. An unknown engine name is ignored. Each of these cases, and any malformed value, logs one warning that names it.

Do not set a `documents` VmData budget below 384 MiB. MarkItDown loads an ONNX Runtime model to detect file types, and that session hangs, without failing, when the limit blocks its thread stacks. Probes hung at 203 and 265 MiB. The watchdog stops such a hang, and the file ends over budget without extracting.

## Measurements

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

The Cloud stage runs `scripts/check-media-samples.py` with the network off. The `EXOMEM_MEDIA_SHIPPED_ENGINES` build argument (default `documents,ocr`) names the engines the image claims. For every media kind in the registry whose engine the image claims, or which needs no engine, the gate needs a sample in `tests/fixtures/media-samples/`. It extracts each sample and checks the phrase in `expected.json`. A kind without a sample fails the build. The gate also fails when a CUDA wheel (`nvidia-*`) or torch is installed. `scripts/make-media-samples.py` regenerates the samples.

The stage installs the `media-cpu` extra at the versions in `uv.lock`. The media layers add 697 MB uncompressed to the Cloud image: 460 MB of system packages (353 MB of them Tesseract models) and 237 MB of Python packages.

## HEIC photos

The Cloud image does not decode HEIC. HEIC carries HEVC video frames. The HEVC patent pools (Access Advance, Via LA) grant no royalty-free licence for decoding in a commercial server-side service. The available Python decoders also bundle LGPL `libheif` and `libde265`, and `pillow-heif` adds the GPL `x265` encoder. Add HEIC only after a licence decision. Personal installs are unchanged.

## Notes for a new engine

- The media child sets `OPENBLAS_NUM_THREADS=1` under the brakes. Without it, numpy's OpenBLAS reserves data segment for one thread per host CPU (about 40 MiB each), so the child's VmData follows the node instead of the engine. On a 20-CPU host, the child's baseline VmData was 855 MiB instead of 97 MiB.
- A pressure or watchdog stop kills the media child's whole session and then reaps the tools that were reparented to the server. In a Cloud cell the server is PID 1, so a killed tool would otherwise stay a zombie.
- An engine that fails an allocation must raise a Python `MemoryError` at its boundary, or the child records an artifact failure. The existing engines show the ways to recognise one without reading a file name:
  - MarkItDown wraps every converter's exception; the extractor reads the wrapped exception types.
  - PyMuPDF raises `FzErrorSystem` with MuPDF's own allocator message, such as `malloc (N bytes) failed`.
  - Tesseract reports a failed allocation only on standard error, and can still exit 0 with partial text. OCR checks standard error for the allocator names. Tesseract echoes only its own temporary file names there.
