## Context

The CLIP index (`clip_index.py`) holds one vector per image and one per sampled video keyframe, keyed by `(file_path, frame_ts)`, in `.clip.sqlite` under `state_paths.vault_state_dir`. The media worker writes it after extraction (`media_worker.py`, CLIP step), and `backfill.py` fills it for existing media. `find` intersects every caller restriction into one eligible page set before the lanes rank; relation participants already join that set the same way. A search restricted to pictures runs the CLIP lane without the chance band (`add-images-only-search`). A keyframe hit collapses to its parent video through `find_types` parent hints.

Load target: today about 2,215 images and 1,168 keyframes on the owner's desktop; the planned scale is 20,000 pages and 100,000 visual rows. At an assumed two faces per visual row, that is up to 200,000 faces. The governed search profile in `src/exomem/request_latency_contract.json` holds the `filter_eligibility` stage to 6 ms at p95; this change adds a separate face stage with its own budget (D10).

Sources consulted, by problem:

- Agent-facing parameter design: Anthropic, "Writing effective tools for agents" (2025).
- Deciding whether the change helps: Anthropic, "Demystifying evals for AI agents" (2026).
- Untrusted content triggering a consequential action (turning biometric processing on): S. Willison, 2025 post on agents that combine private data, untrusted content and outbound actions (sources below).
- Model licences and cost: the primary sources listed under Sources.

## Decisions

### D1. Owner ruling, 26 Nov 2026: cloud processing allowed; per-account opt-in

The first plan kept faces on the desktop owner service. The owner ruled on 26 Nov 2026 that the owner's own Evermatter Cloud cell may detect, store and search faces too. The boundary is therefore the account, not the machine:

- Faces may run wherever the owner's Evermatter runs, desktop or Cloud cell.
- Faces stay off per account until its owner opts in explicitly (D9). A setting in the vault would travel with an import into another account, so the setting lives in the account's state directory.
- The owner's opt-in is the owner's choice to process their own library. It is not consent from the other people in the photos. Biometric data used to identify a person is a special category under GDPR Article 9; Illinois BIPA and Texas CUBI require consent from each person. On a self-hosted deployment the owner runs the processing for personal use. On a hosted deployment Evermatter supplies the means of processing for paying accounts. So a hosted deployment keeps faces unavailable until its operator sets `EXOMEM_FACES_HOSTED=1` (D9a). What the switch prevents: biometric processing of bystanders on other accounts before a legal basis is reviewed. What it costs when it fires wrongly: those accounts wait for the operator, who enables it per cell (D9a).
- Face data never crosses accounts, tenants or principals, and never enters vault pages or sidecars, where it would travel with the vault.
- Erasure reaches every live copy at once (D11). Faces and unnamed groups stay out of every backup and export; backups keep named people (D5).

No other Cloud-specific path is designed. An enabled cell runs the same worker step on its own CPU; the cost is a named risk.

### D2. Models: OpenCV Zoo YuNet and SFace; InsightFace rejected

| Model | Job | Licence of the published files | CPU cost (OpenCV Zoo benchmark, Intel i7-12700K, mean) |
|---|---|---|---|
| YuNet (`face_detection_yunet`) | detection, 5 landmarks | MIT | 0.69 ms at 160 × 120 input |
| SFace (`face_recognition_sface`) | 128-d embedding of an aligned crop | Apache-2.0 | 5.09 ms per face at the benchmark's input |

- YuNet's directory states "All files in this directory are licensed under MIT License"; its LICENSE file is MIT, copyright 2020 Shiqi Yu.
- SFace's directory states "All files in this directory are licensed under Apache 2.0 License"; its LICENSE file is Apache-2.0.
- InsightFace's code is MIT, but its README and model zoo state that all pretrained models, `buffalo_l` included, are "for non-commercial research purposes only", with commercial licensing by contact. Evermatter is a commercial product, so InsightFace weights are out.
- Risk: neither model card names the training data. The SFace paper reports models trained on CASIA-WebFace, VGGFace2 and MS-Celeb-1M. The upstream SFace repository has no LICENSE file; OpenCV Zoo publishes the converted ONNX under Apache-2.0. A survey of recognizers (26 Nov 2026) found no more accurate option whose weights clearly permit commercial use: InsightFace and EdgeFace weights are non-commercial, AdaFace and GhostFaceNets weights carry no explicit licence, dlib's model is public domain but no more accurate and has no ONNX export, and nearly all were trained on scraped celebrity sets. YuNet's training set, WIDER FACE, is listed with non-commercial terms on a secondary source; every candidate detector shares it. The owner chose SFace on 26 Nov 2026.
- Each face row records the embedder's model id. A later switch, for example to a licensed InsightFace model, re-embeds the stored detections into new groups. Names do not survive it: groups belong to one embedder's `model_id`, and an old centroid lies in another vector space. The owner names the new groups again; carrying names across a switch is out of scope.
- Calibration values come from the upstream code, not from this repository: YuNet's demo uses a score threshold of 0.9 and NMS threshold 0.3; SFace's `sface.py` uses a cosine same-identity threshold of 0.363.
- Embedding width (128) and crop size (112 × 112) are expected from SFace; the artifact build records the graph's real input and output shapes, and the manifest carries them.

Per-picture cost estimate, unverified until task 9.4 measures it: one detection pass at the working resolution (about 15 ms on the benchmark CPU when scaled to 640 px), plus about 5 ms per face. At 3,383 visual rows that is a few CPU-minutes; at 100,000 rows about an hour of one core, spread over the backfill.

Both artifacts join `media_models.json` as `face-detect` and `face-embed`, pinned by revision and digest, with licence sources and calibration values, and are acquired through the served-model lifecycle (`media_models.ensure_media_model`). No published YuNet export has dynamic input shapes, so the artifact build makes the 2023mar graph's height and width symbolic with unchanged weights, and ONNX Runtime runs it at each picture's size.

### D3. ONNX Runtime and numpy, not OpenCV, at run time

The closest maintained option is `opencv-python-headless`, whose `FaceDetectorYN` and `FaceRecognizerSF` wrap both models. The gap: it adds a large native wheel and a second image-decoding stack that bypasses `image_codecs`' controls, for two calls. ONNX Runtime and Pillow already ship. The face step takes its execution provider from the service's device policy (`accel.onnx_providers`), the one source other ONNX models use (owner requirement, 26 Nov 2026: CPU and GPU, self-hosted and Cloud). CPU is the default. The GPU provider is used when the service runs on a GPU device (performance mode or `EXOMEM_DEVICE`) and the install carries a GPU build of ONNX Runtime. DirectML has no mapping yet. The added cost is about 150 lines: YuNet's output decode and NMS, a 5-point similarity transform, and a Pillow affine warp. The artifact build compares those outputs with OpenCV's wrappers on fixture images, so OpenCV is a build-time check only. This choice is reversible.

### D4. Faces come from images and from the CLIP keyframes

The face step runs in the media worker inside the CLIP step, for each image and video job. For a video it detects faces at the keyframe timestamps the CLIP index holds; when faces are on, one decode pass serves both steps. The detector sees the picture at its working size, and the embedder crops each face from the picture's own pixels. A face more than twice the crop's size is first reduced to twice that size with an area filter, so the bilinear warp reads every pixel and fine detail cannot alias. The OpenCV parity check covers the plain crop; task 9.1's real-model round measures large faces. Both models take their turn at the process-wide model gate (`runtime_resources.model_execution`), as every other model call does. The CLIP rows commit under the projection guard. The faces commit after that guard is released, so a foreground write never waits for grouping: a second identity check runs first, and the rows carry the modification time of the bytes scanned. A picture that changed since has its own CLIP job, which scans it again. The face step checks that faces are on before it loads the models and again after, and it never creates the face store, so a picture in flight cannot undo an off or a delete. Each process keeps its connections to the two stores open between writes; off and delete close them and drop the centroids held in memory. Scene-frame images (`video-scene-frames`) are skipped: they are copies of the same video's frames, and their faces would double every count. Each face row records its page, `frame_ts`, box as fractions of the upright picture (the `read_media` `region` form), detection score and embedding.

Turning faces on starts a bounded, resumable backfill of the existing pictures and videos in sorted path order. It runs inside the `exomem faces on` process. If that process ends early (Ctrl-C, a closed terminal), the service resumes the backfill at its next start, which can be days later. Status reports processed and total counts meanwhile; an unknown total is `unknown`, never 0.

### D5. Two private stores: backups keep named people only

Both stores live in a private directory, `private_state.store(vault_root, "faces")` (owner-only mode 0700), as SQLite databases with `PRAGMA secure_delete=ON`.

- The face index holds what a re-scan recomputes, plus the account's switch: faces (page, `frame_ts`, box, score, model id, vector, group id), unnamed groups (centroid sum and count), scanned pages, settings (on/off and the consent record) and a generation counter. Its registry family declares itself rebuildable, so backups leave it out, as they leave out the CLIP index.
- The people store holds only what the owner created: people (display name, folded name), the groups linked to a person (centroid sum and count), face exclusions and threshold overrides. Naming a group moves it from the face index to the people store; clearing a group's name moves it back. Named voiceprints (`.voice_profiles.json`) are the precedent: owner-named biometric centroids, declared not rebuildable, kept by backups.

The owner chose on 26 Nov 2026 that backups keep named people. The people store declares itself not rebuildable, so the personal backup stages it and a restore keeps names. A one-face unnamed group's centroid is that face's embedding, so keeping unnamed groups out of the people store keeps strangers' templates out of backups. Because on/off and consent live in the face index, a restore leaves faces off until the owner opts in again. After the re-scan, new faces join the nearest named centroid (D6).

The Cloud cell backup (`infra/cellctl` `BACKUP_PATHS`) takes all of `/data/host`. cellctl excludes the face index, and a test pins that exclusion to the registry's face-index family. Other rebuildable families stay in cell snapshots; that is old behaviour this change does not alter.

Cost: a delete erases every live copy at once, but older backups keep the people store until the deployment's backup retention expires them. The delete result and status state that. The Cloud cell's backup copies the people store's files as they are, its write-ahead log included, so a write that removes a name or a removed picture's data checkpoints the people store to empty that log. The checkpoint is best effort: while a reader holds the log it cannot finish, the store logs that, and the next write that removes data tries again.

### D6. Grouping: incremental nearest-centroid, people own groups

Options:

1. Assign only to named faces (nearest named face wins). The owner has nothing to name until faces are grouped, so this cannot bootstrap.
2. Batch clustering (for example HDBSCAN or Chinese Whispers) over all faces. Better clusters, but each run costs O(n²) or an ANN index at 200,000 faces, and group ids move between runs, which breaks owner names.
3. Incremental nearest-centroid assignment. Each new face joins the most similar group when the cosine similarity clears `join_similarity` and beats the runner-up by `join_margin`; otherwise it starts a group. Cost per face is one product with the centroid matrix (G × 128) and a pass for the best two. Group ids stay stable. A new group's id is random below 2^53, the largest integer a JSON client keeps exact, and no group or face of either store holds it: one past the largest id would reuse a named group's id after a delete and a restore. A named group's count and centroid are taken from the faces the face index holds whenever grouping loads them, so a re-scan after a restore counts each face once. A named group whose faces the face index lost keeps its stored centroid with a count of 0: that centroid lets a restored name attach to the person's faces when they are scanned again.

Choice: 3. The default `join_similarity` starts from SFace's pairwise same-identity threshold (0.363), but D6 compares a face with a group centroid, a different quantity, so task 9.1 tunes it. A person owns one or more groups, so naming a second group with the same name joins it to the person without merging centroids. A face the owner moves out of a group never rejoins it. The rule prefers a new group over a doubtful join, as speaker attribution prefers anonymity over a wrong name. Batch regrouping is out of scope. A wrong merge is corrected by clearing the group's name and moving the wrong faces out; a large wrong merge means deleting faces and re-scanning with tighter thresholds. The precision floor in task 9.2 exists to keep that rare.

Thresholds (`join_similarity`, `join_margin`, `min_face_side`, `min_detection_score`) are settings. Model calibrations live in the model's manifest entry; the assignment policy defaults (margin, minimum face size) and every setting's allowed range live in one shipped data record; owner overrides live in the face store, and a value outside its range is refused. A changed threshold applies to faces grouped afterwards, and status says so.

### D7. A name is owner data in the face store

A person name is not a category: no page is classified by it and no code branches on it. So the vocabulary registry rule does not apply, and putting names in a registry or an entity page would move them into the vault. Names are rows in the face store. Two names are the same when their Unicode NFKC case folds are equal. Linking a person to an entity page is out of scope; if it comes later, the link lives in the face store.

### D8. One agent-facing parameter: `search_memory.people`

Options: a filter field, an `options` knob, or a top-level parameter.

- A filter field would be a page field. The filter language can negate it and join it with OR, and governed and projected retrieval compile filters. A face axis there widens the surface that must refuse it.
- `file_types` started in `options` and moved to a field because hosts kept the old place in cached surfaces and agents did not find it (`public_tools._search`). An `options` knob would repeat that.
- A top-level parameter is the one obvious place. Its description: `Show only pictures and videos where each named person's face appears, e.g. ["Fixture Person"].` The schema measures 31,743 of 31,750 B today and `search_memory` 1,236 of 1,250 B, so the PR raises both ceilings by the measured bytes, with the reason in `tests/test_tool_schema_budget.py`.

A list means each named person appears (AND). A group photo is the case only a face restriction can answer; "any of these people" is one search per name. This differs from `tags`, which means any, so the description says "each".

The server never reads names from the query text. The calling agent maps "photos of Fixture Person" to `people`; a real-model check proves it.

### D9. Owner operations and an explicit two-step opt-in

Four door operations, each with one leaf that MCP, the CLI (`exomem faces ...`) and REST reach:

- `faces.status` (read): on/off, availability, consent record, progress, counts, the last deletion, and groups paginated by size with up to three sample `page` and `region` pairs each. With `page`, it lists the faces on one picture and their groups.
- `faces.set` (write): on, off, and threshold overrides.
- `faces.name` (write): name, rename or clear a group or person; move one face out of its group.
- `faces.delete` (write, destructive): D11.

Every operation and the `people` parameter require `raw_protection.has_unrestricted_access` for the caller, decided before the face store opens. The owner's own connector sessions qualify; an audience, a scoped connector session and projected or governed retrieval for another principal do not.

Turning faces on happens only in the CLI, `exomem faces on`, run by the person. It prints the disclosure (what is stored, where, that names and named groups' centroids go into the account's backups and outlive a delete until retention expires them, how to turn it off and delete it) and requires a confirmation typed at a terminal, then records the time and surface `cli`. No flag takes the confirmation, and the command refuses without a terminal, so an agent's one-line shell call leaves faces off. An agent that drives a pseudo-terminal could still type it; that remaining risk rests on the host's command approval. `faces.set` with on, from any agent-reachable surface, returns the same disclosure and that instruction and changes nothing. What it prevents: an injected instruction turning on biometric processing, which an agent-completable two-call token would not stop. What it costs: one command for the owner. Off, naming, status and delete stay agent-reachable owner operations.

### D9a. Hosted deployments need the operator's switch

One function decides availability, and status, the CLI turn-on, the worker step and the `people` restriction all call it. On a Cloud or hosted deployment (`cloud_cell.cloud_mode_enabled()` or `hosted_mode_enabled()`, the check `capture_backfill` already uses), faces are unavailable with `FACES_UNAVAILABLE` unless the deployment sets `EXOMEM_FACES_HOSTED=1`. On Evermatter Cloud, cellctl renders the setting only into the cells listed in the platform chart's `cellctl.facesHostedCellIds`; a cell's digest changes only when its own entry changes. `model_env`, which reaches every cell, refuses the setting. The operator lists the operator's own cell. Listing any other cell waits for a legal review (D1). Listing a cell does not yet run faces there: the cell image lacks the face models and the cell's CLIP step is off, until task 10.5.

### D10. Search integration and cost

`find` resolves `people` once per search. One indexed query maps folded names to person ids; one indexed query returns the distinct pages of those persons' faces. With several names, the page sets intersect. The result joins the eligible set, as relation participants do, and marks the search as restricted to pictures, so the CLIP lane ranks without the chance band. A keyframe face's page is the video's page. A search without `people` never opens the face store.

The find cache key includes `people` and the face store's generation, which every detection, grouping, naming, correction, removal and delete advances. A cached result therefore never outlives a change to faces.

Budget: the face stage stays within 10 ms at p95 with 200,000 faces and 5,000 pages for the named person, measured on generated data on the governed reference profile. It has its own span, so `filter_eligibility` keeps its 6 ms bar for every search.

### D11. Off keeps data; delete erases it

Off stops detection, cancels queued face work, unloads the models and refuses `people` with `FACES_OFF`. Stored data stays, readable only by `faces.status` and `faces.delete`.

Delete turns faces off, waits for in-flight face work under the store lock, counts faces, groups, people and covered pages, closes every connection, and removes both databases, their `-wal`, `-shm` and `-journal` files and the directory. A failed removal reports `partial` with what remains; a repeat finishes it. A small receipt (time and counts, no face data) stays in the state directory for status.

What a delete leaves depends on the deployment: older backups of the people store under D5 until that deployment's retention expires them (the owner's keep settings, the operator's retention, or the Cloud cell's backup schedule), on a Cloud cell a volume snapshot that a running backup holds (cellctl deletes it when the backup ends), and freed disk blocks until the filesystem reuses them. The result and status name the deployment's residues without repeating retention counts that another component owns.

Removing one picture drops its face rows in the derived cleanup that drops its CLIP rows, and source erasure counts the face store among the derived owners that must acknowledge.

### D12. Every path out, and how each is closed

| Path | Today | Closure | Proof |
|---|---|---|---|
| Vault pages, sidecars, extracted text (`extract._maybe_image_tags`, speaker-name writers) | Pages travel with backup, export, sync and connectors | The face step writes only the face store | Vault tree digest unchanged after detect and name |
| Lexical, embedding and CLIP indexes, graph, vocabulary | Derived from vault pages | Names never reach a page | Index search for a name finds nothing |
| Personal backup (`vault_backup.stage_state`) | Stages non-rebuildable families | Face index is rebuildable, so it stays out; the people store follows D5 | Snapshot staging lists no face-index file |
| Cloud cell backup (`cellctl` `BACKUP_PATHS`) | Whole `/data/host` | Face-index exclusion mirrored from the registry | Pin test; rendered restic command |
| Owner export, vault portability, Cloud export (`vault_portability.iter_archive_members`) | State dir yields `PORTABLE_DERIVED` only | Face family is not portable | Archive members list no face file |
| Cloud import (`cloud_import_client`) | Walks an owner folder; state root lies outside the vault | No change | Covered by the portability test |
| Projected retrieval and projection store | Closed lane set, CLIP samples projected | No face lane; `people` refused | Refusal before store open |
| Governance egress and audience connectors (`egress._postfilter`) | Per-page release for non-owners | `people` and face operations owner-only | Refusal before store open |
| Catalogue and graph publication | Local namespaces | Never read the face store | Published output holds no name |
| Another account, tenant or cell | Each cell has its own state root | One face store per state directory | A name from one store is unknown in another |
| Search results, deep context, explain trace (`retrieval_explain` `normalized_filters`) | Echo filter values | `people` is not a filter; results carry no face fields | Result and trace carry no face field |
| Hooks (`exomem_retrieve_nudge`), served context | Forward search stubs to the model provider | Never pass `people`; results carry no face fields | Covered by the result test |
| Query log, call ledger, hosted logs | Query text, argument hashes; hosted logs exclude raw arguments | `people` values never logged | Query log line has no name |
| Node media-slot lease | Cell id, engine names, bytes | Face step takes no new engine name | No change on the wire |
| Model-provider egress (`context_fit`) | Released vault prose | Reads no state directory | No change |

## Risks

- A false match shows the owner a picture of someone else under a name. The margin rule, owner corrections and a precision floor in the held-out proof limit it.
- Training-data provenance of SFace is undocumented, and YuNet's training set carries non-commercial terms on a secondary source (D2). The owner accepted SFace; a switch is a re-embed.
- CPU and GPU providers give slightly different floats, so a face near a threshold can group differently on another provider. Determinism holds per provider.
- Accuracy across ages, skin tones and lighting is unmeasured for SFace on this corpus. The held-out set should include children and older adults where the owner's photos have them.
- A Cloud cell's CPU limit slows the backfill and competes with other media work. The backfill runs at background priority in bounded batches; no Cloud-specific path is planned.
- Faces of other people appear in the owner's photos, and the owner's opt-in is not their consent (D1). Hosted deployments stay off until the operator enables them after a legal review; the data stays in the owner's account; a delete reaches older backups only as they expire (D11).
- Sampled keyframes can miss a face that appears briefly. A video then does not match that person.
- Older backups keep names and centroids until they expire (D5).

## Sources

- YuNet model card and licence: https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet , https://github.com/opencv/opencv_zoo/blob/main/models/face_detection_yunet/LICENSE , demo defaults: https://github.com/opencv/opencv_zoo/blob/main/models/face_detection_yunet/demo.py
- SFace model card and licence: https://github.com/opencv/opencv_zoo/tree/main/models/face_recognition_sface , https://github.com/opencv/opencv_zoo/blob/main/models/face_recognition_sface/LICENSE , thresholds: https://github.com/opencv/opencv_zoo/blob/main/models/face_recognition_sface/sface.py , Hugging Face mirror: https://huggingface.co/opencv/face_recognition_sface
- CPU benchmark: https://github.com/opencv/opencv_zoo/blob/main/benchmark/README.md
- SFace paper (training sets): https://arxiv.org/abs/2205.12010 ; upstream repository: https://github.com/zhongyy/SFace
- YuNet paper: W. Wu, H. Peng, S. Yu, "YuNet: A Tiny Millisecond-level Face Detector", Machine Intelligence Research 20(5), 2023.
- InsightFace licence statements: https://github.com/deepinsight/insightface/blob/master/README.md , https://github.com/deepinsight/insightface/blob/master/model_zoo/README.md
- GDPR Articles 4(14), 9 and 17: https://eur-lex.europa.eu/eli/reg/2016/679/oj
- Anthropic, "Writing effective tools for agents": https://www.anthropic.com/engineering/writing-tools-for-agents
- Anthropic, "Demystifying evals for AI agents": https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents
- S. Willison, 2025 post on untrusted content and agent actions: https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/
