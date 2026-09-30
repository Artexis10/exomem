## 1. Cloud cells default to the multilingual model

- [x] 1.1 Red: the recall-switch table expects bge-m3 on a cloud cell and bge-base on a hosted cell (a malformed hosted flag included), with an explicit `EXOMEM_RECALL_MODEL` winning on any (`tests/test_recall_switch.py`)
- [x] 1.2 `recall_space.configured_recall_model` returns bge-base on a hosted cell and bge-m3 elsewhere unless `EXOMEM_RECALL_MODEL` names another model
- [x] 1.3 A `builder-cloud-model` stage fetches bge-m3 and loads it offline at its declared width; the cloud stage copies it and names it; the hosted stages keep bge-base, and their gate asserts the declared width (`tests/test_container_distribution.py`)

## 2. A cloud cell re-embeds with one encoder

- [x] 2.1 Red: a cell with an English sidecar never loads the English encoder, refuses the vector lane with `vector_space_mismatch` while lexical recall answers, and after the job's cutover serves dense recall from the new sidecar, including a page written during the gap (`tests/test_embedding_migration.py`)
- [x] 2.2 `recall_migration.run` runs on a cell; the old encoder stays unloaded there
- [x] 2.3 Doctor reports a cell's refused sidecar as dense recall off, and says when the kill switch keeps it off (`tests/test_embedding_migration.py`)

## 3. Verification and rollout

- [x] 3.1 Scoped suites green: recall switch, embedding migration, embedding index fingerprint, container distribution, and every test touching cell mode or the re-embed job
- [ ] 3.2 The hosted and cloud images build locally; the hosted gate loads bge-base at 768 and the cloud gate loads bge-m3 at 1024
- [ ] 3.3 Released; the owner cell runs the release, re-embeds to bge-m3 and cuts over; the cell's peak memory during the build and resident memory after it are recorded against its limit
- [ ] 3.4 The cloud parity smoke is re-run on the owner cell against the personal server, and the result is recorded
