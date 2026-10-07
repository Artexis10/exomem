## Why

An Exomem Cloud cell recalls worse than a personal server holding the same vault. The owner's vault, smoked on the owner's cloud cell on 2026-09-30, lost pages on hybrid queries that the personal server answers. Cells encode with `BAAI/bge-base-en-v1.5`, an English model that also scores 0 on every cross-language query, while a personal server encodes with `BAAI/bge-m3`. The reason cells kept the English model was memory: the multilingual design put bge-m3 at 0.6-0.7 GB, "over the per-cell budget". That compared bge-m3 with nothing. A cell already holds bge-base. Measured through ONNX Runtime on CPU, bge-base fp32 is ~680 MiB resident and bge-m3 int8 is ~775 MiB, so the swap costs about 95 MiB. bge-m3 int8 is also faster per passage: 439 ms against 572 ms for a 350-word passage on two threads.

## What Changes

- A cloud cell (`EXOMEM_CLOUD_CELL`) encodes recall with `BAAI/bge-m3`, from the same pinned int8 artefact as a personal server. The cloud image carries that artefact and its tokenizer, fetched at build time and gated by an offline load at the model's declared width.
- A hosted cell (`EXOMEM_HOSTED_CELL`, the helm cell chart) keeps `BAAI/bge-base-en-v1.5`. Its runtime starts no re-embed job, and its chart pins a 1536 MiB limit that a large-vault index build has already exceeded.
- `EXOMEM_RECALL_MODEL` still names another model explicitly on any deployment.
- A cloud cell re-embeds when its sidecar was written by another model, holding one encoder. The old sidecar is refused rather than served by a second model: the vector lane reports `vector_space_mismatch` and the lexical, keyword, graph and temporal lanes answer until the cutover. Doctor reports that state as dense recall off, not as serving.
- `EXOMEM_RECALL_REEMBED=off` on such a cell keeps dense recall off. A new cloud cell starts on bge-m3 with nothing to re-embed.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `multilingual-recall`: cloud cells encode with bge-m3, and a cloud cell's re-embed runs on one encoder. The personal-server requirement is replaced by one naming personal servers, cloud cells and what hosted cells keep.

## Impact

- `src/exomem/recall_space.py` (default model per deployment), `src/exomem/recall_migration.py` (cells run the job), `src/exomem/doctor.py` (a cell's refused sidecar), `Dockerfile` (a `builder-cloud-model` stage, the cloud stage's model, the offline gates' width).
- The owner's cloud cell does one full re-embed after the release that carries this, with dense recall off for its length. Resident memory rises ~95 MiB, plus a third more for the vector matrix (1024 against 768 dimensions), inside the 3 GiB cloud-cell limit. `bound-cell-memory` owns the budget.
- The cloud image carries both models, since it builds on the hosted image: about 0.55 GB larger.
- No heavy optional capability is added. The encoder was already loaded on every cell, and a load failure soft-fails the vector lane as before. The model is the substrate for dense recall, which cells already ran.

> **Superseded on 2026-10-06 (#1608):** the cloud image no longer builds on the hosted image. Both build on a shared `cell-runtime` stage; the cloud image carries bge-m3 alone, so `EXOMEM_RECALL_MODEL=BAAI/bge-base-en-v1.5` on a cloud cell no longer serves an old sidecar.
