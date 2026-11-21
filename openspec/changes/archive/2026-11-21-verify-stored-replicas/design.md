# Design

## Context

See `proposal.md` for the outcome. These parts already exist (`add-original-replica-custody`):

- **The binding is the record.** A sidecar's `stored_object` pointer names the replica in the store that serves eviction, and `replicas` lists the others. Each entry records the store, object, version, key version, ciphertext size and ciphertext SHA-256. The binding also records the plaintext `binary_sha256` and `binary_size`.
- **The `Store` protocol.** `upload`, `head(name, version)` and `open(name, version)`. `head` returns a size and a SHA-256. The `filesystem` adapter computes that digest from the stored bytes. The `media-edge` adapter returns the digest declared to the provider at upload.
- **The descriptor.** Each declared store states its failure domain, its retrieval class and whether `head` recomputes content (`head_recomputes`). A vault entry without a `stores` list reads as one store whose `head_recomputes` is unknown.
- **The replica table.** `media_replicas` in the media ledger holds one row per original and store, derived from the bindings and rebuilt daily. Status reads it in one grouped query.
- **The replica worker.** One daemon thread per vault rebuilds the table, observes whether each store answers as itself, and drains the copy queue.
- **Fixity repair** tries local copies, then each replica, and keeps a copy only if it hashes to the binding.

No row of the primary-source roster's pick-by-problem table covers storage verification, so none applies here.

The repository records no load target. This design sizes for one vault of 500,000 originals and 25 TB, with every original in two stores: 1,000,000 replica rows.

## Goals / Non-Goals

**Goals:**

- Each replica is checked on a cycle, with the strongest proof its store allows.
- A damaged or absent replica becomes an owner finding, and the product replaces it when it can.
- The status says what each check proved and when, and never claims more.

**Non-Goals:**

- The Cloud `restic check` job for the vault backup. It changes cellctl and needs its own rollout.
- A B2 adapter, a storage policy, a minimum copy count.
- Deleting a damaged version from a store. The adapters have no delete capability.
- Hydration from a replica other than the pointer.

## Decisions

### D1: The descriptor decides which check a store gets

A check's strength depends on what the store can prove, and the descriptor already says so:

| Descriptor | Scheduled check | What it proves |
|---|---|---|
| `head_recomputes: true` | content | The stored ciphertext hashes to the binding's `ciphertext_sha256` and has its size. |
| `head_recomputes: false` | presence | The version exists, with the recorded size and declared digest. |
| unknown (no declared descriptor) | none | Nothing. The status says "not scheduled" and names the reason. |

Both checks call the existing `head`, so no adapter changes. Code branches on the descriptor value, never on an adapter or store name.

A store without a declared descriptor gets no check, because a check whose strength is unknown would report a proof it cannot name. A Cloud cell's file declares no `stores` list today, so Cloud cells keep their current behaviour until cellctl declares the store. That declaration is a separate, operator-approved rollout.

Rejected alternative: run a presence check on every store, declared or not. It would start new edge traffic on every Cloud cell with this release, outside that rollout.

### D2: A sampled check proves decryption within a monthly budget

Only a download and decrypt proves that the key version still opens the replica and that the plaintext matches `binary_sha256`. That costs egress on a remote store. So each store gets a monthly byte budget for sampled checks, in an optional descriptor field `sample_bytes_per_month`. The default is 1 GiB. Zero turns sampling off. The owner sets it per store, because egress cost is a property of the store.

A sample uses the replica read that fixity already uses (`write_stored_original`) into a sink that only hashes. No plaintext reaches disk. It runs only where repair could read the replica: an `immediate` store and a key version whose identity this install can read. Any other replica reads "sample not possible" with the reason.

The sample takes the replica whose last decrypt proof is oldest, never-proven first, that fits the month's remaining budget. A replica larger than the remaining budget is skipped for the month, and the status counts the skips. The media ledger records the bytes spent per store and calendar month (UTC).

### D3: Check results live in the derived table and survive its rebuild

The table gains `checked_at`, `level` (`presence`, `content` or `decrypted`), `decrypted_at`, `failure` and the state `quarantined`. A rebuild keeps these columns for a row whose object and version are unchanged, as it already keeps the upload time. A new version starts unchecked.

A lost table loses the check history. Every row then reads "never checked", which is true of what the vault knows, and the cycle checks them first. A lost quarantine is found again by the next check of that replica. Repair rehashes every copy anyway, so a forgotten quarantine never lets bad bytes in.

Rejected alternative: write check results into the binding. 1,000,000 sidecar writes per cycle, each with index and sync work, to record observations that rebuild cheaply.

### D4: A failed check quarantines, reports and re-copies

A replica is quarantined when `head` answers "absent", or returns another size or digest than the entry records, or when a sample's plaintext does not match. An unavailable store, a timeout or a store that does not answer as itself never quarantines: unavailable is not proof of loss. Those leave the row as it was and count in the store's reach.

A quarantined replica:

- is not a repair candidate, and eviction does not trust it;
- shows in the status and in the per-original view, with the failed level and time;
- gets a re-copy request in the existing copy queue.

The re-copy writes a new version into the same store. The source is the local original when its bytes hash to the binding, or else another replica that is not quarantined, copied as ciphertext and hashed against its own recorded digest on the way, as the copy path does for evicted originals. The publication replaces exactly the quarantined entry in the binding, by compare-and-swap on its object and version, under the vault mutation guard. The bad version stays in the store as an orphan. With no verified source, the replica stays quarantined, and the finding reads `no_verified_source`.

Eviction already calls `head` on the store serving eviction before it drops local bytes. A mismatch there now quarantines the pointer and reports it, instead of refusing silently. This closes the drill's gap.

What quarantine prevents: repair or eviction trusting a replica known to be bad, and an owner who believes they hold two copies when they hold one. Its cost when it fires wrongly: one extra copy of an original into a store. A false quarantine needs a store to return a wrong size or digest for an intact object, so the cost is small and the store pays in space.

### D5: Checks are paced like fixity, inside the replica worker

The replica worker runs a check round after its copy drain, within a time bound per round, and yields to foreground work between replicas. It never holds the vault mutation guard for a check. A content check reads bytes under a read-rate cap, with a default of 32 MiB/s like fixity's.

The cycle defaults to 30 days and is an operator setting. Each round picks the rows whose `checked_at` is oldest, never-checked first, per store, through an index on `(store, checked_at)`. A round's byte budget follows fixity's `budget_for`: the store's total bytes times the round interval over the cycle.

At the sizing target:

- Presence checks cost 500,000 `head` requests per store per 30 days, about one every five seconds.
- Content checks of a 25 TB filesystem store read about 10 MB/s averaged over the cycle, under the 32 MiB/s cap.
- One pick is an index range scan with a `LIMIT`, whatever the table size.

### D6: Fixity skips quarantined replicas and keeps a copy it could not install

Fixity's candidate list drops a replica whose row is quarantined, and names it in the route as `quarantined`. A replica that fixity reads and that fails its hash is quarantined through D4, so the check and the repair agree.

When a copy verifies but cannot be put in place, fixity now keeps the staged file in its private state, named by the binding's SHA-256. The next repair of that row tries the kept file first, as a local copy, and rehashes it like any other. The kept file is removed after a restore, when the row's hash changes, or when the walk retires the row. `clear_staging` removes only unfinished `.part` files, never a kept copy. At most one kept file exists per row.

## Risks / Trade-offs

- **Orphaned versions.** Each re-copy leaves the bad version in its store. Mitigation: re-copy is rare, and the orphan is named in the finding. A release path for replica stores is a later change.
- **Large originals starve the sample.** An original larger than the month's budget is never sampled. Mitigation: the status counts the skips per store. A larger budget or a ranged sample is a later choice.
- **The presence check trusts the store's declared digest.** It proves existence, not integrity. Mitigation: the status labels its level `presence`, and the sample proves content within its budget.
- **A lost table forgets quarantines.** Mitigation: the next check finds them again, and repair rehashes every copy.

## Migration Plan

The table gains columns through `CREATE TABLE IF NOT EXISTS` plus `ALTER TABLE ADD COLUMN` for an existing table. Every existing row starts unchecked. The binding format and the configuration file format are unchanged, apart from the optional `sample_bytes_per_month` field. An older release ignores that field.

Rollback is a release downgrade. The older release ignores the new columns and the sample ledger. A quarantined replica reads "verified at upload" again, and repair still rehashes it.
