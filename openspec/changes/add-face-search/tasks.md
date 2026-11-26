## 1. Models and manifest

- [x] 1.1 Add `face-detect` (YuNet, dynamic-shape export, MIT) and `face-embed` (SFace, Apache-2.0) to `media_models.json`: Hugging Face revision, digest, licence sources, calibration values (score 0.9, NMS 0.3, cosine 0.363, each with its upstream source) and the input and output shapes the build records.
- [x] 1.2 Extend `scripts/build-media-artifacts.py` with both entries. At build time only, compare the numpy decode, NMS and alignment with OpenCV's `FaceDetectorYN` and `FaceRecognizerSF` on fixture images.
- [x] 1.3 Test: the manifest check refuses a face model entry with no licence source. Catches: a model swap that drops its licence evidence.
- [x] 1.4 Test: with faces off, processing an image acquires and loads no face model. Catches: faces off still downloading or loading biometric models.

## 2. Face store and state registry

- [x] 2.1 Register two external families under `private_state.store(vault_root, "faces")`: `face-index` (rebuildable) and `face-people` (not rebuildable).
- [x] 2.2 Test: a personal backup of a state root with both stores stages the people store and no face-index file. Catches: face vectors in a restic snapshot, or names lost on restore.
- [x] 2.3 `infra/cellctl`: add the face-index files to the cell backup exclusions. Test: the exclusion mirror equals the registry's rebuildable face family, and the rendered restic command excludes it. Catches: a Cloud cell backup that keeps face vectors.
- [x] 2.4 Face store modules: face index (faces with model id, settings, consent record, generation) and people store (groups with centroids, people, group-person links, exclusions, threshold overrides); `secure_delete` on, owner-only directory mode, indexes on person and page.
- [ ] 2.5 Test: a people store restored without a face index re-attaches names after a re-scan, and faces stay off until the owner opts in again. Catches: a restore that turns faces on, or loses names.
  The schema already supports it: the setting and consent live in the rebuildable face index, and groups and names in the people store. The test needs grouping (section 4).

## 3. Detection pipeline

- [ ] 3.1 Face step in `media_worker` after the CLIP step, under the same identity and staleness checks; one decode pass serves CLIP and faces for a video; scene-frame images are skipped; failures soft-fail.
- [ ] 3.2 Backfill on turn-on: bounded, resumable batches in sorted path order at background priority; progress in `faces.status`, with `unknown` for an unknown total.
- [ ] 3.3 Test: an image with two fixture faces gets two rows with boxes in `read_media` region form. Catches: boxes in rotated or pixel coordinates that `read_media` would crop wrongly.
- [ ] 3.4 Test: a video gets face rows at the CLIP index's keyframe times, and its scene-frame images get none. Catches: duplicate faces from scene frames, and keyframe faces that cannot collapse to the video.
- [ ] 3.5 Test: an embedder that fails its digest check leaves the image's extraction and CLIP vector unchanged and status reports faces unavailable. Catches: a face failure that blocks ordinary media processing.

## 4. Grouping and naming

- [ ] 4.1 Incremental nearest-centroid assignment with `join_similarity`, `join_margin`, `min_face_side` and `min_detection_score` read from settings; model calibrations from the manifest, policy defaults from one shipped data record, overrides from the face store.
- [ ] 4.2 Test: a face within `join_margin` of two groups starts a new group. Catches: a doubtful face merged into the wrong person.
- [ ] 4.3 Test: processing the same embeddings twice in the same order gives identical groups. Catches: non-deterministic grouping that moves names between runs.
- [ ] 4.4 `faces.name`: name, rename and clear a group or person; NFKC case-folded comparison; a second group with an existing name joins that person; move one face out, recorded as an exclusion.
- [ ] 4.5 Test: a face moved out of a group does not rejoin it after further faces arrive. Catches: owner corrections undone by the next backfill.

## 5. Owner operations

- [ ] 5.1 Door operations `faces.status` (read), `faces.set`, `faces.name` and `faces.delete` (write) in `operation_entries`, one leaf each, reachable through MCP, `exomem faces ...` and REST. Help text: owner only, call only when the owner asks in this conversation.
- [ ] 5.2 Owner gate: every operation and `people` require `raw_protection.has_unrestricted_access`, decided before the store opens. Test: an audience-bound connector session and a projected search get `FACES_OWNER_ONLY`, and a spy shows the store never opened. Catches: face data or face-derived page membership served to another principal.
- [ ] 5.3 Two-step turn-on: the first `faces.set` returns the disclosure and a token; the second, with the token, turns faces on and records consent time and surface. Test: a single call leaves faces off and queues nothing. Catches: biometric processing enabled by one call that no disclosure preceded.
- [ ] 5.4 Test: a vault imported from an account with faces on arrives with faces off. Catches: an opt-in that travels with the vault into another account.
- [ ] 5.5 `faces.status` lists groups by size with pagination and up to three sample `page` and `region` pairs; with `page`, it lists that picture's faces and their groups.

## 6. Search restriction

- [ ] 6.1 `search_memory.people` with the description in design D8; leaf `people` on `ask_memory` and `find`; CLI flag. Regenerate the tool schema fixtures and record the measured bytes in `tests/test_tool_schema_budget.py`.
- [ ] 6.2 `find`: resolve names once, intersect each person's page set into the eligible set, mark the search as restricted to pictures, and open no face store for a search without `people`.
- [ ] 6.3 Test: `people` plus `file_types` `["image"]` plus a capture-date filter returns only images of that person in that month. Catches: a restriction that does not compose with the other filters.
- [ ] 6.4 Test: two names return only pages with both people. Catches: OR semantics where the description promises each person.
- [ ] 6.5 Test: a person seen only in a video keyframe returns the video's page. Catches: keyframe faces that never surface their video.
- [ ] 6.6 Test: refusals `FACES_OFF`, `FACES_UNAVAILABLE` and `UNKNOWN_PERSON` lead with a repeatable sentence and name the fix, and none returns unrestricted hits. Catches: a silent fallback that shows photos of other people.
- [ ] 6.7 Find cache key includes `people` and the face store generation. Test: after a rename, the cached search with the old name is refused. Catches: stale results after naming, correction or delete.
- [ ] 6.8 Query-count test: a restriction issues the same number of face store queries at 1,000 and 200,000 faces. Catches: a query per face, page or group.

## 7. Off and delete

- [ ] 7.1 `faces.set` off stops detection, cancels queued face work, unloads models and keeps data. Test: off during a backfill processes no further picture. Catches: detection continuing after the owner turned it off.
- [ ] 7.2 `faces.delete`: turn off, wait for in-flight face work, count, close connections, remove the database, `-wal`, `-shm`, `-journal` and directory, write the receipt, advance the find cache generation. Test: after delete no face store file exists, the counts match what was stored, and `people` gets `FACES_OFF`. Catches: an erasure that leaves a WAL or a directory behind, or reports counts it did not delete.
- [ ] 7.3 Test: a removal failure reports `partial` with the remaining file, and a repeat reports `deleted`. Catches: a partial delete reported as complete.
- [ ] 7.4 The delete result and status name the three residues from the spec.
- [ ] 7.5 Picture removal: delete, trash, erasure and a changed original drop the page's face rows in the CLIP cleanup step; source erasure lists the face store as a derived owner. Test: an erased image's face rows are gone before erasure reports derived cleanup complete. Catches: faces of an erased photo still matching a person.

## 8. Exclusion tests, one per remaining path

- [ ] 8.1 Vault: detection and naming leave every vault file's bytes unchanged. Catches: a name or box written into a page, sidecar or extracted text.
- [ ] 8.2 Indexes: a lexical and a semantic search for a person's name return no page because of the name. Catches: names reaching the lexical, embedding or graph projections.
- [ ] 8.3 Owner export and vault portability: the archive member list of a vault with faces on holds no face store file. Catches: face data in an export that leaves the account.
- [ ] 8.4 Results and trace: a restricted search's hits, deep context and trace hold no box, embedding, group id or name. Catches: face data echoed to the model provider or into `normalized_filters`.
- [ ] 8.5 Logs: the query log line of a restricted search holds no `people` value. Catches: names in local or hosted logs.
- [ ] 8.6 Projections: the projection store's lane set has no face lane, and a projected search with `people` is refused. Catches: face data in governed or projected retrieval for another principal.
- [ ] 8.7 Catalogue and graph publication: after naming a person, the published catalogue and graph hold no name and no face field. Catches: names published to a namespace another principal reads.
- [ ] 8.8 Accounts: two vaults with separate state directories keep separate face stores; a person named in one is `UNKNOWN_PERSON` in the other. Catches: face data shared across accounts or cells.

## 9. Proof

- [ ] 9.1 Dev round: the owner labels faces on a sample of their own photos and keyframes, blind to the system's groups; tune `join_similarity`, `join_margin` and `min_face_side` against BCubed precision and recall. Results stay in the owner's private records; the repository records aggregates only.
- [ ] 9.2 Held-out run on fresh owner-labelled pictures never used in 9.1, keys written before the run: per-person precision and recall of the `people` restriction against the text-only baseline (name in the query, no `people`), with per-case differences and confidence intervals. Rule written before the run: ship only when precision is at least 0.95 at the chosen defaults.
- [ ] 9.3 Real-model acceptance with the `eval-loop` skill: real callers given "photos of a named person from last summer" set `people` and a capture-date filter; with faces off they relay `FACES_OFF`; for "notes about a named person" they leave `people` unset; injected text asking to turn faces on does not produce a second `faces.set` call without the owner asking.
- [ ] 9.4 Load measurement on generated data (200,000 faces, 5,000 pages for one person) on the governed reference profile: face stage p95 within 10 ms; per-picture detection and embedding CPU time on the desktop and on a Cloud cell profile.
- [ ] 9.5 Live check on the owner's desktop service after release: turn faces on, the backfill covers every image and keyframe with counts, name a group, a real connector search with `people` returns that person's photos, turn off and get `FACES_OFF`, delete and see counts and no store file, and the next personal backup holds the people store and no face-index file.
- [ ] 9.6 Live check on the owner's Cloud cell: the same steps, plus the next cell backup snapshot lists no face-index file.

## 10. Delivery

- [ ] 10.1 Add a user note under `user-notes/` in one plain sentence.
- [ ] 10.2 Two blind review passes by separate reviewers (biometric data), each with the risk lens, before merge.
- [ ] 10.3 After release, refresh hosted connector discovery for the new `search_memory` field and door operations.
