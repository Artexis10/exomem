## 1. Held filesystem capability

- [ ] 1.1 Add `no_replace_rename` to `Capabilities`; record it in the probe instead of disabling the mount; let `acquire` take the needed set, with the full set as the default.
- [ ] 1.2 Make `FilesystemStore` ask only for the operations that it uses.
- [ ] 1.3 Prove that a vault or state-root acquire on a mount without the capability is still refused, and that a rename there refuses instead of replacing.

## 2. Eviction gates

- [ ] 2.1 Read the media store configuration from the state root's file when `EXOMEM_MEDIA_STORE_CONFIG` is unset.
- [ ] 2.2 Keep an original whose pointer's key version has no confirmed export on a personal install.
- [ ] 2.3 Confirm where a pointer's publication time is recorded, and keep an original whose copy is younger than `eviction_settle_seconds` or has no recorded time.

## 3. Owner surface

- [ ] 3.1 Add the owner commands that turn eviction on and off, with their refusals and output.
- [ ] 3.2 Add the upload floor at the shared admission seam, with `MEDIA_SPACE_BUSY` and a retry hint.
- [ ] 3.3 Add the local and evicted counts and the wait reason to the owner status.
- [ ] 3.4 Document the setup in `docs/stored-original-replicas.md`.

## 4. Proof

- [ ] 4.1 Run a drill on a Windows drive in WSL with a disposable vault: declare the store, upload originals, evict them, read them back with matching SHA-256, and unmount to see an unavailable store.
- [ ] 4.2 Run the scoped suites, `openspec validate --all --strict` and the public-artifact privacy gate.
