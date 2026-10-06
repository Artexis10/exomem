## 1. Prove equivalence before changing anything

- [x] 1.1 Add a committed equivalence check that encodes a fixed text set, including
  empty, unicode, very long, and repeated-token edge cases, through both backends and
  asserts minimum cosine similarity above 0.9999 at identical dimensionality. Record the
  measured minimum rather than only asserting the bound.

- [x] 1.2 Capture the current torch baseline on the CPU profile the node runs — import
  cost, warm resident, peak, and encode throughput — so the after-measurement is a
  comparison rather than a claim.

## 2. Introduce the backend seam

- [x] 2.1 Add a failing test asserting `embed_texts()` returns identical-shaped, unit-norm
  vectors under either backend, selected by configuration, with no call-site change.

- [x] 2.2 Route `get_model()` through a backend seam. Keep the lazy singleton, the load
  lock, and the local heavy import so `test_cli_lazy_imports` continues to prove the
  framework stays off the hot path.

- [x] 2.3 Make `encode_batch_size()` take the batch policy from the backend rather than
  reading `model.device`, preserving the measured policy: 8 on CPU, 32 on accelerators.

- [x] 2.4 Make device selection backend-aware in `accel.py` by mapping the selected device
  onto execution providers for ONNX and leaving the torch path unchanged. Do not let a
  torch-only concept such as fp16-on-MPS leak into the ONNX path.

- [x] 2.5 Keep idle reaping working under both backends. `unload_model()`, `_ModelGuard`,
  and `BGE_GUARD` assume a torch object and a torch cache; give the seam an explicit
  release hook instead.

- [ ] 2.6 Record an embedding runtime fingerprint in the sidecar so a future model change
  is detected rather than silently mixing vector spaces. Prove that substituting the
  backend alone does not change the fingerprint and triggers no rebuild.

## 3. Make readiness backend-aware

- [x] 3.1 Add a failing doctor test for an installation whose configured backend is ONNX
  and where torch is absent, asserting the embeddings check passes and names the correct
  remediation extra.

- [x] 3.2 Replace the hardcoded dependency triple with a check against the configured
  backend, and scope the CUDA probe to backends that can use one.

## 4. Build the hosted image on the new runtime

- [x] 4.1 Add the backend to the dependency set as its own extra, and stop pinning the
  hosted lane to the CUDA torch index. Leave `ml` and `cuda` on torch — they still serve
  the reranker and CLIP.

- [x] 4.2 Build the hosted stage against the new backend, exporting or fetching the model
  in the ONNX form, and replace the offline load gate with one that constructs an
  inference session and asserts an encode succeeds with the hub pinned offline.

- [x] 4.3 Trim the framework artifacts the serving runtime never reads, keeping the ONNX
  weights, and assert the final hosted image contains no torch distribution.

- [x] 4.4 Prove the image embeds under the exact conditions a cell runs under —
  `--network none --read-only --user 10001:10001 --cap-drop ALL`.

## 5. Re-derive the envelope

- [ ] 5.1 Measure warm resident and peak for one cell on the new runtime, on the node
  profile in service, and record the numbers.
  - Measured in the hosted image under exact cell constraints (`--network none
    --read-only --user 10001:10001 --cap-drop ALL --memory=1536m`), batch 8, 200
    chunks: import 33 MiB, warm 548 MiB, **peak 604 MiB**, 22.7 chunks/s. The torch
    envelope this replaces was 918 MiB peak at the same batch size.
  - Still open because the cap also needs the platform's own memory delta, which
    cannot be measured until the platform is installed on the node.

- [ ] 5.2 Set the cell resource requests and limits and the namespace quota from that
  measurement, keeping the declared and rendered worker policy in step.

- [ ] 5.3 Raise the USER cell cap from the measurement, moving the operations contract and
  its byte-identical chart copy together with the limits the provisioner hardcodes, and
  correct the provisioner README, which still claims six cells against a contract of four.

- [x] 5.4 Measure whether cells on one node can share the model weights' memory
  without sharing a process. Use separate processes on one host to map one
  read-only weight file, then compare their total resident and shared pages
  against the same processes with private copies. Tenant text must never leave
  its own cell: a shared inference process stays rejected (proposal,
  alternatives). If ONNX Runtime copies the initializers into private memory,
  record that and close the task without a change.
  - Found on 2026-09-28: the owner cell's first index build over a 3.6 GB vault
    peaked above 1.2 GiB against a 1536 MiB limit. The peak was the build's working
    set, not the model, so this is a density measure, not the fix for that peak.
  - Measured on 2026-09-30 with bge-m3 int8, ORT 1.27.0, and 1/2/3 independent
    laptop processes: product-path total PSS 643/1265/1886 MiB (additional
    processes 622/621 MiB). Offline-optimized ORT loaded by path with
    `session.use_memory_mapped_ort_model=1`,
    `session.use_ort_model_bytes_for_initializers=1`, and
    `session.disable_prepacking=1`, with load-time optimization disabled,
    used 637/966/1295 MiB (additional processes 329/328 MiB), sharing 310 MiB
    of resident read-only model pages; private anonymous memory remained
    327 MiB per process. All successful variants were bit-identical on the
    probe's three short texts (max absolute difference 0, minimum cosine
    0.9999999999999998). External ONNX data without prepacking also shared
    pages (additional processes 332/331 MiB); Python mmap-buffer input was
    rejected. See `scripts/shared_weights_probe.py` for the repeatable probe.
    Container/cgroup confirmation remains part of 5.1/5.2 limit sizing: Linux
    charges page cache to the first cgroup that faults it, so do not assume a
    uniform ~331 MiB per cell until the behavior is measured on the node.
  - Latency measured on 2026-09-30 after warming each session, with three
    repetitions over 20 query texts (9-13 tokens) and 20 synthetic prose chunks
    (458-477 tokens). On the product `_OnnxEncoder` path, default prepacking took
    24.2 ms/query and 514.5 ms/chunk at the median; disabling prepacking took
    27.1 ms/query and 524.7 ms/chunk: about +12% per query text and +2% per chunk.
    The direct probe pair measured 23.5/493.4 ms with prepacking and 27.0/502.9
    ms without it (query/chunk respectively).

- [x] 5.5 Disable ONNX prepacking for served artifacts in Exomem Cloud cells so
  independent cell processes share file-backed weights; keep personal and hosted
  servers on the default prepacked path, with an explicit `EXOMEM_ONNX_SHARE_WEIGHTS`
  binary override.
  - Shipped in #1489. Measured in production on 2026-10-06: two 0.108.0 cells
    (`xuonhoiy3pvzqom6` and `abs7pee2cpysoumu`, image `sha256:2643e398…`) map the
    same `bge-m3` `model.onnx.data` file (host inode 3894058, containerd snapshot
    450334).
  - Of the 74,106 resident pages of that file, 74,102 (289.5 MiB) are the same
    physical frames in both processes, read from the pagemap PFNs and
    `/proc/kpagecount`.
  - Per process, Rss is 290.1 MiB and Pss is 144.8 MiB for that mapping.
  - The page cache is charged to `system.slice/k3s.service` (the image unpack), not
    to either cell. The cells' `memory.stat` `file` values are 18.0 MiB and 4.9 MiB.
  - Private memory is 225.3 MiB and 224.2 MiB per cell.

## 6. Verify

- [ ] 6.1 Run the full repository suite plus the pinned validation gate and strict
  OpenSpec validation.

- [ ] 6.2 Confirm retrieval quality is unchanged on the golden retrieval set rather than
  inferring it from cosine similarity alone.

- [ ] 6.3 Independent review of the equivalence evidence, the measured envelope, and the
  resulting cap.
