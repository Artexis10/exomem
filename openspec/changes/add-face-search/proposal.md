## Why

An owner can already find a photo or a video by what it shows: the CLIP lane ranks pictures and video keyframes, and `search_memory.file_types` restricts a search to pictures. The owner cannot find pictures by who is in them. "Photos of a named person" today depends on that person's name appearing in OCR text, tags or a note, which is rarely true for a photo.

Face recognition answers that question. Face data is biometric data, so the capability must be off by default, stay inside the owner's account, and be easy to switch off and erase.

## What Changes

- **Opt-in, per account.** Faces are off for every account. The owner turns them on with the CLI command `exomem faces on`, which shows a disclosure and asks the person to type a confirmation. An agent can show the disclosure but cannot turn faces on. The setting lives in that account's state directory, never in the vault.
- **Detection in pictures and keyframes.** While faces are on, the media pipeline detects faces in each image and in each video keyframe that the CLIP index holds. It embeds each face with a pinned, permissively licensed model that runs on CPU, or on a GPU when the service runs on one: OpenCV Zoo YuNet (MIT) for detection and SFace (Apache-2.0) for embedding, run through ONNX Runtime.
- **Groups and names.** Similar faces form groups incrementally. The owner names a group once (for example "Fixture Person"), can add more groups to the same person, and can move a wrong face out of a group. Grouping thresholds are settings with defaults from data.
- **One search parameter.** `search_memory` takes `people`, a list of named people. A search with it returns only pictures and videos where each named person's face appears. It composes with `file_types`, capture dates, every other filter and the query text. A video matches through its keyframes and returns as the video.
- **Owner operations.** `faces.status`, `faces.set`, `faces.name` and `faces.delete` reach one leaf each through MCP, the CLI and REST. Only the owner with unrestricted content authority may call them or use `people`.
- **Off switch and delete.** Turning faces off stops detection and the person restriction and keeps stored data. `faces.delete` erases every face record, group and name, turns faces off and reports the counts.
- **Boundary.** Face data lives only in a private face store under the account's state directory. It never enters vault pages, sidecars, indexes, the graph, projections or the catalogue. It never crosses to another account, tenant or principal. Backups keep only named people: each name with the centroids of its groups, as they keep named voiceprints today. Faces and unnamed groups stay out of every backup, and exports carry nothing. A delete erases every live copy at once and reports that older backups hold the named people until they expire.
- **Hosted deployments.** On Evermatter Cloud and other hosted deployments, faces are unavailable until the operator enables them for that deployment. Other people appear in an owner's photos, and their consent is not the owner's to give; a legal review precedes enabling faces for accounts other than the operator's.

Default-off and soft-fail: with faces off, no face model loads and no face store exists. A missing runtime or model leaves every other media result unchanged; status reports faces unavailable and a person restriction refuses.

Pure-substrate justification: the detector and the embedder are frozen, deterministic content transducers, like CLIP and the voice embedding that speaker attribution uses. Grouping is a deterministic cosine rule. No generative model runs, and only the owner supplies names.

Scope: self-hosted deployments, and the operator's own Evermatter Cloud cell once the operator enables it (owner ruling, 26 Nov 2026). Out of scope: linking a person to an entity page, face results inside a hit (boxes, timestamps), batch re-clustering, and face search for any principal other than the owner.

## Capabilities

### New Capabilities

- `face-search`: opt-in face detection, grouping, owner naming, the `people` search restriction, the off switch and delete, and the boundary that keeps face data inside the owner's account.

### Modified Capabilities

None. The two face families use the existing rebuildable attribute.

## Impact

- Code: `media_worker.py` and `backfill.py` (face step and backfill), a new face store and grouping module, `find.py` and `find_candidates.py` (eligibility and the pictures-only ranking), `public_tools.py` (`people`), `operation_entries.py` and `commands.py` (face operations), `reserved_paths.py` (two face families), `media_models.json` (two pinned artifacts), `infra/cellctl` (Cloud cell backup exclusion).
- Public surface: one `search_memory` field and four door operations. The tool-schema budget rises by the measured bytes, and hosted connectors need a discovery refresh after release.
- Dependencies: none new at runtime; ONNX Runtime and Pillow already ship in the `embeddings` and `image-codecs` extras.
- Depends on `add-images-only-search` for the pictures-only CLIP ranking that a person restriction reuses.
- Review: face data is biometric, so the change takes two blind review passes.
