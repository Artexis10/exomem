## ADDED Requirements

### Requirement: A committed file is never reported as failed

The compact projection of a per-file outcome whose bytes were committed (`stored`, `already_stored`, or an adoption `replayed`) SHALL NOT report that file as `failed`, and SHALL NOT replace the row with `INVALID_ARTIFACT_RECEIPT`, whatever its warnings or receipt fields hold. Warnings are advisory. The row SHALL carry the first 8 warnings, each cut to 300 characters, and `warnings_omitted` with the number beyond 8; `warnings_count` SHALL count every warning. A receipt field that does not read, among `state`, `stored_path`, `size`, `hash`, `hash_algorithm`, `content_type`, `media_id` and the optional earlier-copy, archive, transcription and adoption receipts, SHALL be left out of the row and named in the row's `unverified_fields`. Such a row SHALL keep its `outcome` and, where it reads, its `state`. The server SHALL log a warning that names the fields. A terminal that committed a file SHALL NOT read "Nothing saved".

#### Scenario: A long title makes each file's warning long

- **WHEN** `save_source` receives two attached files and a title whose slug exceeds the filename limit, and its per-file warnings exceed 300 characters
- **THEN** both rows read `stored` with their own SHA-256, the stored bytes hash to the supplied bytes, and the summary reads "Saved 2 files"

#### Scenario: A stored row has a malformed size

- **WHEN** a committed file's outcome carries a `size` that is not a non-negative integer
- **THEN** the row reads `stored`, omits `size`, lists `size` in `unverified_fields`, and the summary counts the file as stored

### Requirement: A digest holds an upload

`upload_files` with the lane `source` or `evidence` and `expected_sha256` SHALL imply a held upload, as the lane `import` does. `transfer_artifact` with operation `upload` and `expected_sha256` and no `artifact` SHALL do the same. A call without `expected_sha256` and without `hold` SHALL keep returning the bearer-token handoff. The reply SHALL be the held handoff that `hold=true` returns, with `committed=false`.

#### Scenario: A digest without hold

- **WHEN** an agent calls `upload_files` with the lane `source` and an `expected_sha256` and no `hold`
- **THEN** the reply is a held handoff for that lane with `hold=true` and `handoff_status=handoff_prepared`, and not an `INVALID_MODE` refusal
