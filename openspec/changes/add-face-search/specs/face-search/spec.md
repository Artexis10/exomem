## ADDED Requirements

### Requirement: Faces are off until the account owner turns them on

Face detection, grouping and the person restriction SHALL be off for every account until its owner turns them on. The setting SHALL live in the account's face store under its state directory, never in the vault, so importing, restoring or copying a vault never turns faces on. Only the CLI command `exomem faces on`, run by a person, SHALL turn faces on, after it shows the disclosure and the person types a confirmation.

#### Scenario: A new account

- **WHEN** an account has never turned faces on
- **THEN** `faces.status` reports faces off
- **AND** no face model is loaded and no face store exists

#### Scenario: A vault arrives from an account with faces on

- **WHEN** a vault whose source account had faces on is imported or restored into another account
- **THEN** faces are off in the receiving account

#### Scenario: An agent asks to turn faces on

- **WHEN** an agent calls `faces.set` to turn faces on through MCP or REST
- **THEN** the result carries the disclosure and the CLI command to run
- **AND** faces stay off and no picture is queued

#### Scenario: The owner turns faces on in the CLI

- **WHEN** the owner runs `exomem faces on` and types the confirmation
- **THEN** faces are on, the consent time and surface `cli` are recorded, and the command scans the existing pictures
- **AND** if the command ends before the scan finishes, the service resumes the scan at its next start

#### Scenario: No terminal

- **WHEN** `exomem faces on` runs without a terminal, as in an agent's shell call
- **THEN** it refuses with `CONFIRMATION_REQUIRED` and faces stay off

### Requirement: Hosted deployments need the operator's switch

On a Cloud or hosted deployment, faces SHALL be unavailable for every account unless the deployment setting `EXOMEM_FACES_HOSTED` is `1`. One function SHALL decide availability for status, turn-on, detection and the person restriction. A self-hosted deployment SHALL NOT need the setting. On Evermatter Cloud, cellctl SHALL render it only into the cells the operator lists, never cluster-wide.

#### Scenario: A hosted deployment without the switch

- **WHEN** an owner on a hosted deployment without the setting runs `exomem faces on` or searches with `people`
- **THEN** both are refused with `FACES_UNAVAILABLE`, which says the operator has not enabled faces on this deployment

#### Scenario: The operator lists one Cloud cell

- **WHEN** the operator lists one cell for faces
- **THEN** only that cell receives the setting and restarts, and every other cell keeps faces unavailable
- **AND** a cluster-wide model environment entry for the setting is refused

### Requirement: Only the owner reaches face data

Every face operation and the `people` restriction SHALL serve only the owner with unrestricted content authority. Any other caller, including an audience, a scoped or shared connector session, and projected or governed retrieval for another principal, SHALL be refused with `FACES_OWNER_ONLY`. The refusal SHALL be decided from the principal before the face store is opened.

#### Scenario: An audience asks for a person

- **WHEN** a connector session bound to an audience calls `search_memory` with `people`
- **THEN** the call is refused with `FACES_OWNER_ONLY`
- **AND** the face store is not opened

#### Scenario: Projected retrieval meets a person restriction

- **WHEN** a projected search carries `people`
- **THEN** it is refused with `FACES_OWNER_ONLY` and no projection lane reads face data

#### Scenario: The owner's own connector

- **WHEN** the owner's connector session with unrestricted content authority calls `faces.status`
- **THEN** the status is served

### Requirement: Faces are detected in pictures and video keyframes

While faces are on, the media pipeline SHALL detect faces in each image and in each video keyframe that the CLIP index holds, and SHALL embed each face with the pinned embedder. Each face row SHALL record its page, frame time, box as fractions of the upright picture, detection score and embedding. Scene-frame images SHALL be skipped. Turning faces on SHALL queue existing pictures and videos in bounded, resumable batches.

#### Scenario: A photo with two people

- **WHEN** an image with two faces above the minimum size and score is processed
- **THEN** the face store holds two face rows for that image's page

#### Scenario: A video's keyframes

- **WHEN** a video whose keyframes the CLIP index holds is processed
- **THEN** each detected face row carries the video's page and its keyframe's time

#### Scenario: A face below the minimum size

- **WHEN** a detected face is smaller than the `min_face_side` setting
- **THEN** no face row is written for it

#### Scenario: Backfill progress

- **WHEN** the owner reads `faces.status` during the backfill
- **THEN** it reports processed and total counts, and `unknown` when the total is not yet known

### Requirement: Face models are pinned, permissively licensed and follow the device policy

The detector and the embedder SHALL be pinned artifacts in the media model manifest, each with revision, digest, licence source, calibration values and input and output shapes. Each SHALL carry a licence that permits commercial use of its weights. Both SHALL run through ONNX Runtime with the provider the service's device policy selects, CPU by default, and SHALL load only while faces are on. A host SHALL acquire them through the served-model lifecycle and never build them.

#### Scenario: A manifest entry without a licence source

- **WHEN** a face model entry in the manifest names no licence source
- **THEN** the manifest check fails

#### Scenario: Faces are off

- **WHEN** faces are off and media is processed
- **THEN** no face model is acquired or loaded

### Requirement: Face processing is deterministic and soft-fails

Detection, embedding and grouping SHALL be deterministic measurements with no generative model. A face failure SHALL be logged and SHALL leave the picture's extraction, OCR and CLIP results unchanged. A missing runtime or model SHALL make `faces.status` report faces unavailable, SHALL refuse a person restriction with `FACES_UNAVAILABLE`, and SHALL let every other media step finish.

#### Scenario: The embedder cannot load

- **WHEN** faces are on and the embedder artifact fails its digest check
- **THEN** the picture's extraction and CLIP vector are written as without faces
- **AND** `faces.status` reports faces unavailable with the reason

#### Scenario: Same input, same groups

- **WHEN** the same pictures are processed twice in the same order from an empty store on the same execution provider
- **THEN** the face rows and groups are identical

### Requirement: Similar faces group incrementally with thresholds from settings

A new face SHALL join the group whose centroid is most similar when that cosine similarity clears `join_similarity` and beats the next group by `join_margin`; otherwise it SHALL start a new group. `join_similarity`, `join_margin`, `min_face_side` and `min_detection_score` SHALL be settings whose defaults come from data. A changed setting SHALL apply to faces grouped afterwards, and status SHALL say so.

#### Scenario: The same person in two photos

- **WHEN** two faces of one person are processed and their similarity clears both thresholds
- **THEN** both faces are in one group

#### Scenario: A face close to two groups

- **WHEN** a face clears `join_similarity` for two groups but the difference is below `join_margin`
- **THEN** the face starts a new group

#### Scenario: The owner raises a threshold

- **WHEN** the owner sets a new `join_similarity` through `faces.set`
- **THEN** faces processed afterwards use it, existing groups stay as they are, and status says so

### Requirement: The owner names and corrects groups

The owner SHALL name a group with `faces.name`. Names SHALL compare by Unicode NFKC case folding. Naming a group with an existing person's name SHALL add the group to that person. The owner SHALL rename or clear a person and SHALL move one face out of its group; a moved face SHALL never rejoin that group. `faces.status` SHALL list groups by size, paginated, with up to three sample pages and regions each.

#### Scenario: Two groups of one person

- **WHEN** the owner names one group "Fixture Person" and another "fixture person"
- **THEN** both groups belong to one person

#### Scenario: A wrong face is moved out

- **WHEN** the owner moves a face out of a named group and later pictures are processed
- **THEN** that face is not in that group again

#### Scenario: The owner finds a group to name

- **WHEN** the owner reads `faces.status`
- **THEN** each listed group carries its face count, its person or none, and sample `page` and `region` pairs that `read_media` can show

### Requirement: Search restricts to pictures of named people

`search_memory` SHALL take an optional `people` list. A search with it SHALL return only pictures and videos where a face of each named person appears, combined with every other filter, `file_types`, capture dates and the query text. A video SHALL match through its keyframes and return as the video. The CLIP lane SHALL rank as in a search restricted to pictures. The server SHALL NOT read person names from the query text.

#### Scenario: Photos of one person

- **WHEN** the owner searches with `people` set to `["Fixture Person"]` and `file_types` set to `["image"]`
- **THEN** every hit is an image with a face in a group of that person

#### Scenario: A person and a capture date

- **WHEN** the search also filters capture dates to one month
- **THEN** every hit shows the person and was captured in that month

#### Scenario: Two people together

- **WHEN** `people` names two persons
- **THEN** every hit shows a face of each of them

#### Scenario: A person in a video

- **WHEN** a person's face appears only in a video's keyframe
- **THEN** the hit is the video's page, not a keyframe

#### Scenario: A name only in the query text

- **WHEN** the query text is "photos of Fixture Person" and `people` is not set
- **THEN** the search applies no face restriction

### Requirement: A person restriction refuses what it cannot answer

A person restriction SHALL refuse with `FACES_OFF` while faces are off, `FACES_UNAVAILABLE` when the models cannot run, and `UNKNOWN_PERSON` for a name that no person has, listing up to 20 named people. Each refusal SHALL lead with a sentence the agent can repeat to the owner and SHALL name what to change. A person restriction SHALL never fall back to a search without it.

#### Scenario: Faces are off

- **WHEN** the owner searches with `people` while faces are off
- **THEN** the call is refused with `FACES_OFF` and the outcome names `faces.set`

#### Scenario: An unknown name

- **WHEN** `people` names no existing person
- **THEN** the call is refused with `UNKNOWN_PERSON` and lists the named people

### Requirement: A person restriction costs one bounded lookup

A person restriction SHALL resolve with one indexed name lookup and one indexed page lookup per search, never a query per face, page or group. A search without `people` SHALL NOT open the face store. With 200,000 faces and 5,000 pages for the named person, the face stage SHALL stay within 10 ms at p95 on the governed reference profile. A cached result SHALL NOT outlive a change to faces, groups or names.

#### Scenario: Query count does not grow with the store

- **WHEN** a person restriction runs against stores of 1,000 and 200,000 faces
- **THEN** both searches issue the same number of face store queries

#### Scenario: A rename reaches a cached search

- **WHEN** the owner renames a person after a search with `people` was cached
- **THEN** the same search with the old name is refused with `UNKNOWN_PERSON`

### Requirement: Results and logs carry no face data

Search results, deep context, explain traces, query logs and hosted logs SHALL carry no face box, embedding, group id, face count or person name. The trace SHALL report the face stage only by status and eligible page count. Only `faces.status` and `faces.delete` SHALL return face data, and only to the owner.

#### Scenario: A restricted search's result and trace

- **WHEN** the owner searches with `people` and asks for a trace
- **THEN** no hit and no trace field holds a box, embedding, group id or name

#### Scenario: The query log

- **WHEN** a search with `people` is logged
- **THEN** the query log line holds no `people` value

### Requirement: Face data stays inside the owner's account

Face rows, groups, names, settings and the consent record SHALL live only in the account's face store, a private directory under its state directory. It holds a face index (faces, unnamed groups, settings, consent) and a people store (people, named groups with their centroids). They SHALL NOT be written to a vault page, sidecar, extracted text, index, graph, vocabulary registry, projection or catalogue. They SHALL NOT reach another account, tenant, cell or principal.

#### Scenario: Detection and naming leave the vault unchanged

- **WHEN** faces are detected in a vault's pictures and the owner names a group
- **THEN** every vault file has the bytes it had before
- **AND** a lexical or semantic search for the name returns no page because of the name

#### Scenario: A Cloud cell's store

- **WHEN** faces are on in a Cloud cell
- **THEN** the face store sits under that cell's own state directory and no other cell can open it

### Requirement: Backups keep only named people, and exports carry nothing

The face index, which holds faces and unnamed groups, SHALL be rebuildable, so no backup SHALL carry it; the Cloud cell backup SHALL exclude it, pinned by a test. The people store SHALL hold only people and their named groups, SHALL be not rebuildable, and every backup SHALL keep it. The owner export, vault portability archives and Cloud import SHALL carry neither.

#### Scenario: A personal backup

- **WHEN** a personal backup runs on a vault with faces on and named groups
- **THEN** the snapshot holds the people store and no face index file
- **AND** no unnamed group's centroid is in the snapshot

#### Scenario: A Cloud cell backup

- **WHEN** cellctl renders a cell's backup Job
- **THEN** its restic command excludes every face index file

#### Scenario: A restore

- **WHEN** the owner restores a backup and turns faces on again
- **THEN** faces stay off until that second opt-in, the re-scan rebuilds the face index, and new faces join the restored named groups

#### Scenario: An owner export

- **WHEN** the owner exports a vault with faces on
- **THEN** the archive holds no face store file

### Requirement: Turning faces off stops detection and search

Turning faces off SHALL stop new detection, cancel queued face work, unload the face models and refuse the person restriction with `FACES_OFF`. It SHALL keep the stored faces, groups and names, which only `faces.status` and `faces.delete` may read, until the owner deletes them or turns faces on again.

#### Scenario: Off during a backfill

- **WHEN** the owner turns faces off while the backfill runs
- **THEN** no further picture is processed for faces and status reports the backfill stopped

#### Scenario: On again

- **WHEN** the owner turns faces on again
- **THEN** the stored groups and names are used and only pictures without face rows are queued

### Requirement: Delete erases every face record and reports counts

`faces.delete` SHALL turn faces off, stop face work, count faces, groups, named people and covered pages, then remove the face store's database, its write-ahead log, shared-memory and journal files, and its directory. It SHALL report the counts. A failed removal SHALL report `partial` and name what remains, and a repeated delete SHALL finish it. Status SHALL then report faces off with the last deletion's time and counts.

#### Scenario: A complete delete

- **WHEN** the owner calls `faces.delete` on a store with faces and names
- **THEN** the result reports each count and the outcome `deleted`
- **AND** no face store file remains and a search with `people` is refused with `FACES_OFF`

#### Scenario: A repeat after a partial delete

- **WHEN** a delete reported `partial` and the owner repeats it
- **THEN** the remaining files are removed and the outcome is `deleted`

### Requirement: A delete leaves no managed copy

After a delete, no face data SHALL remain in any live store or export that Evermatter manages. Three residues MAY remain, and the delete result and status SHALL name each and when it ends: older backups that hold the people store, until their retention expires them; a volume snapshot that a running Cloud cell backup holds, until that backup ends; and freed disk blocks, which the volume's encryption protects until the filesystem reuses them.

#### Scenario: The delete result names the residues

- **WHEN** a delete completes
- **THEN** the result states the three residues and when each ends

### Requirement: Removing a picture removes its faces

When a picture or video page is deleted, trashed or erased, or its original changes, the face store SHALL drop that page's face rows in the derived cleanup that drops its CLIP rows, with SQLite secure delete on, and SHALL update the affected group centroids. Source erasure SHALL count the face store among the derived owners that acknowledge cleanup.

#### Scenario: A photo is erased

- **WHEN** an image Source with face rows is erased
- **THEN** its face rows are gone before the erasure reports the derived cleanup complete
- **AND** a search with `people` no longer returns it
