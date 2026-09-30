## 1. Cells default to the multilingual model

- [x] 1.1 Red: the recall-switch table expects bge-m3 on a hosted and a cloud cell, with an explicit `EXOMEM_RECALL_MODEL` winning on either (`tests/test_recall_switch.py`)
- [x] 1.2 `recall_space.configured_recall_model` returns bge-m3 unless `EXOMEM_RECALL_MODEL` names another model
- [x] 1.3 The hosted build stage and runtime name `BAAI/bge-m3`; the offline load gate asserts `recall_space.declared_dim` (`tests/test_container_distribution.py`)

## 2. A cell re-embeds with one encoder

- [x] 2.1 Red: a cell with an English sidecar never loads the English encoder, refuses the vector lane with `vector_space_mismatch` while lexical recall answers, and after the job's cutover serves dense recall from the new sidecar, including a page written during the gap (`tests/test_embedding_migration.py`)
- [x] 2.2 `recall_migration.run` runs on a cell; the old encoder stays unloaded there

## 3. Verification and rollout

- [x] 3.1 Scoped suites green: recall switch, embedding migration, embedding index fingerprint, container distribution
- [ ] 3.2 The cloud image builds locally and its offline gate loads bge-m3 at 1024 dimensions
- [ ] 3.3 Released; the owner cell runs the release, re-embeds to bge-m3 and cuts over; the cell's peak memory during the build and resident memory after it are recorded against its limit
- [ ] 3.4 The cloud parity smoke is re-run on the owner cell against the personal server, and the result is recorded
