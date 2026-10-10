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
6. Add a PDF and an image with text to the cell's vault, then search for a phrase from each.
7. Check `media.memory_blocked_count` and `media.over_budget_count` in runtime status after the first day.

Expect the search to find both files within a few minutes.

To switch an engine off, remove the cell ID from its list and deploy. At the next start the media supervisor drops the queued jobs of that engine. Their sidecars stay pending, and switching the engine on again queues them.

A job that waited on a missing engine returns to the queue at the next start, once the engine is switched on and its tools are present.

## Memory brakes

The brakes stop media from taking the serving process down. They run only in Cloud cells. Each brake reads the cell's own cgroup v2 files at run time. No brake reads `memory.current`: it counts reclaimable page cache and model weights charged to whichever cell read them first.

- **Admission.** A media child takes a job only when the cell's anonymous memory (`memory.stat` anon) plus the engine's anonymous-memory budget fits under the admission ceiling. The ceiling is the lower of `memory.high` and the admission fraction of `memory.max`. Without room, the child exits and the supervisor tries again after 15 seconds.
- **Hard limit.** The child runs each job under a data-segment limit (`RLIMIT_DATA`): the engine's VmData budget plus the margin. Tesseract and the other native tools inherit it. An allocation past the limit fails inside the child, not in the serving process. The child exits after each such failure.
- **Pressure stop.** While a child runs, the supervisor reads the `memory.pressure` 10-second averages. Above the threshold, it kills the child and the tools it started, and returns the job to the queue. Where pressure is unreadable, the supervisor stops the child when anonymous memory reaches the admission ceiling. No new child starts while pressure is above the threshold.

Each value is deployment configuration. Set it in the cell's environment.

| Variable | Default | Reason for the default |
|---|---|---|
| `EXOMEM_MEDIA_BUDGETS` | 640 MiB anon, 1024 MiB VmData for every engine | VmData: about 3 times the largest measured need of a small sample (329 MiB). Anon: a child admitted at the ceiling that grows to its hard limit stays under a 3 GiB `memory.max` (0.80 × 3072 − 640 + 1024 + 128 = 2970 MiB). |
| `EXOMEM_MEDIA_VMDATA_MARGIN_MIB` | 128 | Covers the allocator's own arenas above the engine's budget. |
| `EXOMEM_MEDIA_ADMISSION_FRACTION` | 0.80 | The service-v1 profile's 80% cgroup peak gate. |
| `EXOMEM_MEDIA_PRESSURE_AVG10` | 10.0 | Sustained reclaim, well above an idle cell and below the stalls that come before an OOM kill. |
| `EXOMEM_MEDIA_MEMORY_STOP_LIMIT` | 3 | Consecutive memory stops before a job leaves the stop cycle. |

`EXOMEM_MEDIA_BUDGETS` is a JSON object from engine to budget, for example `{"ocr": {"anon_mib": 640, "vmdata_mib": 1024}, "default": {"anon_mib": 512, "vmdata_mib": 768}}`. The `default` entry covers the kinds without an engine. A malformed value falls back to its default and logs a warning that names it.

Do not set a `documents` VmData budget below 384 MiB. MarkItDown loads an ONNX Runtime model to detect file types, and that session hangs, without failing, when the limit blocks its thread stacks. Probes hung at 203 and 265 MiB.

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

A local cell container with 2 CPUs and 3 GiB held one file of each format and a 10,000 x 10,000 pixel image:

- With `documents` and `ocr` on, search found every file at rank 1 by a phrase inside it.
- With an OCR budget of 384 MiB VmData, the 100 MP image ended over budget after 3 hard-limit failures.
- A query every 2 seconds got 38 answers and no failures during the work. The median was 0.30 s and the slowest was 4.8 s, while the first index build ran.
- The cell's peak anonymous memory was 932 MiB, against 725 MiB for the same vault with every engine off. The cgroup recorded no OOM event.

## Job states

Memory stops never count as attempts. A job that the brakes stop moves through these states:

- **Pending.** A pressure stop or a single hard-limit failure returns the job to the queue.
- **Memory-blocked.** The job reached the stop limit and at least one stop was a pressure stop, so other work caused it. The tenant sees the job as pending. The supervisor returns it to the queue by itself: at its start, and then on a timer that starts at 60 seconds and doubles to 30 minutes, whenever pressure is below the threshold.
- **Over budget.** The job reached the stop limit and every stop was a hard-limit failure, so the file itself needs more memory than the engine's budget. The tenant sees "the file exceeds this deployment's processing budget" with no next action. The job returns to the queue only after the cell's limit or the engine's budget changes, at the next supervisor start. A manual retry does not return it.

Runtime status reports `media.memory_blocked_count` and `media.over_budget_count`. `exomem doctor` warns the operator about either, never the tenant.

## OCR languages

OCR first detects each image's script with Tesseract's OSD model. It then reads the image with the installed script models for that script and the installed language packs written in it. Each pack's script comes from the pack's own unicharset, so no code lists languages. A Japanese page reads with both a horizontal and a vertical pass, and OCR keeps the pass with the higher word confidence.

The Cloud image installs every Tesseract script model and the language packs that the `EXOMEM_OCR_LANGS` build argument names. The default is `eng+jpn+jpn_vert+est`. The image sets `EXOMEM_OCR_DEFAULT_LANGS` from the same argument; OCR uses it for an image with too little text to detect a script.

To add a language:

1. Find its Debian package name: `tesseract-ocr-<code>`, with `_` written as `-`.
2. Build the Cloud image with `--build-arg EXOMEM_OCR_LANGS=eng+jpn+jpn_vert+est+<code>`.
3. Check that the build's media sample gate passes.

Expect the new pack to read pages whose script its unicharset names.

A script that OSD names differently from every script model has no route, and its pages read with `EXOMEM_OCR_DEFAULT_LANGS`. Korean is one: OSD reports `Korean`, and the script model is `Hangul`.

## Build-time gate

The Cloud stage extracts every sample in `tests/fixtures/media-samples/` with the network off, and checks the expected phrase in `expected.json`. The gate also fails when a CUDA wheel (`nvidia-*`) or torch is installed. `scripts/make-media-samples.py` regenerates the samples.

The media layers add about 663 MB uncompressed to the Cloud image: 460 MB of system packages (353 MB of them Tesseract models) and 203 MB of Python packages. The compressed image grew from 532 MB to 815 MB.

## HEIC photos

The Cloud image does not decode HEIC. HEIC carries HEVC video frames. The HEVC patent pools (Access Advance, Via LA) grant no royalty-free licence for decoding in a commercial server-side service. The available Python decoders also bundle LGPL `libheif` and `libde265`, and `pillow-heif` adds the GPL `x265` encoder. Add HEIC only after a licence decision. Personal installs are unchanged.

## Notes for a new engine

- The media child sets `OPENBLAS_NUM_THREADS=1` under the brakes. Without it, numpy's OpenBLAS reserves data segment for one thread per host CPU (about 40 MiB each), so the child's VmData follows the node instead of the engine. On a 20-CPU host, the child's baseline VmData was 855 MiB instead of 97 MiB.
- A pressure stop kills the media child's whole session and then reaps the tools that were reparented to the server. In a Cloud cell the server is PID 1, so a killed tool would otherwise stay a zombie.
- A native library that fails an allocation must say so in a way the child can classify: a Python `MemoryError`, or one of the allocator names (`malloc`, `calloc`, `realloc`, `bad_alloc`) in its error. Leptonica reports a failed allocation on standard error and can still exit 0 with partial text, so OCR checks Tesseract's standard error.
