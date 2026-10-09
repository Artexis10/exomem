## Context

- **Every Cloud cell already runs the media machinery.** It starts the media supervisor (`server_runtime.py`) and its serialized, disposable child (`media_worker.py`). But the `cloud` image installs only `.[embeddings-onnx]`:
  - no torch;
  - no Pillow, Tesseract or faster-whisper.

  So every media job fails. The jobs stay blocked, because the callers leave `recover_interrupted(retry_blocked=...)` at its default `False`. The blocked next action reads "install the required media dependency, then retry" (`media_jobs._status_job`). The CLIP lane counts as enabled but cannot run.
- **Memory is the binding limit.**
  - A cell runs in 2 CPU and 3 GiB.
  - An owner-sized vault already peaked at 2,544,365,568 B (78.99% of 3 GiB) during an import checkpoint (`add-cloud-service-resource-policy` tasks, 2026-10-04).
  - Under cgroup v2, Kubernetes 1.28 and later set `memory.oom.group=1`, so a child that runs out of memory can take the serving process down with it. This is unverified on our node; acceptance checks it inside a cell.
  - Today only `os.nice(10)` bounds the child.

## Goals / Non-Goals

**Goals:**
- Cloud reaches parity with a personal install for documents, images and speech.
- The gaps that exist everywhere are closed: OCR languages, multilingual image queries, EPUB, OpenDocument, RTF and HEIC.
- Media never degrades search, and never shows its machinery to a tenant.

**Non-goals:**
- speaker diarisation (it needs a torch runtime; revisit separately);
- a shared inference service across cells;
- GPUs;
- third-party media APIs;
- video scene-frame JPEGs, which would raise backup cost.

## Decisions

### D1. Engines run in the existing disposable worker, inside the cell

Extraction runs only in the serialized media child. The serving process runs one media model, the CLIP text encoder, because the image lane encodes the query. The reaper unloads that encoder when idle.

Rejected alternatives:
- **A per-cell media Job.** Volumes are RWO and Jobs run with the runtime scaled to zero, so every media batch would cost downtime. Job pods also match the `job-egress` policy, so plaintext would sit in a pod with egress.
- **A shared media pool.** It needs a new path from the cell to the pool, and one process would hold several tenants' plaintext. With a few cells it saves little.

Escalation: if acceptance shows starvation or memory aborts, move the engines into a second container in the cell pod, with its own memory limit.

### D2. Memory brakes, derived from the cell

All three brakes are derived from the cell's own cgroup (`memory.max`, `memory.current`), never from constants. Each one, when it fires wrongly, costs only delay: a job waits and retries. Search never pays for it.

- **Admission.** The worker claims a job only when `memory.current` plus the engine's measured budget stays at or below 80% of `memory.max`.
- **Hard limit.** The child runs under a data-segment limit (`RLIMIT_DATA`) set to the engine's budget. Since Linux 4.7 it counts private writable memory, and not file-backed read-only mappings, so the shared weights of D3 do not count against it. An address-space limit (`RLIMIT_AS`) would count them. Tesseract and other native code inherit the limit.
- **Pressure stop.** The supervisor stops the child when cell memory crosses a high-water mark. The job returns to pending, never to an artifact failure.

Each engine's budget is measured during acceptance and pinned in deployment configuration.

A job that a brake stops repeatedly would otherwise loop. After a bounded number of consecutive stops, it becomes blocked with a typed memory reason, visible on runtime status. It never becomes failed, because its artifact is not at fault.

The 80% admission fraction is the `service-v1` cgroup peak gate ("Cloud outcome and capacity gates" in `add-cloud-service-resource-policy`). The spec references that gate rather than restating the number.

### D3. Pre-baked engines and shared weights

Every model and language pack is baked into an image layer and loaded with network access off, behind a build-time offline load gate.

Weights SHALL be file-backed and shared read-only across cells, as `onnx_share_weights_enabled` already does for the text encoder. On 2026-10-09 the Cloud node showed the bge-m3 int8 `model.onnx.data` mapped by 3 cell processes:

- Rss 290, 509 and 290 MB;
- Pss 96, 315 and 96 MB.

So the node held one copy of about 509 MB, not three. Each cell adds only its working memory while active, plus its own indexes, which stay per tenant. Only immutable model bytes are shared. No writable page, cache or process is shared between tenants, so sharing costs no isolation.

Two consequences:
- An engine whose runtime cannot map its weights, such as one that copies them into private memory, needs a measured reason to be chosen over a shareable one.
- Acceptance measures the sharing of each engine with Pss.

The rule itself is stated once, in the `shared-model-runtime` capability of the change `ship-models-as-shared-onnx`. This change applies it to the media engines, and its spec references that capability. So `ship-models-as-shared-onnx` lands first, or in the same delivery.

### D4. Documents

Cloud reads every type a personal install reads: PDF through PyMuPDF, DOCX, XLSX, PPTX, HTML, TXT, EML and ICS. Every install gains EPUB, ODT, ODS, ODP and RTF.

A personal install already converts DOCX, XLSX, PPTX and HTML with Microsoft MarkItDown (the `media` extra in `pyproject.toml`), and reads PDF with PyMuPDF. HEIC is already in the image registry (`media_types.IMAGE_EXTS`), but it fails to decode without a HEIF decoder.

Before writing any extractor for the new formats, implementation compares these libraries for format coverage, licence, size and maintenance:
- a maintained converter that covers many formats (for example `markitdown`);
- per-format libraries.

Reuse wins where it fits. A format is "supported" only when a check in the Cloud image extracts a real sample of it.

### D5. Images

- **OCR.** Tesseract 5 with two kinds of model:
  - every **script** model Tesseract publishes (Latin, Japanese and Japanese vertical, Han, Cyrillic, Arabic, Devanagari, Hangul and the rest). Each one reads every language written in its script;
  - dedicated **language** packs for the languages our users write: `eng`, `jpn`, `jpn_vert` and `est` first. A language pack beats its script model on that language's own words.

  Orientation and script detection runs first, and then only that script's models run. So installed models cost image size only, never time or memory on an unrelated page.
  - When detection names no script, which happens on images with little text, OCR reads with the deployment's configured default language packs.
  - Today every install calls Tesseract without a language, so it reads English only (`extract._ocr_image` and `extract._ocr_pdf_page`).
  - The installed set is an image build parameter, which makes it deployment data, never a code list.
  - Mapping scripts to models is a closed set that Tesseract defines.
  - Acceptance records the image size the models add.
- **Image search model.** It is chosen by published benchmarks, not by our own accuracy runs. The candidates are:
  - `clip-ViT-B-32`, today's model, English queries only, with the multilingual text encoder `clip-ViT-B-32-multilingual-v1` aligned to its image space;
  - a current multilingual image-text model of the SigLIP 2 class, which handles images and text in many languages in one model.

  The rule:
  - use published multilingual image–text retrieval results, such as Crossmodal-3600 recall@k for English and Japanese;
  - pick the best model whose measured CPU speed and memory on our hardware fit the media budget, with its weights shareable under D3.

  If the model changes, every install switches together, and stored image vectors are re-encoded once by D8's backfill. Vectors from different models are never mixed.
  - Today the image vector sidecar (`.clip.sqlite`) records no vector space: `clip_index` fixes the width at 512 and stores no model. So this change extends the `multilingual-recall` space rule to that sidecar. It records the model and, for a served artifact, the same artifact identity that a recall sidecar records. A legacy sidecar with rows and no record is read as `clip-ViT-B-32` at 512 dimensions.
  - Parity of an ONNX build against its reference at the same precision stays cosine ≥ 0.9999 (the existing `MIN_COSINE` in `tests/test_embedding_backend.py`). That bound is for a runtime substitution. An int8 build does not meet it against fp32: a shared int8 batch alone moves a vector by up to 0.02 cosine (archived `make-recall-multilingual` design). So the stored vectors stay interchangeable across installs only while every install runs the same artifact, or artifacts that meet the bound against one reference.
- **Image captions.** Optionally, a small pinned-weight, frozen captioner writes one descriptive sentence per image, chosen by the same benchmark-then-measure rule. The caption joins the image's OCR text in ordinary multilingual semantic search, so a full-sentence question in any language finds the photo, and the assistant can read what the photo shows.
  - The captioner is never an instruction-following model. The authority matrix in `openspec/config.yaml` admits pinned-weight frozen captioners as transducers, default-off because they emit prose. It puts an instruction-following generative model out of bounds in every modality pairing, except as an instrument.
  - The hook exists today as `EXOMEM_VISION_CAPTION`, off by default. Its default checkpoint (`Salesforce/blip-image-captioning-large`) runs through the transformers pipeline on torch, so Cloud needs a build that meets the shared model runtime rule.
  - It ships behind its own D7 switch after images.
- **HEIC.** Decoded through `pillow-heif`, after a licence check of its bundled decoders.

### D6. Speech

The engine and model are chosen by the bake-off recorded below:
- faster-whisper `small` and `large-v3-turbo`, int8;
- Parakeet-TDT-0.6B-v3 in ONNX;
- SenseVoice-Small in ONNX;
- optionally, Whisper `large-v3-turbo` in ONNX.

Accuracy comes from published results: the Open ASR Leaderboard, model cards and papers, with FLEURS or Common Voice for English, Japanese and Estonian. We measure only what no benchmark covers:
- the int8 real-time factor on pinned CPU threads, calibrated to the cell's CPU;
- peak memory;
- whether the weights can be shared under D3.

A 10-utterance-per-language sanity check catches a broken int8 or ONNX conversion.

The frozen rule picks the lowest-memory arm whose published error is within 2.0 points of the best arm in every language. Shareable weights count once per node. Routing by language is allowed only if no single arm passes, and only if the routed pair stays under 1.5 GB.

Bake-off result: *pending; recorded here when the run completes.*

### D7. Off until proven, and the disabled state

Each engine (documents, OCR, image search, captions, speech) has a deployment switch.
- **Cloud:** the switch stays off until that engine's acceptance passes on a real cell.
- **Personal installs:** they keep today's defaults.

On Cloud, an engine that the image does not ship is disabled by the deployment's configuration. On a personal install, an enabled engine that is missing keeps today's blocked state with install guidance, and D8 requeues its jobs once it appears.

An engine that is off or not shipped:
- is reported as disabled on runtime status and doctor;
- is not a warming or degraded component on queries;
- creates no per-file jobs that are bound to fail;
- never shows a tenant an install instruction.

On Cloud, the next action of a blocked job never tells the tenant to install anything. A shipped engine that cannot load is reported as unavailable on this deployment, for the operator to repair.

Until images ship, the interim is configuration only: `EXOMEM_DISABLE_CLIP=1` in the `cloud` stage. That interim is in-flight work on the branch `fix/recall-during-initial-build`, which has no OpenSpec change or pull request yet. It is not part of this change.

### D8. Backfill and recovery

- **Backfill.** When an engine's switch turns on, media already in the cell is queued automatically. Rollout turns engines on one cell at a time, so backfill never runs in every cell at once. On every install, the same backfill is reconciliation: media that waited with a pending sidecar and no job gets its stage once the engine is enabled.
- **Recovery.** A job blocked with the typed reason "engine unavailable" is requeued when the engine appears. Today that reason is an `ExtractionUnavailable` error (`media_worker`). Other blocked reasons keep today's behaviour.

## Acceptance (per engine, before its switch turns on)

Run on the owner-sized cell with that engine's backlog active:

- the existing service-v1 gates hold. They live in "Cloud outcome and capacity gates" of `add-cloud-service-resource-policy`, and the spec references them there:
  - a cgroup peak of at most 80% of the limit;
  - at least 20% node headroom;
  - query p95 of 3 s or less;
  - publication p95 of 5 s or less;
- there are no OOMs and no serving restarts, and `memory.oom.group` has been read and recorded;
- outputs match a personal install in shape. Image vectors meet the parity bound;
- shared weights are measured on the node: the page cache counts the file once.

## Risks / Trade-offs

- **The image grows by 0.6–1.5 GB.** That costs pull time on rollout only.
- **Fewer cells fit per node.** Acceptance measures the new warm peaks before admission counts them.
- **A pressure stop can delay media for the largest vault.** The delay is visible on runtime status only. The escalation is D1's second container.

## Migration Plan

1. Release 1: documents and images, with their switches off.
2. Turn each engine on per cell after its acceptance, owner first, then QA, then the reviewer cell.
3. Release 2: speech, and image vectors of sampled video frames, in the same pattern. Scene-frame JPEGs (`EXOMEM_VIDEO_SCENE_FRAMES`) stay off, as the non-goals say.
4. Rollback: turn the switch off. That stops new jobs, and stored sidecars and vectors stay valid.

## Open Questions

- The speech engine: pending the bake-off (D6).
- Whether `markitdown` or per-format libraries cover D4 at an acceptable size and licence.
- The default OCR language packs used when script detection names no script, and how many consecutive memory stops block a job (D2). Both are deployment values that acceptance sets.
- Whether installing a new OCR pack re-extracts images whose OCR completed without it. The spec keeps completed text unless an explicit reprocessing mode asks, which is today's rule.
- Whether the `process_media` and `read_media` Cloud exclusions lift when their engines turn on. Their recorded lift condition is "a media-capable cloud image ships" (`commands.CLOUD_SURFACE_EXCLUSIONS`). That requirement lives in the active change `adopt-exomem-cloud-plain-cells`, so an amendment belongs there.
- Zero-shot image tags (`EXOMEM_IMAGE_TAGS`) reuse the loaded image model and score an English tag vocabulary. If D5 selects another image model, the tags follow it, and their threshold needs recalibration.
