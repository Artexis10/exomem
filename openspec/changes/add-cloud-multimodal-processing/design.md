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

The media bytes and the extraction process stay in the cell, and no third party receives media for extraction. This promise covers extraction only. Later reads of the stored text follow the capabilities that own them. One of those is the per-tenant opt-in API instrument of `add-sensed-epistemic-model` (its ruling R7).

Rejected alternatives:
- **A per-cell media Job.** Volumes are RWO and Jobs run with the runtime scaled to zero, so every media batch would cost downtime. Job pods also match the `job-egress` policy, so plaintext would sit in a pod with egress.
- **A shared media pool.** It needs a new path from the cell to the pool, and one process would hold several tenants' plaintext. With a few cells it saves little.

Escalation: if acceptance shows starvation or memory aborts, move the engines into a second container in the cell pod, with its own memory limit.

### D2. Memory brakes, derived from the cell

All three brakes are derived from the cell's own cgroup, never from constants. Each brake names the measure it reads, because the obvious one is wrong:

- `memory.current` counts reclaimable page cache. It also counts shared model weights, charged to whichever cell faulted them first (change `ship-models-as-shared-onnx`, design Context). So it can block a cell that has room, or admit work into a cell that has none. Admission and the pressure stop never read it.
- **Admission** reads anonymous memory (`memory.stat` anon). The worker claims a job only while anonymous memory plus the engine's measured anonymous-memory budget stays below the lower of two values. The budget is anonymous memory too, so the shared weight pages never count twice:
  - the cell's `memory.high`, when one is set;
  - the `service-v1` admission fraction of `memory.max`, which is the 80% cgroup peak gate in "Cloud outcome and capacity gates" (`add-cloud-service-resource-policy`). The spec references that gate rather than restating it.

  With kubelet MemoryQoS on, `memory.high` = request + 0.625 × (limit − request) (`memoryThrottlingFactor` in the K3s role). At a 1 GiB request and the 3 GiB limit that is 2.25 GiB, or 75%, which is below 80%. At cellctl's default 512 MiB request it is about 2.06 GiB. MemoryQoS is off by default (`k3s_memory_qos_enabled: false`), and then `memory.high` is `max`. The request in production was not checked for this design.
- **Pressure stop** reads memory pressure stall information (`memory.pressure`). The supervisor stops the child when the `some` or `full` 10-second average (`avg10`) crosses a stall threshold set in deployment configuration. A stall threshold measures time lost to reclaim, so it does not depend on the cell's size. Where `memory.pressure` cannot be read, the fallback stops the child when anonymous memory reaches the admission ceiling: the lower of `memory.high`, when set, and the profile's admission fraction of `memory.max`. It never waits for `memory.max`, where the OOM killer acts; with MemoryQoS off, `memory.high` is unset, so the admission fraction is the ceiling.
- **Hard limit** is a VmData budget (`RLIMIT_DATA`), and it is a backstop against runaway allocation, not the main brake.
  - ONNX Runtime maps `model.onnx.data` as `rw-p`: private and writable. VmData counts that mapping. The review reproduced it on ORT 1.27.0: a limit of VmData + 100 or 120 MiB failed the load with `std::bad_alloc`, and VmData + 140 MiB passed.
  - So the budget includes the mapped weights. Each engine's VmData budget is measured at acceptance and pinned with a margin.
  - The pages stay clean, because prepacking is off and nothing writes them. So they stay shared, and the Pss sharing evidence stands.
  - Tesseract and other native code inherit the limit.

Each engine has two measured budgets: an anonymous-memory budget for admission, and a VmData budget for the hard limit. Both are pinned in deployment configuration.

**What a wrong firing costs.** A brake that fires wrongly delays media; it never takes search down. Delay is cheap only while it is visible and bounded, so:

- A pressure stop, and an allocation failure under the hard limit (an ORT `bad_alloc`, a native allocation failure), are typed memory stops. The job returns to pending and is never an artifact failure.
- After a bounded number of consecutive memory stops, the job leaves the cycle in one of two typed states, so it cannot loop:
  - **Exceeds this deployment's processing budget.** Each stop was an allocation failure under the child's own VmData hard limit. That is the only evidence about the file itself: anonymous demand above the engine's budget also raises VmData, so the hard limit catches an oversized file. Examples are a huge panorama, or a corrupt header that asks for a giant buffer. The tenant sees that the file exceeds this deployment's processing budget, with no install and no "corrupt" wording. The job returns to pending only when the cell limit or that engine's budget changes, because nothing else can make it fit.
  - **Memory-blocked**, in every other case, including every case with a pressure stop. Pressure can come from the whole cell: backfill encoding in the serving process, the sensor child, an import checkpoint at 79% of the limit. Blaming the file for it would park ordinary files with a false reason, and the drain gate would then pass while they wait. So a pressure stop never leads to the over-budget state. The job returns to pending automatically: when a supervisor starts, when the cell limit or an engine budget changes, and periodically with a bounded backoff while pressure is low. No human retry is needed. On Cloud none would be possible anyway, because `process_media` is excluded from the cell's tool surface. A tenant sees the job only as waiting, with no action to take, and the memory reason appears on operator surfaces only.
- **Time verdict.** A watchdog stop of a child that outruns the job timeout counts apart from memory stops, and after the same bound the job takes the over-budget outcome, returning to pending only when the job timeout grows past the one it exceeded.
- Starvation is caught before rollout: acceptance requires the owner-sized backlog to drain within a bound stated before the run, with no job left memory-blocked. Over-budget jobs are excluded from the drain and listed in the acceptance report with their file size and type.

### D3. Pre-baked engines and shared weights

Every model and language pack is baked into an image layer and loaded with network access off, behind a build-time offline load gate.

The engines and their dependencies install in a build stage that only the `cloud` target uses, never in `builder-hosted`. So the Hosted image still carries only the runtime it serves with (the `hosted-tenant-cell` delta of `swap-embedding-runtime-to-onnx`).

The weights follow the rule of the `shared-model-runtime` capability (change `ship-models-as-shared-onnx`): file-backed, read-only and shared across the cells on a node. That change's design records the 2026-10-09 production measurement, in which three cells held one copy of the bge-m3 weights. Each cell adds only its working memory while active, plus its own indexes, which stay per tenant. Acceptance measures each engine's sharing with Pss.

This change applies that rule to the media engines, and its spec references the capability. So `ship-models-as-shared-onnx` lands first, or in the same delivery.

### D4. Documents

Cloud reads every type a personal install reads: PDF through PyMuPDF, DOCX, XLSX, PPTX, HTML, TXT, EML and ICS. Every install gains EPUB, ODT, ODS, ODP and RTF.

A personal install already converts DOCX, XLSX, PPTX and HTML with Microsoft MarkItDown (the `media` extra in `pyproject.toml`), and reads PDF with PyMuPDF. HEIC is already in the image registry (`media_types.IMAGE_EXTS`), but it fails to decode without a HEIF decoder.

Before writing any extractor for the new formats, implementation compares these libraries for format coverage, licence, size and maintenance:
- a maintained converter that covers many formats (for example `markitdown`);
- per-format libraries.

Reuse wins where it fits.

**Support proof.** On Cloud, a format is supported only when the image build extracts a real sample of it; the build fails otherwise. No runtime surface lists formats. On a personal install, a format whose dependency is missing keeps today's blocked state with install guidance.

**Plain text, email and calendar files work on Cloud today.** `extract._extract_textfile`, `_extract_eml` and `_extract_ics` use only the Python standard library. On 2026-10-09 I ran all three through `extract.extract_text` in a development environment, and no media module (torch, PIL, pytesseract, fitz, markitdown, faster_whisper) was imported. I did not run them inside the Cloud image, but nothing in the `cloud` stage blocks them. So the documents switch covers only the formats that need newly shipped dependencies. TXT, EML and ICS stay on, and nothing regresses.

### D5. Images

- **OCR.** Tesseract 5 with two kinds of model:
  - every **script** model Tesseract publishes (Latin, Japanese and Japanese vertical, Han, Cyrillic, Arabic, Devanagari, Hangul and the rest). Each one reads every language written in its script;
  - dedicated **language** packs for the languages our users write: `eng`, `jpn`, `jpn_vert` and `est` first. A language pack beats its script model on that language's own words.

  Orientation and script detection runs first, and then only that script's models run. So installed models cost image size only, never time or memory on an unrelated page.
  - When detection names no script, which happens on images with little text, OCR reads with the deployment's configured default language packs.
  - Today every install calls Tesseract without a language, so it reads English only (`extract._ocr_image` and `extract._ocr_pdf_page`).
  - The installed set is an image build parameter, which makes it deployment data, never a code list.
  - Each installed pack's script coverage is read from the pack's own data: the script property of its unicharset, or Tesseract's documented equivalent. Implementation verifies which one Tesseract exposes. If neither can be read at run time, the image build derives a coverage file from the installed packs; it is never hand-written.
  - A pack belongs to every script its data covers. Kanji-only Japanese may be detected as Han, so the Han route includes the deployment's Japanese packs.
  - Acceptance records the image size the models add.
- **Image search model.** It is chosen by published benchmarks, not by our own accuracy runs. The candidates are:
  - `clip-ViT-B-32`, today's model, English queries only, with the multilingual text encoder `clip-ViT-B-32-multilingual-v1` aligned to its image space;
  - a current multilingual image-text model of the SigLIP 2 class, which handles images and text in many languages in one model.

  The rule:
  - use published multilingual image–text retrieval results, such as Crossmodal-3600 recall@k for English and Japanese;
  - pick the best model whose measured CPU speed and memory on our hardware fit the media budget, with its weights shareable under D3.

  If the model changes, every install switches together, and stored image vectors are re-encoded once by D8's backfill. Vectors from different spaces are never mixed.
  - **Precision.** The image model ships at its reference precision, as an fp32 ONNX build. No image relevance fixture exists, so nothing could gate an int8 build (`shared-model-runtime`; change `ship-models-as-shared-onnx`, design D3, explains why int8 cannot meet the 0.9999 bound). int8 waits until such a fixture exists.
  - **One rule for the space.** A same-precision substitution that passes cosine ≥ 0.9999 keeps the vector space, and its new artifact identity is recorded. So an fp32 ONNX build of today's `clip-ViT-B-32` keeps every stored vector of a personal install. Another model or another precision is another space.
  - The record names the space, not the writer of each row. Rows that PyTorch wrote stay in the kept space on the strength of the substitution's parity proof.
  - Values calibrated on a space carry across a space-keeping substitution; the image-tags threshold (`EXOMEM_IMAGE_TAGS_THRESHOLD`) is one. A space change voids them until they are calibrated again.
  - **Recorded space.** Today the image vector sidecar (`.clip.sqlite`) records no vector space: `clip_index` fixes the width at 512 and stores no model. This change extends the `multilingual-recall` space rule to that sidecar: it records model, width, precision and artifact identity. A legacy sidecar with rows and no record is read as `clip-ViT-B-32` at 512 dimensions and full precision.
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

Selection follows the general rule of the `shared-model-runtime` capability. The speech terms add only what speech needs, and the spec states them:

- **Metric per language.** WER for English and Estonian, which are written with spaces between words, and CER for Japanese, which is not.
- **One benchmark.** Candidates are compared on the same published benchmark wherever one covers them all: the Open ASR Leaderboard, then FLEURS or Common Voice results from model cards and papers.
- **Missing results.** When any candidate lacks a published result in a required language, every candidate is measured for that language on the full FLEURS test split. That language is then judged on those measurements only. A measurement is never compared with a published number, because the two differ in normalisation and decoding.
  - The full split costs only local CPU, and it is expected to give intervals narrower than the 2.0-point margin (to be confirmed by the first run).
  - Each difference from the best candidate is reported with its 95% bootstrap confidence interval, resampling the utterances. On a measured language, a candidate passes the 2.0-point bound only when the upper end of its interval is within 2.0 points.
  - The owner's amendment to the bake-off allows this measurement. That amendment is not recorded in this repository.
- **Cost.** The int8 real-time factor at pinned CPU threads on the cell's CPU, peak memory, and whether the weights can be shared under D3.
- **Sanity check.** Ten utterances per language catch a broken int8 or ONNX conversion.
- **Pick.** The lowest-memory candidate whose error rate is within 2.0 points of the best in every required language. Shareable weights count once per node. Routing by language is allowed only if no single candidate passes, and only if the routed pair stays under 1.5 GB.

Bake-off result: *pending; recorded here when the run completes.*

### D7. Off until proven, and the disabled state

Each engine (documents, OCR, image search, captions, speech) has a switch.
- **Cloud:** every switch defaults to off. The operator turns it on first in the owner's cell, the acceptance cell and canary, and runs that engine's acceptance there. A pass rolls the switch to the other cells one at a time. A miss turns it off again in the owner's cell, and it stays off elsewhere.
- **Personal installs:** they keep today's defaults.

**Restart cost.** Under the current cellctl render, changing a cell's switch re-renders that cell and restarts its pod. A cell has one replica on a single-attach volume, so that is a brief outage. While that holds, the canary and the rollout change switches outside the cell backup window (`CELLCTL_BACKUP_WINDOW`). A later hot reload of the switch would remove the restart, and the spec allows one.

**Where the switch lives.** cellctl renders each cell's environment. Today it has one chart-level `model_env` map (`CELLCTL_CELL_MODEL_ENV`), which it renders into every cell after `check_model_env` refuses forbidden keys, prefixes and suffixes. A change to that map changes every cell's render digest at once, so it cannot hold a per-cell canary. cellctl selects cells per feature only through cell-ID lists, such as `CELLCTL_ARTIFACT_BROKER_CELL_IDS` and `CELLCTL_DEDICATED_CELL_IDS`. So this change adds a per-engine cell-ID selection to cellctl on that precedent. It renders the engine's switch variable into the selected cells only, and the variable passes the same `check_model_env` rules.

The documents switch covers only the formats that need newly shipped dependencies; TXT, EML and ICS stay on (D4).

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

## Acceptance (per engine, in the owner's cell, before the switch rolls out)

Run on the owner-sized cell with that engine's backlog active:

- the service-v1 latency and freshness gates hold. They live in "Cloud outcome and capacity gates" of `add-cloud-service-resource-policy`, and the spec references them there: query p95 of 3 s or less, and publication p95 of 5 s or less;
- the memory peak is read as anonymous plus non-reclaimable memory, sampled at least once per second, and stays within the service profile's peak fraction (80% of the limit). `memory.stat` is sampled, so a spike shorter than the interval can be missed; the no-OOM gate is the backstop for those. The fields come from `memory.stat`: `anon`, `slab_unreclaimable`, `kernel_stack`, `pagetables`, `percpu` and `sock`, plus `shmem`, which cannot be reclaimed without swap. The final field list is fixed at implementation and recorded with each run.
  - The service-v1 peak gate (`add-cloud-service-resource-policy` tasks, 2026-10-04) reads an inclusive cgroup peak that counts page cache. Large media reads can fill the cache up to `memory.max`, so that gate could fail on reclaimable cache alone. See Open Questions.
- node headroom stays at 20% or more under the service-v1 rule;
- there are no OOMs and no serving restarts, and `memory.oom.group` has been read and recorded;
- the owner-sized backlog drains within a bound stated before the run, and no job is left memory-blocked. Jobs over the processing budget are excluded from the drain, and the report lists each with its file size and type;
- a labelled known-content subset per format and engine agrees with a personal install's extraction of the same files, under an agreement bound stated before the run:
  - documents and OCR images whose text is known;
  - speech clips with reference transcripts;
  - silent media, which must produce its marker: a video without audio gets the `no-audio` engine and "(no text detected)", and audio without speech gets "(no speech detected)".

  This replaces a per-sidecar check. "Non-empty text" always passes, because "(no text detected)" is text, and a `+timed` check fails on silent video.
- image vectors carry the recorded model, width and precision, and meet the parity bound;
- shared weights are measured on the node: the cells' Pss for the weights file sums to about one copy;
- the engine's anonymous-memory and VmData budgets are recorded and pinned.

## Risks / Trade-offs

- **The image grows by 0.6–1.5 GB.** That costs pull time on rollout only.
- **Turning an engine on restarts the cell under the current cellctl render.** That is a brief outage per cell, outside the backup window.
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
- The default OCR language packs used when script detection names no script, how many consecutive memory stops end the cycle, and the pressure stall threshold (D2). All are deployment values that acceptance sets.
- **Reconcile the memory gate before the first acceptance run.** The service-v1 peak gate counts page cache, and media acceptance reads anonymous plus non-reclaimable memory. The owner of `add-cloud-service-resource-policy` decides how the two measures agree. This change does not edit that change.
- The agreement bounds for the known-content subsets and the drain-time bound are stated before each run.
- Whether installing a new OCR pack re-extracts images whose OCR completed without it. The spec keeps completed text unless an explicit reprocessing mode asks, which is today's rule.
- Whether the `process_media` and `read_media` Cloud exclusions lift when their engines turn on. Their recorded lift condition is "a media-capable cloud image ships" (`commands.CLOUD_SURFACE_EXCLUSIONS`). That requirement lives in the active change `adopt-exomem-cloud-plain-cells`, so an amendment belongs there.
- Zero-shot image tags (`EXOMEM_IMAGE_TAGS`) reuse the loaded image model and score an English tag vocabulary. If D5 selects another image model, the tags follow it, and their threshold needs recalibration.
