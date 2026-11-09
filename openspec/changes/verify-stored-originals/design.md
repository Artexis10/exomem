## Context

A stored original is a file that a binding names with its path and SHA-256. Two kinds of binding exist today. A sidecar binding names `evidence_file`, `data_file` or a governance companion with `binary_sha256`. Artifact Sources carry `evidence_file` and `binary_sha256` too, so `media_store.bound_originals` finds them with Evidence and datasets. An archive kept as members has a member manifest. It names each pool blob by the SHA-256 of its uncompressed bytes, and `archive_members.read_manifest` reads it.

The derived lexical index can be dropped and rebuilt at any time. Fixity state must therefore live outside it.

## Decisions

**D1. A daemon thread of its own, started as `_start_graph_drain` is started.** Reason: fixity must run where media extraction is off. Rejected: the media worker, because `EXOMEM_DISABLE_MEDIA_EXTRACTION` removes it.

The thread runs by default. `EXOMEM_DISABLE_ORIGINAL_FIXITY=1` stops it, as the other `EXOMEM_DISABLE_*` flags stop their components, and status then reports `off`. The flag is an operational lever for a rollback without a release revert. It is not a gate.

**D2. A fixity table keyed by vault path and payload SHA-256.** It holds the file identity, the last verification time and the state. The expected hash always comes from the binding, never from the table. Reason: the binding is the authority, and a lost table then costs only verification times. Rejected: columns in the lexical index, because a rebuild would erase the history and fixity would depend on that index.

- A binding walk fills the table from `bound_originals`, the member manifests and the Source pages that `adoption_plan.read_import_index` reads. That index adds only the text originals that a Source page fences, because `bound_originals` already finds artifact Sources. A path that two walks find is one row. A row retires when no binding names its path and hash.
- The binding walk runs at start and then once a day by default. The interval is configuration, and task 5.1 measures it.
- With a missing table or a new schema, the binding walk rebuilds it, and every row starts `unverified`.

**D3. Identity decides when to hash.** Fixity records `import_stage.Identity` at each verification. That identity has no device number, so a pod move keeps it. A matching identity lets the last verification stand until its turn. A different identity forces a hash in the same pass. This is the rule that `media_store.cached_sha256` applies to the media cache record. Fixity applies it to its own record and does not call `cached_sha256`, because that record carries the device number. Reason: a stat is cheap and a hash is not. Rejected: hashing every original on every pass.

**D4. Budget and schedule are configurable defaults.** The proposed defaults are a pass every hour, a full cycle in about 30 days and a 32 MiB/s read cap. A pass's byte budget is the local original bytes times the pass interval, divided by the cycle, with a floor. The budget limits only the re-hash of unchanged originals. The hash of a changed identity and the check of a restore copy fall outside it, and every read stays under the read-rate cap. Task 5.1 measures the defaults. Fixity never reads an evicted original, because the read would download it and the store confirmed its digest at upload. The thread takes the vault mutation guard as a background holder and yields to foreground writes.

**D5. A restore uses only a copy whose hash matches the binding.** Fixity tries these copies in order and re-hashes each one before use:

1. the media-store version of a cached original;
2. a pool blob with that hash, decompressed;
3. another stored original with that hash;
4. on a local install, the outside file that a local import journal recorded with that hash, if it is still present.

Before it moves anything, fixity re-checks under the vault mutation guard that the file and the binding are unchanged since the hash. The changed bytes then go to Trash with their protection, and the restored file takes the protection of its own path. A pool blob may restore an unprotected original, because its bytes are exactly what the binding already names. Reason: a restore with the wrong bytes is worse than none. Rejected: adopting the new hash when no copy exists, because that turns damage into truth.

**D6. An outside edit is restored as corruption is.** A changed identity gives the cause `edited`, and an unchanged identity with other bytes gives `corrupted`. A row with no recorded identity gives `unknown`, as after a table rebuild. Reason: originals are never modified, and every note that cites an original rests on its recorded bytes. Any write moves the change time, so `corrupted` points at the storage itself. Rejected: reporting edits without a restore.

**D7. A missing original is reported, never restored.** Reason: a deletion by hand may be deliberate, and a silent restore would undo it. The report names each matching copy that fixity finds, so the owner can restore it.

**D8. Fail closed on restore, eventually consistent on schedule.** Restore selection and the pre-move re-check refuse on any doubt. A busy guard, a pressured pass or a stale table reschedules work and refuses nothing.

**D9. Signals carry no content off the owner's surfaces.** The attention item and the status block may name paths, because the owner reads them. Another audience that reads attention receives a copy outside the vault by its kind only, because its path names the owner's own filesystem. Cloud readiness carries only counts, and the count never makes a cell unready. Rejected: failing readiness, because one damaged file would take a serving cell out.

A `restored` original reaches the owner's attention item and the status counts. The readiness count holds only unresolved `missing` and `corrupt_no_copy` rows, because a restore needs no operator action.

**D10. A text original is verified and never repaired automatically.** Fixity hashes the UTF-8 bytes of the text that `adopt.text_source_body` returns and compares them with the page's `original_sha256`. A mismatch records `corrupt_no_copy` with the cause and a route: restore the page from a backup, or import the outside file again. Reason: a repair is a Source page write, and the append-only guard forbids it. Rejected: a new state for text, because `corrupt_no_copy` already means "damaged, nothing changed, owner action needed".

This design keeps two existing contracts. A restore copies byte-exact originals only, so "Missing original material is never reconstructed automatically" holds. Fixity only reads outside files, so "Originals are never modified" holds.

## Risks

- A pass after a whole-volume restore finds every identity changed and hashes everything at once. The read-rate cap still holds, so the pass takes longer and denies nothing.
- On Cloud, a cached original deleted by hand reads as `evicted` through `original_state`, and the media store hydrates it on the next read. Fixity follows that state function and does not second-guess it.
