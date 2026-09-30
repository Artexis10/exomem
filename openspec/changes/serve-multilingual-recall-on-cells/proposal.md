## Why

A hosted or cloud cell recalls worse than a personal server holding the same vault. The owner's vault, smoked on the owner cell on 2026-09-30, lost pages on hybrid queries that the personal server answers. Cells encode with `BAAI/bge-base-en-v1.5`, an English model that also scores 0 on every cross-language query, while a personal server encodes with `BAAI/bge-m3`. The reason cells kept the English model was memory: the multilingual design put bge-m3 at 0.6-0.7 GB, "over the per-cell budget". That compared bge-m3 with nothing. A cell already holds bge-base. Measured through ONNX Runtime on CPU, bge-base fp32 is ~680 MiB resident and bge-m3 int8 is ~775 MiB, so the swap costs about 95 MiB. bge-m3 int8 is also faster per passage: 439 ms against 572 ms for a 350-word passage on two threads.

## What Changes

- A hosted or cloud cell encodes recall with `BAAI/bge-m3` from the same pinned int8 artefact as a personal server. `EXOMEM_RECALL_MODEL` still names another model explicitly.
- The hosted and cloud images carry the bge-m3 artefact and its tokenizer, fetched at build time and gated by an offline load at the model's declared width.
- A cell re-embeds when its sidecar was written by another model, holding one encoder. The old sidecar is refused rather than served by a second model. The vector lane reports `vector_space_mismatch` and the lexical, keyword, graph and temporal lanes answer until the cutover. `EXOMEM_RECALL_REEMBED=off` still builds nothing.
- An existing cell loses dense recall for the length of its re-embed: hours for a large vault on two cores. A new cell starts on bge-m3 with nothing to re-embed.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `multilingual-recall`: cells encode with bge-m3 (the personal-server requirement is replaced by one covering every deployment). The blue/green re-embed runs on a cell with one encoder, and the old sidecar is refused until the cutover.

## Impact

- `src/exomem/recall_space.py` (default model), `src/exomem/recall_migration.py` (cells run the job), `Dockerfile` (hosted build stage and runtime model, offline gate width).
- Owner cell: one full re-embed after the release that carries this. Resident memory rises ~95 MiB, plus a third more for the vector matrix (1024 against 768 dimensions). This stays within the 3 GiB default cell limit; `bound-cell-memory` owns the budget.
- No heavy optional capability is added. The encoder was already loaded on every cell, and a load failure soft-fails the vector lane as before. The model is the substrate for dense recall, which cells already ran.
