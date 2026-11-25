## Context

An owner can move an existing Evermatter vault to Cloud in two ways.

- **Adopt.** `exomem cloud import <folder>` plans every folder as an `adopt` import (`cloud_import_client._adopt_plan`). The cell unpacks it under `_Imports/<import_id>/`, and Adoption Studio files it. An `.md` file is a text item, and every other file is an artifact (`adoption_run.walk_inventory`). Adoption gives a recording a new Source page with a pending extraction, and asks for extraction after the commit (`adoption_run._request_extraction`). The old sidecar beside the recording becomes a text Source that names no original. The upload path cannot help either: it refuses transcriptions with an adoption (`client_artifacts.py`, `_refuse_transcriptions`).
- **Restore.** A whole-vault restore renames the staged vault into place byte-identical (`cloud_import_job._restore`). Reconciliation finds each completed sidecar and admits no speech job (`media_processing.reconcile_media`, `_is_completed_sidecar_shape`).

A restore must fit the cell's volume. Only adopt imports can arrive in windows (`add-cloud-media-store`, task 5.4), so a vault larger than the volume must use adopt.

What the sidecar holds today:
- The page beside an original `P` is `P.md` (`preserve._sidecar_path`).
- A completed audio or video page has `type: source`, the `media_type`, no pending or failed state, an `extracted_by` engine and text under `## Extracted text`.
- It records the original's provenance: `evidence_file`, `original_filename`, `binary_sha256`, `binary_size` and two timestamps. `evidence_file`, `binary_sha256` and `binary_size` are its identity fields (`media_processing._IDENTITY_FIELDS`). A difference in a timestamp or the file name is repairable.
- The completed-shape check also needs `evidence_file` to equal the original's vault path.
- The speech fields are `speakers` (the distinct labels), `speaker_verification`, `diarize_requested` and the `asr_` facts (`preserve._EXTRACTION_FIELDS`). The transcript keeps its turns only as timed lines, `[m:ss] [Speaker A]: text`. `semantic_segments.parse_timed_lines` reads them, and `render_timed_lines` writes them.
- A long extraction keeps its first part on the page and the rest in a dot folder beside it: `.X.parts/part-02.md` for the page `X.md` (`extraction_parts`). The page lists `extraction_parts` and `next_part`.

What the cell can run:
- 2 CPUs and 3 GiB per cell (`add-cloud-multimodal-processing` design).
- No PyTorch: the image build asserts that `torch` is absent (`Dockerfile`).
- Only CPU devices: Cloud configuration refuses any other value for each device variable, including the diarisation and voice devices (`cloud_cell.py`).
- A provisional speech budget of 1024 MiB anonymous memory (`docs/runbooks/cloud-media.md`).
- Every engine switch is off by default. The operator selects engines per cell through `cellctl.mediaEngineCellIds` (`add-cloud-multimodal-processing` D7).

How speech results reach a Source today:
- Agents call media operations through the write door, such as `manage_memory(operation="media.reprocess", ...)`. The `process_media` group holds them, with the `change_media` write class (`operation_entries`).
- The speech pipeline assigns each ASR segment to the diarisation turn that overlaps it most (`speaker_assignment.assign_span`), names labels in order of first appearance, and renders timed lines (`extract._diarize`, `semantic_segments.render_timed_lines`).
- The media worker publishes a result only while the original's identity and the sidecar's content hash still match. It splits a long result into part pages, and keeps a replaced extraction on a linked prior page (`media_worker`, `prior_extraction`).
- An owner reads a Cloud original on their own machine with `exomem cloud download`.

Measured rates. These were measured outside this repository, and this change does not re-measure them:
- Cell Whisper: about 0.43 times real time.
- pyannote on CPU: RTF 1.13 to 1.26, with a 2.9 GiB peak.
- pyannote on a GPU: RTF 0.015.

## Goals / Non-Goals

**Goals:**
- A vault that moves to Cloud by adoption keeps its transcripts, timed text, speaker labels and part pages, and the cell does not transcribe them again.
- New audio is transcribed in the cell, with no new mechanism.
- The owner's agent can attach speech results that the owner's machine computed, so Cloud recordings get speaker labels.
- Multitrack recordings and naming after the fact work wherever speech runs.

**Non-Goals:**
- Diarisation on a cell (D6).
- A speech worker, service, daemon, route or credential on the owner's machine or in the platform.
- A platform GPU job. D6 records it as a priced later alternative.
- Kept results for other media: OCR, documents and a video's scene-frame images keep today's behaviour.

## Terms

- **Sidecar-named page:** a staged page whose name is an original's name plus `.md`, in the same folder.
- **Kept extraction:** a completed audio or video extraction on a sidecar-named page that matches its original's bytes (D1).
- **Paired page:** a sidecar-named page that holds a kept extraction for its original.
- **Part folder:** the dot folder that holds a page's further parts.
- **Attached result:** a speech result that an agent sends to the cell with `media.attach-speech` (D6).
- **Track:** one transcribed audio signal: an audio stream, a declared channel or one member file of a session (D7).
- **Session:** a group of separate recordings of one event, grouped by a governed operation (D11).

## Decisions

### D1. Adoption pairs a page with its original

The plan pairs a sidecar-named page with its original when all these checks pass:
1. The original classifies as audio or video.
2. The page has `type: source` and the same `media_type`.
3. The page's extraction is complete: no pending or failed state, an `extracted_by` engine, and text under `## Extracted text`.
4. Its `binary_sha256` and `binary_size` equal the staged original's SHA-256 and size.
5. Its `evidence_file` names the original's file name.
6. Every part that `extraction_parts` lists is in the page's part folder.

The plan already hashes each original, so check 4 adds one front matter read per sidecar-named page. Apply repeats the checks under its mutation guard, with the bytes it writes. A page or part that changed after the plan gives the item `SOURCE_CHANGED`, as a changed original does.

A page that fails a check keeps today's behaviour. The plan and the apply result count these pages by reason: `KEPT_EXTRACTION_MISMATCH`, `KEPT_EXTRACTION_INCOMPLETE` or `KEPT_EXTRACTION_PARTS_MISSING`. The person sees why a transcript was not kept. The scan can read the page's front matter, but only the plan knows the original's SHA-256, so the plan decides the pairing.

Pairing reads the product's own sidecar layout and schema fields, and binds by SHA-256. It reads no words of the page to decide what the page means. So it is not a C6 pattern.

A paired page is not filed as a Source of its own, and neither are its parts. The plan shows it with its original. Two identical recordings in one run each keep their own paired page, and the duplicate rules decide which Source stores the bytes.

When a stored Source already holds the original's bytes, the original records an import mapping. The paired page is not filed. A completed extraction on that Source stays, and the counts list the page under `KEPT_EXTRACTION_ORIGINAL_STORED`. So a second run of the same tree still writes nothing.

When that Source's extraction is pending or failed, the page pairs with it. The checks are the same, with the stored original's SHA-256 and size in place of a staged original's. The file-name check does not apply, because filing renamed the original (`preserve`, `original_filename`). The SHA-256 and size bind the page to its bytes. Apply writes the kept extraction fields and part pages of D2 to that Source, guarded by the Source page's content hash. The Source keeps its own provenance. This is the case of a windowed import: the original arrives in an artifact window and its page in a later text window. It is also the case of a recording that an earlier import filed with a pending extraction.

Apply retires a speech job that waits for that Source. A job that already runs produces a stale result, and the worker does not publish it, because the sidecar changed (`media_worker`, "newer canonical input preserved").

The scan still records a part folder as a skipped hidden folder, as the scan rule requires. Pairing reads the part pages of a paired page as content of that page. The plan shows the number of parts with each pairing.

### D2. What the kept Source holds

Apply writes the original's Source page from the paired page. It keeps:
- the text under `## Extracted text`, including the timed lines;
- `speakers`, `speaker_verification`, `diarize_requested`, `extracted_by` and the `asr_` facts;
- the part pages, with their text unchanged.

It sets:
- the provenance fields for the new artifact, as reconciliation records them, so that `evidence_file` names the artifact's vault path;
- the adoption provenance that every applied Source carries (`imported_from`, `import_collection`, `collection_path`, `original_sha256`);
- the Source's identity, as for any adopted Source;
- the part folder's new name, and the `part_of` and `next_part` links, because adoption names the artifact anew.

The kept text passes the card-number and credential removal that applies to every kept text (`client-artifact-preservation`). A removal is reported as a warning, as elsewhere.

### D3. The extraction request finds the result complete

Apply still asks for extraction after each artifact commit, as `adoption-studio` requires. Reconciliation sees a completed extraction for the unchanged original and admits no speech stage. That is the existing rule: a valid transcript is never overwritten, and no ASR is queued for it (`automatic-media-processing`).

A kept video still needs its frame vectors. Today the worker queues those for completed visual media only when it starts (`media_worker._scan_unindexed_images`). The request for a kept video therefore admits a frame-vector job alone, the same job shape that scan queues. Visual search then does not wait for a worker restart.

`adoption-studio` "Media apply acknowledges durable custody without joining derived work" persists the admitted extraction job before acknowledgement. Reconciliation admits a job only when the media type's stage is on and the extraction is not complete (`media_processing.reconcile_media`, `media_engines.stage_enabled`). An original on a cell with speech off is acknowledged with no job today. A kept audio recording is acknowledged the same way. An added requirement states this, and leaves that long requirement's text unchanged.

### D4. Imports carry part folders, and windows keep them with their page

The selection rules leave hidden entries behind by default (`import_selection.select`). A part folder is a dot folder, so a long transcript would arrive without its parts, and D1 would not pair it.

`exomem import` and `exomem cloud import` share these rules, and the Cloud CLI must apply "the same rules as `exomem import`" (`cloud-vault-import`). So the exception lives in the shared selection: it selects the part folder of each selected page. The other selection rules still apply to the files in it. The report counts the part folders that the selection carries. An added requirement in `adoption-studio` states the exception. It leaves the text of the local staging requirement unchanged, which the active change `scale-agent-led-imports` also modifies.

A part folder of a page that does not pair, such as a long document extraction, also travels. The scan records it as a skipped hidden folder, and filing ignores it. Its bytes are text.

A windowed series sends artifacts first and text last, each in path order (`add-cloud-media-store`). The CLI orders members by class, then by path (`cloud_import_windows._sorted`). A part folder's name starts with a dot, so it usually sorts before its page, with other entries between them. A window boundary could therefore fall between them. The CLI sorts a part folder's members with their page, places no boundary between them, and counts them as one member for window sizing. Text still travels last. The original arrives in an earlier window, so D1 pairs the page with the stored original.

The CLI does not judge whether a page holds a completed extraction; the cell does.

### D5. New audio is transcribed on the cell's CPU

Cell Whisper runs at about 0.43 times real time. One audio hour takes about 2.3 hours of processing. A cell can therefore transcribe at most about 10 audio hours a day, with its media slot busy all day. New audio in a personal vault is well under an hour a day, so the cell keeps up.

No mechanism is added. The owner's cell gets the speech engine through `cellctl.mediaEngineCellIds`, under an operator-approved live change (task 2.1). The switch re-renders the cell and restarts its pod, and it starts the cell's backfill. So the operator switches speech on after the import finishes, outside the backup window. Before it, the operator reads how many recordings still wait with a pending extraction, and their hours, because the backfill transcribes them.

### D6. The owner's agent attaches speech results

A cell cannot diarise:
- pyannote needs PyTorch, and the image build gate keeps PyTorch out.
- On a CPU, pyannote runs at RTF 1.13 to 1.26 with a 2.9 GiB peak. The speech budget is 1 GiB, and the cell has 3 GiB in total.
- RTF 1.13 to 1.26 means more than an hour of diarisation for each audio hour. A cell would spend that after about 2.3 hours of transcription, with its media slot held.

A personal install diarises through its diarizer sidecar: slowly on a CPU, fast on a GPU. So the owner's agent computes the labels on the owner's machine, and attaches them to the Cloud Source. The agent is an AI client that already holds a connection to the vault.

**The route.**
1. The agent reads the original with `exomem cloud download`, or uses the owner's local copy.
2. The agent processes that file on the personal install with labels requested (`media.process` with `diarize`), as for any recording there.
3. The agent builds the call from the local page's timed lines, labels and `asr_` facts.
4. The agent calls `media.attach-speech` with that result and the original's `binary_sha256` and `binary_size`.

Nothing new runs on the owner's machine: no service, no daemon and no credential. The call uses the agent's existing connection and write access. The same operation serves any customer's agent.

**The operation.** `media.attach-speech` is a new kind in the `process_media` group, with the `change_media` write class, so the existing write authorization applies. The name avoids `exomem attach`, which uploads a file. The call carries:
- the original's `binary_sha256` and `binary_size`;
- segments: `start`, an optional `end`, `text` and an optional `track`;
- optional speaker turns: `start`, `end`, `label` and an optional `track`;
- optional names: a name for a label, with its basis, `person` or `profile`;
- the engine identity, as the local page's `extracted_by` and `asr_` facts state it;
- an optional `replace_labelled` flag.

A personal install's sidecar holds timed lines, not segment ends. An agent that starts from timed lines sends no `end`, and each segment ends at the next one's start, as in D13. Each line's label becomes a turn over that span.

**Binding.** The operation compares the call's SHA-256 and size with the Source's original, and refuses a mismatch with `ATTACH_BINARY_MISMATCH`. A Source with no audio or video original is refused with `ATTACH_NOT_MEDIA`.

**One rendering.** The cell feeds the segments and turns to the speech pipeline's code: `speaker_assignment.assign_span`, label naming, the multitrack merge (D7) and `render_timed_lines`. `extract._diarize` today reads its turns from the diarizer. Its assignment and rendering move into one function that takes turns, and both paths call it. The result then goes through the worker's extraction publication. That splits parts, keeps a replaced extraction on a prior page, and checks the original's identity and the page's content hash.

**Honest provenance.** The Source records `extraction_origin: attached`, the declared engine in `extracted_by`, and the result's digest. The cell cannot check the engine identity, and the Source does not claim that it did.

**Bounds.** The cell reads the recording's duration from the container metadata with PyAV, without decoding, as `video_frames._probe_duration` does for video. It refuses, with `ATTACH_PAYLOAD_OUT_OF_BOUNDS` and the bound's name:
- a segment or turn that starts before 0 or ends after the duration plus a tolerance;
- a segment that ends before it starts;
- more segments, turns or characters than a rate per audio second allows.

A container without a duration is refused with `ATTACH_DURATION_UNKNOWN`. The bounds are constants beside the operation's schema. They check a payload's shape, not its meaning, so they are not policy data. A ten-hour transcript is a few megabytes, well under the service's ingress body limit (32 MiB by default, `service_ingress`).

**Replacement.** An attach replaces a pending or failed extraction, and a completed one with no labels and no names, such as a cell's own transcript. A result with labels or `speaker_names` is replaced only when the call sets `replace_labelled: true`; otherwise the call is refused with `ATTACH_REPLACES_LABELS`. A replaced extraction stays on a linked prior page. The attach retires a speech job that waits for the Source. A running job's result is stale and is not published.

**Names.** When an attach replaces a named result, names carry by the rule of D13. A name in the call wins for its label. `speaker_verification` is `human-verified` only when a person named every label. An attach never enrols a voice; D14 keeps that an owner action.

**Repeats.** Before the replacement rules, the operation compares the call's original and result digest with the Source's current attached result. When they match, it writes nothing and reports the earlier outcome. A retry after an unknown outcome follows the write door's mutation identity rules.

**Cost.** On a personal GPU, diarising a recording costs a few cents of electricity. This is an estimate from one owner's setup, not a measurement.

**Later alternative: a platform burst GPU job.** The platform could rent a GPU for a batch, read the originals under the platform's custody rules, and call `media.attach-speech` for each. Nothing would be installed on a customer's machine. The price below uses an L4-class GPU at about EUR 0.79 per hour. The provider price is not verified. The example collection holds 81 audio hours.

| Job | Rate | GPU-hours | Cost |
|---|---|---|---|
| Labels only (pyannote) | RTF 0.015, measured | about 1.2 | about EUR 1 |
| Transcription and labels again | 6 to 10 times real time, estimated | about 8.1 to 13.5 | about EUR 6.40 to 10.70 |

Each job also pays its start-up time. The platform job needs a privacy copy change and an operator decision about the provider. A later change decides it.

### D7. Tracks

A recording can carry its voices on separate tracks. Speech takes tracks from three sources:
1. **Audio streams.** Each audio stream of a container is a track.
2. **Declared channels.** The channels of one stream are tracks only when they are declared as separate microphones. The upload, the owner, the agent or the device metadata (D8) declares them. Otherwise a multi-channel stream is one mixed track, as today. No measurement of the audio declares channels.
3. **Session members.** Separate files grouped as one session (D11) are one track each.

Each track is transcribed on its own. The transcript merges the tracks' segments by start time and labels each turn with its track's name.

A track is diarised only when labels are requested for the recording and the track is not declared single-voice. Its turns then read `<track name>: Speaker A`. Where diarisation cannot run, as in a cell, the result reports that labels are unavailable. The track names still label the turns, so a multitrack recording gets speaker names on Cloud without diarisation.

### D8. Track metadata comes from published specifications

Speech reads track names and start times from these published fields:
- iXML `TRACK_LIST`, for each track's name and channel;
- BWF `TimeReference`, the start as a sample count since midnight;
- the container's stream `title` tag and its timecode.

A recorder family that writes these facts in its own fields gets a mapping in shipped data, not in code (C4). Adding a family is a data change. A name that the owner or the agent gives wins over metadata.

### D9. Crosstalk: a track keeps a segment where it is the dominant source

Each microphone also hears the other speakers. Without a remedy, one sentence appears twice, once in each track.

The remedy: a track keeps a segment only where it is the dominant source. For each segment, speech compares the tracks' speech energy over the segment's span. Each track's energy is normalised by that track's own speech level. The segment stays when its track is the loudest, or within a margin of the loudest. The margin lives in the speech decoding policy data, and the sidecar records the policy revision (`automatic-media-processing`, "Speech Decoding Policy Is Data").

Why this remedy:
- It runs with no person, so it works for every upload and for backfill.
- It is deterministic and cheap: it reads audio that speech has already decoded.
- It keeps track names, so it keeps free speaker labels.
- Real overlap stays: two people who speak at once are both within the margin.

The alternative, where the owner chooses the mixed track for the text, needs a person for each recording, and it loses the track names.

### D10. Progress, timeouts and cost per track

Each track keeps its own progress record and its own job timeout. Today the record is keyed by the file's vault path (`onnx_asr_runtime._progress_path`). It is keyed by vault path and track. A stop then resumes the track that it stopped, and a long multitrack recording does not spend the time budget of one job.

The cell's CPU cost multiplies by the number of tracks. A two-track hour takes about 4.6 hours of processing on a cell. Status reports the tracks of a recording that waits.

### D11. Sessions

A governed operation groups separate recordings as one session. It names each member's track. An agent or the owner calls it.

Alignment:
- When every member carries a BWF `TimeReference` or a container timecode of the same kind, the operation aligns the members by it.
- Otherwise the members are aligned by their start. That alignment is assumed, and the session page records `alignment: assumed-start`.

The session transcript is a page of the note type that the note-type registry marks as the session transcript type, in that type's folder (C4). This change ships that entry in the core pack, marked by an attribute, in the way the pack marks its profile type. Code finds the type by the attribute, never by its key. The page cites its member Sources.

The session page stays current. When a member's extraction or names change, the session page is rendered again from its members. A section that the owner writes on the page is kept, as on a sidecar.

### D12. Renaming speaker labels

An agent-facing governed operation renames the speaker labels of one recording. It takes the recording's Source and a map from current labels to names. The person tells the agent who spoke, and the agent records it.

The sidecar stores no turn list. The timed lines are the stored turns. The operation:
1. reads every timed line of the extraction, including the part pages, with `parse_timed_lines`;
2. replaces the label of each line that the map names;
3. renders the lines again with the transcript renderer;
4. rewrites `speakers` from the rendered lines;
5. records each name and the label it replaced in `speaker_names`.

Text outside a label never changes, and the operation never replaces text in the body. A line whose words mention "Speaker B" keeps those words.

`speaker_verification` becomes `human-verified` only when every label carries a name that a person gave. Otherwise it keeps its value, so a partial naming does not claim more than it is (`automatic-media-processing`).

A name must be one line and must not hold a character of the label syntax (`[`, `]` or `:`). This checks the name's format, not its meaning. The operation refuses a bad name, and a label that the recording does not carry, each with a code that names the rule.

`speaker_names` is an extraction field. A reprocess moves it with the old text to the prior extraction page, as it moves `speakers`.

### D13. Names survive a reprocess

A reprocess that produces labels carries each name of the prior extraction to the new label whose lines overlap the named lines most. A line's span runs from its start to the next line's start. A name carries only when the overlap covers more than half of the new label's speech time. A majority rule needs no tuning, so it is not policy data.

The result lists each name that it could not carry. A reprocess without labels carries no names, and the result says so. The prior extraction page keeps its names in any case. An attach that replaces a named result uses the same rule (D6).

### D14. Keeping a voice

Keeping a voice enrols a voice profile, so that later recordings name that speaker without help. `speaker-diarization` says that enrolment is not an MCP connector tool. So keeping a voice is an owner action on a personal install:
- `exomem enroll-speaker` gains a form that takes a recording's Source and one label.
- The personal install's terminal Home offers the same action.

Both show the label's sample lines, and the owner confirms. At confirmation, the install computes that label's ECAPA centroid from the label's line spans. It stores the centroid in the local voice-profile store under the given name.

For a Cloud vault, the install reads the original through the owner's signed-in Cloud download. It checks the original's SHA-256 against the Source, and it deletes its copy after it computes the centroid. Cloud stores no speaker embedding for any recording.

The rename reply names the owner action that keeps a voice, so that an agent can point the person to it.

## Scale

The repository records no load target for Cloud cells. This change reports that as debt, and sizes for one vault with tens of thousands of recordings.

- **Pairing** adds one bounded front matter read for each sidecar-named page to a plan that already hashes every original. A paired page's parts are read once, in apply, inside the existing chunk bounds of items and bytes.
- **The extraction request** for a kept result runs the existing reconciliation for one file, and admits at most one frame-vector job.
- **An attach** validates and renders one recording's result, then writes its page and parts in one batch. Its cost grows with that recording's length only, and its payload stays under the ingress body limit.
- **Pairing with a stored original** looks up the Source by the page's SHA-256, through the duplicate lookup that adoption already uses (`duplicates.find_duplicate`).
- **Renaming** reads and writes one recording's page and its parts in one batch. Its cost grows with that recording's length only.
- **A session render** reads its members. A session holds the tracks of one event, so its member count stays small.
- **Multitrack cost** multiplies the cell's speech time by the track count (D10).

## Risks / Trade-offs

- **A hand-edited old page.** A person may have edited a transcript in the old vault. The edit is kept, as in that vault. Secrets are removed as from any kept text.
- **An old engine.** A kept transcript may come from an older engine than the cell runs. `extracted_by` and the `asr_` facts say which engine wrote it, and an explicit reprocess replaces it.
- **A declared engine.** An attached result names an engine that the cell cannot check. `extraction_origin: attached` says so, and a reprocess or another attach replaces it.
- **A wrong attach.** An agent can attach a poor result to the right bytes. A labelled result needs the explicit flag to be replaced, and every replaced extraction stays on a prior page.
- **Part folders of other pages.** The selection also carries part folders of pages that do not pair. They are small text, and filing ignores them.
- **Crosstalk margin.** A margin that is too narrow drops a quiet speaker's words, and a margin that is too wide keeps echoes. The margin is data, and the sidecar records its revision.

## Migration Plan

1. Ship the adoption pairing, the kept Source write, pairing with a stored original, the extraction request rule, the part-folder selection and the window order.
2. The owner runs `exomem cloud import` of the vault, and the agent files each window.
3. The operator reads the pending speech count, then switches speech on for the owner's cell under an operator-approved live change (task 2.1).
4. Undo for step 3: remove `speech` from the cell's entry in `cellctl.mediaEngineCellIds` and deploy. Transcripts already published stay.
5. Ship `media.attach-speech`. The owner's agent can then attach labels for recordings whose kept page had none, and for new recordings.
6. Ship multitrack and naming when their tasks are done. They need no migration.

This change only adds requirements. `scale-agent-led-imports` modifies `adoption-studio` requirements that this change does not touch, so the two archive in either order. The window order requirement refines the windows of `add-cloud-media-store`, so this change archives after it.

## Open Questions

1. When adoption meets a stored Source with a completed but unlabelled in-cell transcript, and the paired page carries labels, should adoption replace it, as `media.attach-speech` would? This change keeps the stored transcript, and the owner's agent can attach the labels.
2. Which values do the attach bounds take? Task 3.3 sets them beside the schema, each with its reason.
