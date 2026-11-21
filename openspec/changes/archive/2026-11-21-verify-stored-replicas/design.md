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

The sample runs only where checks run (D1), so a store with no declared descriptor is never sampled and never charged. It takes the replica whose sample turn came longest ago, never-sampled first, that fits the month's remaining budget. A replica larger than the remaining budget is skipped for the month, and the status counts the skips. The media ledger records the bytes spent per store and calendar month (UTC). A sample charges a replica's bytes before it downloads them, so a sample that stops part-way is still paid for.

### D3: Check results live in the derived table and survive its rebuild

The table gains `checked_at`, `level` (`presence`, `content` or `decrypted`), `decrypted_at`, `failure`, the schedule keys `check_from` and `sample_from`, and the state `quarantined`. `checked_at` and `decrypted_at` are proof times that the status reports; the schedule keys are the order of each schedule, because a failed attempt proves nothing and must move a row on without reading as a proof. A rebuild keeps these columns for a row whose object and version are unchanged, as it already keeps the upload time. A new version starts unchecked.

A lost table loses the check history. Every row then reads "never checked", which is true of what the vault knows, and the cycle checks them first. A lost quarantine is found again by the next check of that replica. Repair rehashes every copy anyway, so a forgotten quarantine never lets bad bytes in.

Rejected alternative: write check results into the binding. 1,000,000 sidecar writes per cycle, each with index and sync work, to record observations that rebuild cheaply.

### D4: A failed check quarantines, reports and re-copies

A replica is quarantined only when its bytes are proved damaged: `head` answers "absent" or "condemned", the ciphertext that a read returns has another size or SHA-256 than the entry records, or ciphertext that matches its record decrypts to other bytes than the binding. One place proves damage in a read that decrypts: when the read is a sample or a repair, it hashes the whole ciphertext even when `age` stops early, and compares it with the record before it looks at `age`'s exit status. Hydration and export quarantine nothing, so they stop downloading when `age` stops and report only that the original did not verify.

These never quarantine, because they say nothing about the bytes: an unavailable store, a timeout, a store that does not answer as itself, an `age` that a signal stopped, and matching ciphertext that the identity refuses. The last is its own finding, `key_refused`, shown in the status; a sample that decrypts clears it, and a later failed attempt replaces it with its own finding. A row whose binding cannot be read, or whose version the store cannot address, is likewise recorded with what was found (`binding_unreadable`, `binding_lists_no_replica`, `record_invalid`, `store_read_failed`) and skipped; its next turn comes after a day. Those leave the row's state as it was and count in `failed_attempts`.

A quarantined replica:

- is not a repair candidate, and eviction does not trust it;
- shows in the status and in the per-original view, with the failed level and time;
- gets a re-copy request in the existing copy queue.

The re-copy writes a new version into the same store. The source is the local original when its bytes hash to the binding (one that is short, unreadable or of other bytes is no source, and the next is tried), or else another replica that is not quarantined, copied as ciphertext and hashed against its own recorded digest on the way, as the copy path does for evicted originals. The publication replaces exactly the quarantined entry in the binding, by compare-and-swap on its object and version, under the vault mutation guard. The bad version stays in the store as an orphan. With no verified source, the replica stays quarantined, and the finding reads `no_verified_source`.

Eviction already calls `head` on the store serving eviction before it drops local bytes. A mismatch there now quarantines the pointer and reports it, instead of refusing silently. This closes the drill's gap. Eviction reads the quarantine again inside the transaction that unlinks, because the first read comes before the `head` and a finding can arrive while the store answers; the write lock that the transaction holds keeps a later finding out until the bytes are gone. For a pooled object, one quarantined filing already stops the object's eviction (the gate reads the version), so the other filings that share the damaged pointer are quarantined in a bounded way. A filing that holds the damaged pointer goes first: the object's facts carry one holder (the first by path) for each distinct pointer, because the filing under eviction may hold another, healthy pointer of the object and the bounds must never hide a damaged one. Pages of 64 filings by `(object_key, binary_rel)` follow, one transaction a page, up to 1,024 filings and two seconds. What the bounds leave is found by a later attempt or by the filing's own check, so no object with many names costs one write per name.

A quarantine whose ciphertext matched its record and whose plaintext differed stays due for a sample once per cycle. A sample that decrypts to the binding releases it and drops its queued copy. A check cannot release it, because the ciphertext matching its record proves nothing about the plaintext. Every other quarantine stays until a new copy replaces its entry. The pointer of the store serving eviction that a copy replaces has its old name released for collection (collect-released-media D5) before the binding changes, unless a retried copy reuses that name; replica stores collect nothing, so their names stay.

What quarantine prevents: repair or eviction trusting a replica known to be bad, and an owner who believes they hold two copies when they hold one. Its cost when it fires wrongly: one extra copy of an original into a store, and a replica that reads as lost, and as not recoverable, until a sample lifts it or a new copy replaces it. Because only a size or digest that differs from the record, or a plaintext that differs from the binding, quarantines, a wrong identity, a killed decryption, a busy ledger or one unreadable row cannot reach it; the first version of this design let any decryption failure quarantine, and a wrong identity file would have queued a copy again of every replica.

### D5: Checks are paced like fixity, inside the replica worker

The replica worker runs a check round after its copy drain, within a time bound per round, and yields to foreground work between replicas. It never holds the vault mutation guard for a check. A content check reads bytes under a read-rate cap, with a default of 32 MiB/s like fixity's.

The cycle defaults to 30 days and is an operator setting. Each round picks, per store, the rows whose `check_from` is oldest, never-checked first, through an index on `(store, check_from)`, a bounded pick at a time, until the store's share of the cycle, the round's time or the byte budget is spent. A store's share is its replicas times the round interval over the cycle, floored at 16, and for a content check its bytes by the same rule (fixity's `budget_for`). The stores of a round share one read cap, and a round runs 1.25 times the seconds that the cap needs for the byte shares of all its content stores together, between 120 seconds and the interval less the sample's 120 seconds (480). The store with the oldest due replica goes first, so a round that ends early leaves a different store behind each time and no state is kept. The cap is an average over the round: an object is read at the store's speed, and only what remains of its share of time after the read is waited out (the share counts from the read's start, so the read's own time is not paid twice). No read is paced in chunks.

At the sizing target (500,000 replicas and 25 TB in one store, the 10-minute interval, the 30-day cycle and the 32 MiB/s cap):

- A round holds 116 replicas and 5.4 GiB. 4,320 rounds in the cycle check 501,120 replicas and 25 TB.
- Reading 5.4 GiB takes 173 seconds under the cap, so the round runs 216 seconds.
- Two such stores read 10.8 GiB: 346 seconds, a 432-second round, inside the 480-second maximum. Three need 16.2 GiB against the 15 GiB that the maximum reads, so their cycle runs about 32 days until the operator raises the cap.
- Presence checks cost 500,000 `head` requests per store per 30 days, about one every five seconds.
- Content checks of the 25 TB store read about 10 MB/s averaged over the cycle, under the 32 MiB/s cap.
- One pick is an index range scan with a `LIMIT`, whatever the table size.

### D6: Fixity skips quarantined replicas and keeps a copy it could not install

Fixity's candidate list drops a replica whose row is quarantined, and names it in the route as `quarantined`. A replica that fixity reads and that fails its hash is quarantined through D4, so the check and the repair agree.

When a copy verifies but cannot be put in place, fixity now keeps the staged file in its private state, named by the binding's SHA-256. The next repair of that row tries the kept file first, as a local copy, and rehashes it like any other. The kept file is removed after a restore, when the row's hash changes, or when the walk retires the row. The sweep asks the table about the kept digests only, 200 to a lookup through a partial index of the unresolved rows, so a pass costs by the kept files and not by the table. Each lookup probes the index once for each digest and returns at most one row for it, so aliases that share a digest add no rows. A repair takes a replica and the digest it must hash to from one reading of the binding; a binding rebound since the row was read reschedules the row and records no damage against the old digest. `clear_staging` removes only unfinished `.part` files, never a kept copy. At most one kept file exists per row.

## Risks / Trade-offs

- **Orphaned versions.** Each re-copy leaves the bad version in its store. Mitigation: re-copy is rare, and the orphan is named in the finding. The old name of a replaced pointer in the store serving eviction is released for collection; a release path for replica stores is a later change.
- **Large originals starve the sample.** An original larger than the month's budget is never sampled. Mitigation: the status counts the skips per store. A larger budget or a ranged sample is a later choice.
- **The presence check trusts the store's declared digest.** It proves existence, not integrity. Mitigation: the status labels its level `presence`, and the sample proves content within its budget.
- **A lost table forgets quarantines.** Mitigation: the next check finds them again, and repair rehashes every copy.

## Migration Plan

The table gains columns through `CREATE TABLE IF NOT EXISTS` plus `ALTER TABLE ADD COLUMN` for an existing table. Every existing row starts unchecked. The binding format and the configuration file format are unchanged, apart from the optional `sample_bytes_per_month` field. An older release ignores that field.

Rollback is a release downgrade. The older release ignores the new columns and the sample ledger. A quarantined replica reads "verified at upload" again, and repair still rehashes it.
