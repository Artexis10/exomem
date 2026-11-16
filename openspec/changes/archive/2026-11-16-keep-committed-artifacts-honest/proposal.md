# Proposal

## Why

A client that attached two photos to `save_source` was told that nothing was saved. Both originals were committed with the right bytes. The compact receipt projection rejects any row whose advisory warning is longer than 300 characters and replaces it with a failure. A long title makes every file's slug warning longer than that. The same row check turns any unreadable receipt field into a failure. A receipt must never contradict a commit.

The same client then asked `upload_files` for a held upload with a digest and no `hold`. The call fell into download recovery and returned an error that does not name `hold`.

## What Changes

- A file whose bytes were committed is never projected as `failed`. Warnings are advisory: the row keeps the first 8, each cut to 300 characters, and counts the rest.
- A receipt field that does not read is left out of the row and named in `unverified_fields`. The row keeps its state, and the server logs the field names.
- `upload_files` and `transfer_artifact` treat a digest on an upload as a held upload, as lane `import` already does.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `client-artifact-preservation`: a committed file is never reported as failed; a digest holds an upload.

## Impact

The compact `files` rows may carry two optional keys: `warnings_omitted` and `unverified_fields`. The `state` values do not change. The stored terminal keeps its shape. `warnings_count` counts every warning, including those a row omits. The `SLUG_TRUNCATED` warning quotes the slug once.
