## ADDED Requirements

### Requirement: The upload handoff has an import lane

`upload_files` SHALL accept the lane `import`, which SHALL imply a held upload and require `expected_sha256`. The reply SHALL keep the handoff descriptor and add `next`: the multipart POST that sends the archive, as a ready shell line, and the call that redeems it, `manage_memory` `imports.file` with the descriptor's token as `upload`. The lane's bytes SHALL be redeemable only by `imports.file`, for the session that holds them. They SHALL NOT charge the media allowance, and the bearer upload routes and `exomem attach` SHALL NOT accept the lane. A held import upload SHALL keep the held-upload cap, count and one-hour life.

#### Scenario: An import upload is admitted over a full media allowance

- **WHEN** the media allowance is exhausted and an agent sends an archive with the lane `import`
- **THEN** the upload is held, and an upload with the lane `source` of the same size is refused with `MEDIA_ALLOWANCE_EXCEEDED`

#### Scenario: The reply names both steps

- **WHEN** an agent calls `upload_files` with the lane `import`
- **THEN** the reply carries a `next` that names the send and the `imports.file` call with the descriptor's token
