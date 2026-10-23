# Owner-export acceptance evidence

## B2 provider check — 2026-10-23

The disposable-prefix check passed against B2 using the maintained controller
storage and export adapters. The check uploaded 387 bytes of invented content.
It created no control-database rows and accessed no customer vault.

The provider reported `AES256` on both completed multipart versions. A pinned
attachment link returned the first version after the second upload. The provider
refused that link after its short expiry.

An unfinished multipart upload was visible through the S3 API. The maintained
deleted-cell prefix cleanup removed it without a separate pre-abort. Independent
native and S3 listings then reported no versions, delete markers or unfinished
uploads. The disposable key was deleted and its absence was verified.

The operational receipt and sanitized consumer output are retained privately.
No provider credential, signed URL, account identity or bucket name is published.
This closes task 4.2. It does not prove a shared rehearsal or production rollout.

## Shared rehearsal

Task 4.3 remains open. Its optional `owner-export` workflow must record the owner
request, real controller and Job, verified download, expiry cleanup and return
to Ready. The dedicated security review follows that observed workflow.

The isolated Versity double does not implement provider encryption at rest.
The B2 check above supplies that separate provider proof.
