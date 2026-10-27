# Tasks

## 1. Delivered documents and OCR

- [x] 1.1 Run extraction in the serialized cell-local worker, with no extraction in the serving process. Verify the worker boundary in `tests/test_media_worker.py`.
- [x] 1.2 Ship documents and OCR through the CPU-only media extra and offline image gate. Verify real format samples with `scripts/check-media-samples.py`.
- [x] 1.3 Add EPUB, OpenDocument and RTF extraction while preserving plain text, email and calendar processing without a switch. Verify `tests/test_media_processing.py`.
- [x] 1.4 Route OCR through installed script models and language packs, preserving CJK keyword text. Verify the configured samples and the correction in #1807.
- [x] 1.5 Add anonymous-memory admission, VmData limits, pressure stops and bounded timeout recovery. Verify `tests/test_media_brakes.py` and `tests/test_media_worker.py`.
- [x] 1.6 Requeue jobs when a missing engine appears, and keep disabled engines free of jobs or tenant install instructions. Verify the media processing and worker suites.
- [x] 1.7 Add per-cell engine selection and a fleet default without restarting cells whose enabled engines stay unchanged. Verify `infra/cellctl/tests/test_media_engine_selection.py`.
- [x] 1.8 Document engine rollout, budgets and recovery in `docs/runbooks/cloud-media.md`. Verify its configuration against the image and controller defaults.

Evidence: [#1661](https://github.com/Artexis10/exomem/pull/1661) delivered the first slice, including its extraction, pressure and recovery checks.
[#1798](https://github.com/Artexis10/exomem/pull/1798) added the fleet default; [#1807](https://github.com/Artexis10/exomem/pull/1807) corrected CJK recall.
Recorded 2026-10-17 deployment acceptance covers PDF, English OCR and Japanese OCR ingestion and keyword recall through the Cloud MCP connector.
These checks do not establish every format on a production cell, claude.ai attachment links, or the broader acceptance gates below.

## 2. Remaining documents and OCR acceptance

- [ ] 2.1 Reconcile the service-v1 memory gate with the media memory measure through the resource-policy owner. Verify one agreed measure before further acceptance.
- [ ] 2.2 Qualify owner-sized documents/OCR backfill against the existing latency, freshness, memory and node-headroom gates. State the drain bound before the run, and report excluded over-budget jobs.
- [ ] 2.3 Verify the known-content subset against a personal install under a predeclared agreement bound. Reuse existing image samples where their version and conditions match.
- [ ] 2.4 Record each accepted engine's anonymous-memory and VmData budgets, image size and OOM-group setting. Verify no OOM or serving restart during acceptance.
- [x] 2.5 Resolve whether new OCR packs need explicit reprocessing of completed sidecars. Verify that completed text stays unchanged unless that mode is requested.
  Decision (owner, 2026-10-19): no reprocessing mode for now. A new pack or OCR fix leaves completed text unchanged, and later work reopens this only when such a change alters real tenant sidecars.
  Evidence: `media_processing._completed_provenance_state` decides completion from the file identity and sidecar shape only, never from OCR configuration.
  `tests/test_media_processing.py::test_valid_completed_transcript_is_preserved_and_not_requeued` and `::test_process_media_product_leaf_preserves_a_completed_transcript` keep completed bytes through reconciliation, `process` and `retry`.

Automatic ingestion is delivered. Manual `process_media` exposure is decided in `adopt-exomem-cloud-plain-cells` task 2.3a: Cloud cells serve it.

## 3. Image search and optional captions

- [x] 3.1 Select the image model from published multilingual retrieval results under design D5. Record CPU speed, peak memory and shareable weights before implementation.
- [x] 3.2 Ship its pinned offline runtime through the shared-model contract. Verify same-precision parity where the existing vector space is retained.
- [ ] 3.3 Record model, width, precision and artifact identity in the image sidecar. Verify legacy-space reads, space-change backfill and refusal to mix spaces.
- [x] 3.4 Add the aligned multilingual query encoder. Verify supported-language retrieval and that a space-preserving encoder change keeps stored image vectors.
- [x] 3.5 Resolve HEIC decoder licensing before adding its dependency. Verify a real HEIC sample through the image gate and document the supported format.
- [ ] 3.6 Record whether to ship optional frozen captions under design D5. If selected, verify their independent switch, default-off behavior and sidecar search.
- [x] 3.7 Recheck disabled and empty image lanes without loading the query encoder. Verify neither state reports warming or degradation.

HEIC uses the shared pinned decoder and the generated `ocr-eng.heic` sample.
`tests/test_heic.py` verifies real decoding at the OCR and image-embedding boundaries.
Delivery requires the existing offline Cloud image gate on both architectures.
`tests/test_idle_clip.py` verifies disabled, empty, populated and failed-storage behavior through direct and prepared query paths.
Tasks 3.1 and 3.2 keep today's `clip-ViT-B-32` image space, so stored image vectors stay valid.
The multilingual query encoder of task 3.4 supplies multilingual retrieval in that space.
The image tower ships as a pinned fp32 ONNX artifact of the same model. Torch-free lanes select it, and torch lanes keep sentence-transformers.
Parity through the real `embed_image` on both lanes covered 33 images: photographs, alpha, greyscale, palette, CMYK, 16-bit, HEIC, extreme sizes and EXIF rotation. The minimum cosine was 0.99999985.
On 2 CPU threads, a warm encode took a median of 108 ms, and the peak resident set was 586 MiB, against 1043 MiB for the torch lane.
The weights are one external-data file loaded with prepacking off, so cells can share its pages; task 5.2 measures that sharing.
The Cloud image gate encodes a committed CC0 photograph offline and requires cosine 0.9999 against its recorded torch vector.
Image tags keep the torch text tower, so they stay unavailable on torch-free lanes.
Task 3.3 records the vector space, legacy-space reads and refusal to mix spaces; space-change backfill is not built.
Caption selection remains an open decision.

## 4. Speech

- [x] 4.1 Record the selected Whisper turbo default under design D6 and the shared-model contract. Separate diagnostic evidence from unfinished upgrade measurements.
- [x] 4.2 Ship the selected offline speech engine with measured budgets. Verify transcripts, silent-media markers and missing-engine recovery through the same worker path.
- [x] 4.3 Document the speech configuration and rollout procedure. Verify its switch and rollback leave existing sidecars readable and lexical recall serving.

The selected default is the pinned ONNX Whisper large-v3-turbo int8 artifact.
Delivery evidence: [#2142](https://github.com/Artexis10/exomem/pull/2142) contains the pinned artifacts, query parity, automatic transcript publication, speech budgets and offline image gates.
The query path retains the trained 512-dimensional projection and existing image vectors.
The real-model check covers normal and quiet speech, silence, fixed noise and corrupt input.
Quiet speech retains word errors; the voice detector repair prevents omission without changing Whisper input.
Cell-sized backlog acceptance and weight sharing across live cells remain tasks 5.1 and 5.2.
The wider bake-off no longer blocks this delivery. Future model or routing changes require the frozen quality comparison and fresh confirmation in D6.

- [ ] 4.4 Qualify later coverage or model upgrades on at least 100 languages and fresh confirmation, with the unchanged 2.0-point bound. This upgrade work does not gate the selected initial default.
- [ ] 4.5 Transcribe long recordings under design D6: language once per file, the reference decoding fallback and resumable progress. Verify on 30-minute English and Japanese recordings at 2 CPU: WER and CER against the source texts, per-window wall time, peaks, and a stopped run that resumes to the same transcript.

## 5. Integration and closure

- [ ] 5.1 Before each remaining engine rolls out, run its existing per-engine acceptance on the owner-sized cell and backlog. Verify resource gates, drain and content agreement.
- [ ] 5.2 For model-bearing engines, verify file-backed weights with node-level Pss evidence. Record sharing and measured budgets without counting shared pages twice.
- [ ] 5.3 Roll out each accepted engine under an operator-approved live change. Verify every cell, the fleet default and unchanged start times where no restart is expected.
- [ ] 5.4 Synchronize the completed deltas and archive this change only after its remaining contracts and acceptance tasks close. Verify strict OpenSpec validation before and after archival.

## 6. Retained result publication

- [x] 6.1 Bind retained media results to input and target evidence versions while keeping full-byte publication guards. Verify backlinks, meaningful edits, binary changes, concurrent writes, legacy rows and crash recovery through the media worker and job suites.

Evidence: `tests/test_media_result_staleness.py` covers inline and durable publication, concurrent edits and recovery on either side of the canonical write.
`tests/test_media_jobs.py` covers additive migration and optional evidence-version validation.

## 7. Node media-slot lease

- [x] 7.1 Add the media-slot service under design D10: the pool rule, first-in first-out grants, one entry per cell and per address, a lease counted from collection, the stated byte count and timeout, and the restart hold. Verify `tests/test_media_slot_lease.py`.
- [x] 7.2 Add the chart Deployment, Service, NetworkPolicy, values and schema, off by default. Verify `infra/cellctl/tests/test_platform_chart_headers.py`: with the switch off, the chart renders as before.
- [x] 7.3 Render the service URL and one egress rule into the selected cells through cellctl and its admission policy. Verify `infra/cellctl/tests/test_media_slot_lease.py`: a selected cell gains exactly one rule, and an unselected cell keeps its digest.
- [x] 7.4 Ask for a slot sized for the job the child will claim before each media child starts, release it when the child exits, and have the child refuse a heavier job. Verify an unreachable service leaves the job pending with no counted stop, and a recovered heavier job runs under a slot sized for it (`tests/test_media_slot_client.py`, `tests/test_media_worker_slot.py`).
- [ ] 7.5 Measure the pool on the production node with two cells contending, and set `mediaSlotLease.poolMiB` from the measured headroom. Verify no eviction and no OOM while both cells hold work.
- [ ] 7.6 Roll the service out under an operator-approved live change: one canary cell, then the fleet switch. Verify the runbook steps in `docs/runbooks/cloud-media.md` and that every cell holds a slot before it starts a child.
