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
- Faces stay off per account until its owner opts in explicitly (D9). Biometric data used to identify a person is a special category under GDPR Article 9, which needs explicit consent. A setting in the vault would travel with an import into another account, so the setting lives in the account's state directory.
- Face data never crosses accounts, tenants or principals, and never enters vault pages or sidecars, where it would travel with the vault.
- Erasure reaches every managed copy (D11), because the face store stays out of every backup and export.

No Cloud-specific path is designed. A cell runs the same worker step on its own CPU; the cost is a named risk.

### D2. Models: OpenCV Zoo YuNet and SFace; InsightFace rejected

| Model | Job | Licence of the published files | CPU cost (OpenCV Zoo benchmark, Intel i7-12700K, mean) |
|---|---|---|---|
| YuNet (`face_detection_yunet`) | detection, 5 landmarks | MIT | 0.69 ms at 160 × 120 input |
| SFace (`face_recognition_sface`) | 128-d embedding of an aligned crop | Apache-2.0 | 5.09 ms per face at the benchmark's input |

- YuNet's directory states "All files in this directory are licensed under MIT License"; its LICENSE file is MIT, copyright 2020 Shiqi Yu.
- SFace's directory states "All files in this directory are licensed under Apache 2.0 License"; its LICENSE file is Apache-2.0.
- InsightFace's code is MIT, but its README and model zoo state that all pretrained models, `buffalo_l` included, are "for non-commercial research purposes only", with commercial licensing by contact. Evermatter is a commercial product, so InsightFace weights are out.
- Risk: neither model card names the training data. The SFace paper reports models trained on CASIA-WebFace, VGGFace2 and MS-Celeb-1M. The upstream SFace repository has no LICENSE file; OpenCV Zoo publishes the converted ONNX under Apache-2.0. Whether the training sets' terms reach the weights is unverified and is an operator question.
- Calibration values come from the upstream code, not from this repository: YuNet's demo uses a score threshold of 0.9 and NMS threshold 0.3; SFace's `sface.py` uses a cosine same-identity threshold of 0.363.
- Embedding width (128) and crop size (112 × 112) are expected from SFace; the artifact build records the graph's real input and output shapes, and the manifest carries them.

Per-picture cost estimate, unverified until task 9.4 measures it: one detection pass at the working resolution (about 15 ms on the benchmark CPU when scaled to 640 px), plus about 5 ms per face. At 3,383 visual rows that is a few CPU-minutes; at 100,000 rows about an hour of one core, spread over the backfill.

Both artifacts join `media_models.json` as `face-detect` and `face-embed`, pinned by revision and digest, with licence sources and calibration values, and are acquired through the served-model lifecycle (`media_models.ensure_media_model`). The pinned detector is the dynamic-shape YuNet export, so ONNX Runtime can run it at each picture's size.

### D3. ONNX Runtime and numpy, not OpenCV, at run time

The closest maintained option is `opencv-python-headless`, whose `FaceDetectorYN` and `FaceRecognizerSF` wrap both models. The gap: it adds a large native wheel and a second image-decoding stack that bypasses `image_codecs`' controls, for two calls. ONNX Runtime and Pillow already ship. The added cost is about 150 lines: YuNet's output decode and NMS, a 5-point similarity transform, and a Pillow affine warp. The artifact build compares those outputs with OpenCV's wrappers on fixture images, so OpenCV is a build-time check only. This choice is reversible.

### D4. Faces come from images and from the CLIP keyframes

The face step runs in the media worker right after the CLIP step, under the same identity and staleness checks, for each image and video job. For a video it detects faces at the keyframe timestamps the CLIP index holds; when faces are on, one decode pass serves both steps. Scene-frame images (`video-scene-frames`) are skipped: they are copies of the same video's frames, and their faces would double every count. Each face row records its page, `frame_ts`, box as fractions of the upright picture (the `read_media` `region` form), detection score and embedding.

Turning faces on queues the existing pictures and videos for a bounded, resumable backfill in sorted path order. Status reports processed and total counts; an unknown total is `unknown`, never 0.

### D5. A private face store, out of every backup

The store is one SQLite database in a private directory, `private_state.store(vault_root, "faces")` (owner-only mode 0700), with `PRAGMA secure_delete=ON`. Tables: faces, groups (centroid sum and count), people (display name, folded name), group-to-person links, face exclusions, settings (on/off, consent record, threshold overrides) and a generation counter.

The registry (`reserved_paths.py`) decides what backups take, through `rebuildable`. The face store is not rebuildable: names and corrections exist nowhere else. Declaring it rebuildable would be false. So the registry gains a second closed attribute, "stays out of every backup", and the face store family sets it. The personal backup stages non-rebuildable families that do not set it. The Cloud cell backup (`infra/cellctl` `BACKUP_PATHS`) takes all of `/data/host`, so cellctl adds the face store to its exclusions, and a test pins that mirror to the registry, as it pins the lexical staging exclusions today.

Cost: a restore brings faces back off and without names. Detection recomputes after the owner turns faces on again; the owner names the groups again.

### D6. Grouping: incremental nearest-centroid, people own groups

Options:

1. Assign only to named faces (nearest named face wins). The owner has nothing to name until faces are grouped, so this cannot bootstrap.
2. Batch clustering (for example HDBSCAN or Chinese Whispers) over all faces. Better clusters, but each run costs O(n²) or an ANN index at 200,000 faces, and group ids move between runs, which breaks owner names.
3. Incremental nearest-centroid assignment. Each new face joins the most similar group when the cosine similarity clears `join_similarity` and beats the runner-up by `join_margin`; otherwise it starts a group. Cost per face is one product with the centroid matrix (G × 128). Group ids stay stable.

Choice: 3. A person owns one or more groups, so naming a second group with the same name joins it to the person without merging centroids. A face the owner moves out of a group never rejoins it. The rule prefers a new group over a doubtful join, as speaker attribution prefers anonymity over a wrong name. Batch regrouping is out of scope.

Thresholds (`join_similarity`, `join_margin`, `min_face_side`, `min_detection_score`) are settings. Model calibrations live in the model's manifest entry; the assignment policy defaults (margin, minimum face size) live in one shipped data record; owner overrides live in the face store. A changed threshold applies to faces grouped afterwards, and status says so.

### D7. A name is owner data in the face store

A person name is not a category: no page is classified by it and no code branches on it. So the vocabulary registry rule does not apply, and putting names in a registry or an entity page would move them into the vault. Names are rows in the face store. Two names are the same when their Unicode NFKC case folds are equal. Linking a person to an entity page is out of scope; if it comes later, the link lives in the face store.

### D8. One agent-facing parameter: `search_memory.people`

Options: a filter field, an `options` knob, or a top-level parameter.

- A filter field would be a page field. The filter language can negate it and join it with OR, and governed and projected retrieval compile filters. A face axis there widens the surface that must refuse it.
- `file_types` started in `options` and moved to a field because hosts kept the old place in cached surfaces and agents did not find it (`public_tools._search`). An `options` knob would repeat that.
- A top-level parameter is the one obvious place. Its description: `Show only pictures and videos where each named person's face appears, e.g. ["Fixture Person"].` The schema measures 31,743 of 31,750 B today and `search_memory` 1,236 of 1,250 B, so the PR raises both ceilings by the measured bytes, with the reason in `tests/test_tool_schema_budget.py`.

A list means each named person appears (AND). A group photo is the case only a face restriction can answer; "any of these people" is one search per name. This differs from `tags`, which means any, so the description says "each". It is an operator question.

The server never reads names from the query text. The calling agent maps "photos of Fixture Person" to `people`; a real-model check proves it.

### D9. Owner operations and an explicit two-step opt-in

Four door operations, each with one leaf that MCP, the CLI (`exomem faces ...`) and REST reach:

- `faces.status` (read): on/off, availability, consent record, progress, counts, the last deletion, and groups paginated by size with up to three sample `page` and `region` pairs each. With `page`, it lists the faces on one picture and their groups.
- `faces.set` (write): on, off, and threshold overrides.
- `faces.name` (write): name, rename or clear a group or person; move one face out of its group.
- `faces.delete` (write, destructive): D11.

Every operation and the `people` parameter require `raw_protection.has_unrestricted_access` for the caller, decided before the face store opens. The owner's own connector sessions qualify; an audience, a scoped connector session and projected or governed retrieval for another principal do not.

Turning faces on returns a disclosure the agent shows the owner: what is stored, where, that it never leaves the account, and how to turn it off and delete it. A second call with the disclosure's token turns faces on and records the time and surface. What it prevents: one injected instruction turning on biometric processing with no disclosure ever produced. What it costs when it fires wrongly: one extra call. It cannot prove that a person read the disclosure; the help text tells the agent to call it only when the owner asks in this conversation, as `approve_procedure` does.

### D10. Search integration and cost

`find` resolves `people` once per search. One indexed query maps folded names to person ids; one indexed query returns the distinct pages of those persons' faces. With several names, the page sets intersect. The result joins the eligible set, as relation participants do, and marks the search as restricted to pictures, so the CLIP lane ranks without the chance band. A keyframe face's page is the video's page. A search without `people` never opens the face store.

The find cache key includes `people` and the face store's generation, which every detection, grouping, naming, correction, removal and delete advances. A cached result therefore never outlives a change to faces.

Budget: the face stage stays within 10 ms at p95 with 200,000 faces and 5,000 pages for the named person, measured on generated data on the governed reference profile. It has its own span, so `filter_eligibility` keeps its 6 ms bar for every search.

### D11. Off keeps data; delete erases it

Off stops detection, cancels queued face work, unloads the models and refuses `people` with `FACES_OFF`. Stored data stays, readable only by `faces.status` and `faces.delete`.

Delete turns faces off, waits for in-flight face work under the store lock, counts faces, groups, people and covered pages, closes every connection, and removes the database, its `-wal`, `-shm` and `-journal` files and the directory. A failed removal reports `partial` with what remains; a repeat finishes it. A small receipt (time and counts, no face data) stays in the state directory for status.

What a delete leaves: a volume snapshot that a Cloud cell backup holds while it runs (cellctl deletes it when the backup ends), and freed disk blocks, which the volume's encryption protects until reuse. The result and status state both.

Removing one picture drops its face rows in the derived cleanup that drops its CLIP rows, and source erasure counts the face store among the derived owners that must acknowledge.

### D12. Every path out, and how each is closed

| Path | Today | Closure | Proof |
|---|---|---|---|
| Vault pages, sidecars, extracted text (`extract._maybe_image_tags`, speaker-name writers) | Pages travel with backup, export, sync and connectors | The face step writes only the face store | Vault tree digest unchanged after detect and name |
| Lexical, embedding and CLIP indexes, graph, vocabulary | Derived from vault pages | Names never reach a page | Index search for a name finds nothing |
| Personal backup (`vault_backup.stage_state`) | Stages non-rebuildable families | Face family stays out of backups | Snapshot staging lists no face file |
| Cloud cell backup (`cellctl` `BACKUP_PATHS`) | Whole `/data/host` | Exclusion mirrored from the registry | Pin test; rendered restic command |
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
- Training-data provenance of SFace is undocumented (D2). If the operator rejects it, the detector stays and the embedder needs a replacement with documented data.
- Accuracy across ages, skin tones and lighting is unmeasured for SFace on this corpus. The held-out set should include children and older adults where the owner's photos have them.
- A Cloud cell's CPU limit slows the backfill and competes with other media work. The backfill runs at background priority in bounded batches; no Cloud-specific path is planned.
- Faces of other people appear in the owner's photos. The opt-in is the owner's; the data stays in the owner's account and the delete is complete.
- Sampled keyframes can miss a face that appears briefly. A video then does not match that person.
- A restore loses names (D5).

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
