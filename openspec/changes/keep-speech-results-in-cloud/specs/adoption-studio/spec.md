## ADDED Requirements

### Requirement: Adoption pairs a recording with its completed Evermatter page

Adoption SHALL pair a staged audio or video original with its sidecar-named page: the staged page named `<original name>.md` in the same folder. It SHALL pair them only when the page holds a kept extraction, with all of these:

- `type: source` and the original's `media_type`;
- a completed extraction with text under `## Extracted text`;
- `binary_sha256` and `binary_size` equal to the original's;
- an `evidence_file` that names the original's file name;
- every part page that it lists.

#### Scenario: A matching page is paired

- **WHEN** a staged folder holds a recording and its completed page, whose `binary_sha256` and `binary_size` match the recording
- **THEN** the plan shows the page paired with the recording

#### Scenario: A page for other bytes is not paired

- **WHEN** a sidecar-named page records a `binary_sha256` that differs from its staged original
- **THEN** the plan does not pair the page

#### Scenario: A pending page is not paired

- **WHEN** a sidecar-named page records a pending extraction
- **THEN** the plan does not pair the page

#### Scenario: A page with missing parts is not paired

- **WHEN** a sidecar-named page lists three parts and its part folder holds one
- **THEN** the plan does not pair the page

### Requirement: A paired recording's Source keeps the page's speech result

When adoption stores a paired original as a new Source, apply SHALL write the Source page from the paired page. The Source SHALL keep the extracted text with its timed lines, `speakers`, `speaker_verification`, `diarize_requested`, `extracted_by`, the `asr_` facts, and the part pages with their text unchanged. Apply SHALL set the artifact's provenance and the adoption provenance as for any adopted Source. The kept text SHALL pass the card-number and credential removal.

#### Scenario: A recording keeps its transcript and labels

- **WHEN** adoption files a recording with a paired page that carries speaker labels
- **THEN** the recording's Source holds the page's extracted text, timed lines, `speakers` and `speaker_verification`
- **AND** the Source carries `imported_from`, `import_collection`, `collection_path` and `original_sha256`

#### Scenario: A long transcript keeps its part pages

- **WHEN** the paired page lists three parts and its part folder holds them, and adoption runs without including hidden files
- **THEN** the new Source lists three parts, and its whole text equals the old page's whole text

### Requirement: A kept speech result starts no speech job

The extraction request that follows the commit of a paired original SHALL admit no speech stage. For a video it SHALL admit a frame-vector job alone, so that visual search does not wait for a worker restart. Custody acknowledgement for a paired audio original SHALL wait for no extraction job, as for an original whose extraction stage is off.

#### Scenario: A kept recording is not transcribed again

- **WHEN** adoption files a recording with a paired page while the speech engine is on
- **THEN** no speech job exists for the recording

#### Scenario: A kept recording needs no job before acknowledgement

- **WHEN** default apply imports an audio recording with a paired page
- **THEN** the original, the copied bytes and the Source page are durable before the reply, and no extraction job exists for the recording

#### Scenario: A kept video gets its frame vectors

- **WHEN** adoption files a video with a paired page
- **THEN** no speech job exists for the video, a frame-vector job exists for it, and its frame vectors are indexed without a worker restart

### Requirement: A paired page is not filed on its own

A paired page and its part pages SHALL NOT be filed as Sources of their own. When a stored Source already holds the original's bytes with a completed extraction, apply SHALL NOT file the paired page, and that Source SHALL keep its own extraction. Apply SHALL repeat the pairing checks with the bytes it writes. A page or part that changed after the plan SHALL give the item `SOURCE_CHANGED`.

#### Scenario: No text Source holds a paired page

- **WHEN** adoption files a recording with a paired page
- **THEN** no text Source holds the page or its part pages

#### Scenario: A second run writes nothing

- **WHEN** the same folder with a paired page is planned and applied again unchanged
- **THEN** the recording reports `already-applied`, the page is not filed, and no file in the vault changes

#### Scenario: A page changed after the plan

- **WHEN** a paired page changes between `plan` and `apply`
- **THEN** the item records `SOURCE_CHANGED`, and nothing is written for it

### Requirement: A page that arrives after its original keeps its result

A sidecar-named page SHALL also pair with a stored Source whose original matches the page's `binary_sha256` and `binary_size`, and whose extraction is pending or failed. Apply SHALL write the kept extraction to that Source as for a staged original, guarded by the Source page's content hash, and SHALL NOT file the page. It SHALL retire a speech job that waits for that Source. A result that a running job produces later SHALL NOT replace the kept one.

#### Scenario: A page in a later window keeps its result

- **WHEN** a series of windows files a recording in an artifact window, and its paired page in a later text window
- **THEN** the recording's Source holds the page's extraction, and no text Source holds the page

#### Scenario: A completed Source keeps its own extraction

- **WHEN** a sidecar-named page matches a stored Source whose extraction is complete
- **THEN** that Source keeps its extraction, the page is not filed, and the counts list the page under its reason

#### Scenario: A running job does not replace a kept result

- **WHEN** a speech job for a pending Source is running when adoption writes the kept extraction to that Source
- **THEN** the job's later result is not published, and the Source keeps the kept extraction

### Requirement: Import selection carries a selected page's part folder

The selection rules that `exomem import` and `exomem cloud import` share SHALL select the part folder of each selected page, as an exception to the hidden-entry rule of local staging. A part folder is the dot folder that holds a page's further extraction parts, such as `.X.parts/` for the page `X.md`. The other selection rules SHALL still apply to its files. The report before staging or archiving SHALL count the part folders that the selection carries.

#### Scenario: A long transcript's part pages travel with it

- **WHEN** the folder holds a page with a part folder, and the user does not pass `--include-hidden`
- **THEN** the selection holds the part folder's pages, and the report counts the part folder

#### Scenario: Other hidden entries still stay behind

- **WHEN** the folder also holds a hidden folder that is no page's part folder
- **THEN** the selection holds nothing from that folder, and the report counts it under the hidden reason

### Requirement: An unpaired page is filed as before and counted

A sidecar-named page that fails a pairing check SHALL keep today's behaviour: adoption files it as a text Source, and its original gets a pending extraction. The plan and the apply result SHALL count the originals stored with a kept extraction. They SHALL also count, by reason, the sidecar-named pages whose extraction was not kept.

#### Scenario: A mismatched page is filed as text and counted

- **WHEN** a sidecar-named page records a `binary_size` that differs from its staged original
- **THEN** adoption files the page as a text Source and the original with a pending extraction
- **AND** the plan and the apply result count the page under its reason
