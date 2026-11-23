## 1. Held filesystem capability

- [x] 1.1 Add `no_replace_rename` to `Capabilities`; record it in the probe instead of disabling the mount; let `acquire` take the needed set, with the full set as the default.
- [x] 1.2 Make `FilesystemStore` ask only for the operations that it uses.
- [x] 1.3 Prove that a vault or state-root acquire on a mount without the capability is still refused, and that a rename there refuses instead of replacing.

## 2. Eviction gates

- [x] 2.1 Read the media store configuration from the state root's file when `EXOMEM_MEDIA_STORE_CONFIG` is unset.
- [x] 2.2 Keep an original whose pointer's key version has no confirmed export on a personal install.
- [x] 2.3 Confirm where a pointer's publication time is recorded, and keep an original whose copy is younger than `eviction_settle_seconds` or has no recorded time.

## 3. Owner surface

- [x] 3.1 Add the owner commands that turn eviction on and off, with their refusals and output.
- [x] 3.2 Add the upload floor at the shared admission seam, with `MEDIA_SPACE_BUSY` and a retry hint.
- [x] 3.3 Add the local and evicted counts and the wait reason to the owner status.
- [x] 3.4 Document the setup in `docs/stored-original-replicas.md`.

## 4. Proof

- [x] 4.1 Use a disposable vault and a WSL-mounted Windows drive. Declare the store, upload originals, evict them and verify their SHA-256 on read-back. Withhold the test store's marker and verify that the store reports unavailable.
- [x] 4.2 Run the scoped suites, `openspec validate --all --strict` and the public-artifact privacy gate.
