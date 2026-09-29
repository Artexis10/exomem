## ADDED Requirements

### Requirement: Both file-handle commands declare OpenAI file parameters

The generated MCP descriptors of `preserve_artifacts` and `capture_source` SHALL both carry
`_meta["openai/fileParams"] == ["files"]`, and each command's `files` items SHALL keep the
four-field file object with only `download_url` and `file_id` required. No tool argument
that receives a file SHALL name a local filesystem path.

#### Scenario: A ChatGPT attachment is raw material

- **WHEN** the generated `capture_source` MCP tool is listed
- **THEN** its descriptor identifies `files` as an OpenAI file-parameter field
- **AND** its file items declare exactly `download_url`, `file_id`, `mime_type` and `file_name`

#### Scenario: No schema accepts a local path for a file

- **WHEN** every published tool input schema is scanned
- **THEN** no file-receiving argument and no argument named as a local or file path exists

### Requirement: Local clients hold uploads and redeem them through the file-handle commands

On local ingress, `/upload` SHALL accept a multipart `hold=1` field with a `lane` of
`evidence` (the default) or `source`. It SHALL stream the file into private machine-local
state outside the vault under the upload byte cap, commit nothing to the vault, and return
a file handle whose `download_url` is an opaque `exomem-held:` reference, together with the
held bytes' size, SHA-256, content type, lane and expiry. The handle's `file_id` SHALL NOT
contain the secret. On any other ingress `hold=1` SHALL be refused with
`HELD_UPLOAD_LOCAL_ONLY`.

`preserve_artifacts` and `capture_source` SHALL redeem such a handle without a network
request. A redemption SHALL succeed only for a request carrying a verified local grant for
the same local session that held the upload, through the command of the held lane
(`preserve_artifacts` for `evidence`, `capture_source` for `source`), at most once, and
within one hour of the hold. An unknown, malformed, foreign-session, expired or already
redeemed handle SHALL fail with `HELD_UPLOAD_UNAVAILABLE` and one reason for all of those
cases, and a refused redemption SHALL NOT consume the hold. A lane mismatch for the
holding session SHALL fail with `HELD_UPLOAD_LANE` and leave the hold redeemable. Held
bytes that no longer match their recorded SHA-256 SHALL NOT be committed. A hold is spent
only when its file is stored or found already stored; when a claimed hold's file fails
without being stored, the bytes SHALL be put back as the same hold, redeemable by the
holding session until the original expiry.

One local session SHALL keep at most 16 live holds and 256 MiB of held bytes; a hold
beyond either SHALL be refused with `HELD_UPLOAD_QUOTA` and hold nothing. Expired holds
SHALL be removed when a hold is made and when one is redeemed, and an existing hold
directory SHALL be made private to its owner before it is used.

`exomem attach <file>` without `--scope` and `--category` SHALL hold the file and print
the returned handle; `--lane source` SHALL hold it for `capture_source`.

#### Scenario: A local image is preserved byte for byte

- **WHEN** a local client holds an image with `exomem attach` and passes the printed handle to `preserve_artifacts` over its local session
- **THEN** the stored Evidence artifact is byte-identical to the original file
- **AND** its outcome reports the original's SHA-256, size and content type

#### Scenario: Another principal presents the handle

- **WHEN** a different local session, a public-path session, or a caller with no local grant presents a held handle
- **THEN** that file fails with `HELD_UPLOAD_UNAVAILABLE`
- **AND** the holding session can still redeem it

#### Scenario: A spent or expired handle is presented

- **WHEN** a held handle is presented after it was redeemed, or more than one hour after it was held
- **THEN** that file fails with `HELD_UPLOAD_UNAVAILABLE` with the same reason an unknown handle gets

#### Scenario: The wrong lane redeems a hold

- **WHEN** the holding session passes an evidence-lane handle to `capture_source`
- **THEN** that file fails with `HELD_UPLOAD_LANE` and the hold remains redeemable by `preserve_artifacts`

#### Scenario: A claimed hold's file fails

- **WHEN** the holding session redeems a handle and that file fails after the claim without being stored
- **THEN** the same handle is still redeemable by that session until its expiry

#### Scenario: A session exceeds its hold quota

- **WHEN** a local session with 16 live holds, or 256 MiB of held bytes, holds another upload
- **THEN** it is refused with `HELD_UPLOAD_QUOTA`, nothing more is held, and another session's holds are unaffected

#### Scenario: A public upload asks to hold

- **WHEN** a request authorised by an upload token on the public path sends `hold=1`
- **THEN** it is refused with `HELD_UPLOAD_LOCAL_ONLY` and nothing is held or preserved

### Requirement: A transcription is recorded only with its original

`preserve_artifacts` SHALL accept optional `transcriptions`, each an object with a
`file_id` naming one supplied file and a non-empty `text`. A transcription naming no
supplied file, or naming one file twice, SHALL refuse the whole call before any file is
staged. Because a `file_id` is a caller-editable label, a call with transcriptions whose
files repeat a `file_id` SHALL also be refused before any file is staged, and each
transcription SHALL be bound to the one supplied file its `file_id` names, never to
another file carrying the same label. When the named original is stored, the transcription SHALL be written in the same
Evidence write as the original's extracted text, with `extracted_by: client-transcription`
and a `transcription` record of the original's SHA-256, size and content type, and the
file's outcome SHALL report `transcription.state == "recorded"` with the page path. When
the original is `already_stored`, its page SHALL be left unchanged and the outcome SHALL
report `transcription.state == "not_recorded"`. When the original fails, no transcription
SHALL be written anywhere.

#### Scenario: An image and its transcription are preserved together

- **WHEN** `preserve_artifacts` stores an image and a transcription names its `file_id`
- **THEN** the image's Evidence page carries the transcription as extracted text, marked `client-transcription`, with the image's SHA-256, size and content type
- **AND** the outcome reports the transcription as recorded on that page

#### Scenario: The original does not arrive

- **WHEN** a transcription names a file whose retrieval fails
- **THEN** that file's outcome is `failed` and the transcription text appears in no vault file

#### Scenario: Two files share a file_id

- **WHEN** a call supplies transcriptions and two of its files carry the same `file_id`
- **THEN** the call is refused before any file is staged, no hold is consumed and no transcription is written

### Requirement: Guidance preserves the original first

Bootstrap, the file-handle tool descriptions and the scaffold skill SHALL tell agents that a
user-shared file used as evidence is preserved as the original first, with any transcription
saved alongside and never instead. The scaffold SHALL name each client's path: file handles
on ChatGPT, held uploads through `exomem attach` on local clients, and `transfer_artifact`
plus `/upload` from code execution on claude.ai, which requires the user's opt-in. Where a
client cannot pass the bytes, guidance SHALL say so plainly and SHALL NOT report the original
as preserved.

#### Scenario: A client cannot pass the bytes

- **WHEN** an agent on a client with no file handle, no local attach and no code-execution egress is asked to keep an attached file
- **THEN** guidance directs it to tell the user the original was not preserved and how to upload it
- **AND** it does not save only a transcription as though custody were complete
