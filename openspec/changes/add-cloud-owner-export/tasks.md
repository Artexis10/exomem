## 1. Contract C6 and measured disk use

- [x] 1.1 Add C6, the `export` hold kind and the C1 usage columns to cellctl's schema fixtures, with the grants split. Verify: the grant test shows each role holds exactly its C6 columns, the gateway none; the wake-up test shows a request and a link request notify and cellctl's writes do not.
- [x] 1.2 Record used and total bytes from every backup report on the cell row. Verify: a decision test for a recorded backup.

## 2. The cell's export command

- [x] 2.1 Add `exomem cell-export`: quiesced export in owner scope, upload to HTTPS part URLs, a content-free termination report. Verify: the reassembled parts pass `verify_export_archive`; an archive larger than its parts and a plain-HTTP URL are refused before any upload; the cell's size replaces the 10 GiB default; only a file above the per-file limit reports `EXPORT_TOO_LARGE`; the shared ETag digest vector.

## 3. cellctl

- [x] 3.1 Add the export hold, the export Job, completion against the report, links, expiry, supersede and deletion. Verify: decision tests for a stopped cell, a running cell, the deadline, Job error codes, cell deletion and another hold; reconcile tests against real Postgres roles for the round trip, a stopped cell, a substituted part, deletion and per-cell key isolation.
- [x] 3.2 Prove the transport against a real S3-compatible store. Verify: encryption at rest, a version-pinned link refused after it expires, a substituted part refused, and every version removed.
- [x] 3.3 Add the TTL and deadline as chart values. Verify: the chart round-trip test.
- [x] 3.4 Run an export of a running cell and of a stopped cell on the live K3s suite with the source-built Cloud image, through the real admission policies and job egress. Verify: the downloaded archive verifies, and the stopped cell's StatefulSet stays at zero replicas.

## 4. Rollout

- [ ] 4.1 Substrate's change ships migration 0066, the grants, the export API, Home, email 08 and the privacy page copy. Verify: Substrate's own tests and a tenant-isolation test against real roles.
- [x] 4.2 Check against B2 itself, on a disposable prefix, before the export API goes on: `ServerSideEncryption=AES256` on a multipart export, `response-content-disposition` on a versioned link, and deletion of every version. Also check that an unfinished multipart upload under `cells/<id>/` is listed and removed by the deleted-cell prefix deletion, with the controller key's capabilities. Verify: a short evidence note in this change.
- [ ] 4.3 Rehearse the whole export on the shared rehearsal environment, then run the dedicated security review of the C6 delivery path. Verify: the review's verdict and the rehearsal evidence are recorded.
