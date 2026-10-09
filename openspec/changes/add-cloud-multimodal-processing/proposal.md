## Why

Exomem Cloud cells search text only. The Cloud image is built without media engines, so images, documents beyond plain text, audio and video are stored but cannot be searched by what they contain:

- no Tesseract and no Pillow, so images get no OCR;
- no CLIP, so images cannot be found by what they show;
- no speech engine, so audio and video get no transcript.

The media worker still starts in every cell and fails each job:

- jobs stay blocked forever, with a message that tells the tenant to install a dependency;
- every query reports `degraded: ["clip"]`.

A personal Exomem does all of this today. Cloud tenants should get the same memory, and the machinery must stay invisible to them.

Some gaps exist on every install:

- OCR runs in English only, so Japanese text in an image is never read;
- image search understands only English queries;
- EPUB, OpenDocument and RTF files are not read;
- iPhone HEIC photos fail.

Invited friends write in Japanese and other languages, so these gaps are part of the same work.

## What Changes

- **Media runs inside each cell.** Media runs in the cell's existing serialized, disposable media worker, inside the cell container. Plaintext never leaves the cell, and no third-party service sees it.
  - Engines and their weights are pre-baked into the image and load offline.
  - Where the runtime allows it, weights are file-backed, so one node keeps one copy for all cells.
- **Memory brakes keep search safe.** Media must never take the serving process down. The worker:
  - claims a job only while the cell's measured memory leaves room for it;
  - runs under a hard memory limit;
  - stops when memory pressure rises.

  A stopped job returns to pending. It is never recorded as an artifact failure.
- **Documents.** Cloud reads every type a personal install reads: PDF, Word, Excel, PowerPoint, HTML, plain text, email and calendar. Every install gains EPUB, OpenDocument (text, spreadsheet, presentation) and RTF.
- **Images.**
  - OCR detects the script first, then reads with that script's model and the installed language packs for it. The installed set is deployment configuration. The first language packs are English, Japanese (horizontal and vertical) and Estonian.
  - Every install encodes images with one pinned image model, chosen by published benchmarks (design D5), so stored vectors are interchangeable across installs. The candidates are:
    - an ONNX build of today's CLIP image model, with a multilingual text encoder aligned to its image space, which answers queries in any supported language and leaves stored image vectors unchanged;
    - a multilingual image–text model, which re-encodes stored image vectors once.
  - HEIC is decoded, subject to a licence check.
- **Speech.** Audio and video get transcripts from the engine and model that the bake-off in design.md selects. The first required language set is Japanese, English and Estonian, and it is deployment configuration.
- **Off until proven.** Each engine has a deployment switch. On Cloud it stays off until its acceptance passes on a real cell.
  - An engine that is off or not shipped is reported as disabled on operator surfaces.
  - It is never reported as degraded on each query, and never shown to a tenant as an install instruction.
- **Backfill.** Media already in a cell is processed automatically once its engine is on, one cell at a time.
- **Blocked jobs recover.** A job blocked because its engine was missing is requeued when the engine appears.

## Capabilities

### New Capabilities

- `cloud-multimodal-processing` covers how a Cloud cell processes media:
  - placement inside the cell;
  - pre-baked offline engines and shared weights;
  - memory admission, the hard limit and the pressure stop;
  - deployment switches and their acceptance;
  - backfill, and the honest disabled state.

### Modified Capabilities

- `multimodal-job-runtime`:
  - a job blocked on a missing engine is requeued when the engine becomes available;
  - a worker stopped for memory pressure returns its job to pending.
- `automatic-media-processing`:
  - EPUB, OpenDocument, RTF and HEIC are extracted on every install;
  - media whose engine is disabled keeps its pending sidecar without a job, and is queued once the engine is enabled;
  - on a Cloud cell, a blocked job's next action is never an install instruction.
- `instant-start`: an image lane that is disabled, or that has no stored image vectors, is not a warming or degraded component.
- `multilingual-recall`:
  - OCR reads with the installed script models and language packs, after script detection;
  - image search accepts queries in every language the multilingual text encoder supports;
  - the image vector sidecar records its vector space, and image spaces never mix.

## Impact

- **Image:**
  - the `cloud` stage gains Tesseract with the configured packs, Pillow, PyMuPDF, the document libraries, the ONNX CLIP models and the selected speech model;
  - a CPU-only media extra keeps CUDA wheels out;
  - the image grows by an estimated 0.6–1.5 GB, depending on the speech model (unverified).
- **Code:**
  - the media worker's admission and pressure stop;
  - the ONNX CLIP backend;
  - the multilingual query encoder;
  - the new document extractors and HEIC decoding;
  - requeueing jobs blocked on a missing engine;
  - the CLIP lane's disabled state.
- **Capacity:** measured warm peaks per cell rise, so fewer friends fit on one node before the 20% headroom rule closes admission. Acceptance measures this before any switch is turned on.
- **Depends on:** the `shared-model-runtime` capability of `ship-models-as-shared-onnx`, which states the shared-weights rule that the media engines follow.
- **Tool surface:** the Cloud exclusions of `process_media` and `read_media` name a media-capable image as their lift condition. That requirement lives in the active change `adopt-exomem-cloud-plain-cells`, so any lift is amended there, not here.
- **Unchanged:**
  - cellctl, quotas, holds, backups and the rollout procedure;
  - the media Markdown sidecar format, and every field search reads. Only the image vector sidecar gains a record of its vector space.

## Pure substrate and soft-fail

- OCR, speech, the image encoders and the document extractors are frozen transducers. They transcribe or represent what the tenant stored, and decide nothing.
- Captions come only from a pinned-weight, frozen captioner. It is off by default because it emits prose, and an instruction-following model never writes them.
- Every engine is off on Cloud until its acceptance passes. A missing, disabled or stopped engine leaves lexical and dense recall serving, and never takes down the serving process.
