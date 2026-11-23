<!-- authority:non-specification -->

# Check the replicas of your originals

A replica is a second encrypted copy of an original file in a store that you declared.
Exomem checks each replica on a cycle, reports a damaged one as a finding and copies it again when it can.
This page says what each check proves, how to set the sample budget and how to read a finding.

## Free space on a personal vault disk

A personal install can put encrypted originals on a separate filesystem store.
The store supports WSL-mounted Windows drives that provide hard links and directory flushes.
Eviction removes plaintext from the vault and its current mirrors. Keep the store and the exported key for recovery.

Place the vault's media store configuration in `media-store.json` in its state directory.
Keep the existing `vaults` object and store descriptors. Remove `EXOMEM_MEDIA_STORE_CONFIG` from the process environment before using the owner commands.

The following commands use the example vault `/srv/archive` and a configured store named `disk`.

1. Run `exomem media-replicas declare-store disk --vault /srv/archive`. The store gets an identity marker.
2. Export the decryption key outside the machine. You can then record that export.
3. Run `exomem media-replicas confirm-key-export --vault /srv/archive`. The ledger records the current key version.
4. Run `exomem media-replicas enable-eviction disk --vault /srv/archive --free-bytes 10737418240`. Eviction targets 10 GiB of free space.

The store must have no replica entries. A switch also requires the previous store to have no remaining pointers, including pointers in Trash.
The store must be on a different device from the vault. Declaration writes its marker through the open store directory.
If the store disappears, put the original volume and marker back before retrying.
The default margin frees an extra tenth of the requested space. The upload floor reserves half of the requested space.
Use `--margin-bytes` and `--upload-floor-bytes` to set those values. An upload at the floor returns `MEDIA_SPACE_BUSY` with a retry hint.
The final part of a resumable upload reserves space for its commit stages. The service keeps accepted parts if commit must wait.
Hydration, signed-container extraction and each final storage copy use the same vault-volume budget.
The budget covers database, search and graph writes for stored Markdown originals. Their projected files keep the floor protection.
When an outside rename meets the floor, the next retry checks both paths. Other imports continue.

After a copy settles for ten minutes, eviction checks its whole ciphertext before it removes the local bytes.
The service flushes the version directory and store root before relying on that copy for eviction.
Eviction confirms the configured path still reaches that same copy immediately before removal.
On personal installs, eviction starts and stops using the disk space available to the service. A bounded walk continues across passes.
Use `--eviction-settle-seconds` to set that time. An unknown publication time keeps the original local.
Reading an evicted original verifies and decrypts it from the store. A missing store reports `MEDIA_STORE_UNAVAILABLE`.

Run `exomem media-replicas disable-eviction --vault /srv/archive` to stop eviction.
The worker restores originals that fit. You can still read the remaining originals from the store.

The owner’s `process_media` status has a `replicas.eviction` section. It reports bound physical originals and plaintext bytes from the last complete inventory.
The inventory counts a shared object once. Its `observed_at` field gives the time of the count.
If the latest walk cannot prove a count, `inventory_state` reports `failed` at `inventory_attempted_at` and retains the preceding counts.
The `waiting` field gives the last eviction pass's reason and `waiting_observed_at` gives its time.
If a shared original has too many bindings or distinct versions to check in one pass, `binding_proof_limit` reports why its local bytes stay.
Unknown inventory counts and wait reasons appear as null.

## What each check proves

A check proves one of three levels. The status names the level that each replica last passed.

| Level | What the check does | What it proves |
|---|---|---|
| `presence` | Asks the store whether the version exists with the recorded size and SHA-256. | The store says it holds the object. A store that only repeats the digest it was given proves nothing more. |
| `content` | Asks a store that recomputes the SHA-256 of the stored ciphertext. | The stored bytes are the bytes that Exomem wrote. |
| `decrypted` | Downloads the replica, decrypts it and hashes the plaintext. | The key still opens the replica and the plaintext matches the original. |

The `head_recomputes` field of the store's descriptor picks between the first two levels.
`true` gives a `content` check and `false` gives a `presence` check.
A store with no `head_recomputes` is not checked, and the status says so with the reason `head_recomputes_not_declared`.
A `presence` check never counts as `content`: the status counts each level apart.

The `decrypted` level is a sample, because it costs a download.
It runs only where checks run (a store whose descriptor states `head_recomputes`), for a store with `retrieval_class` `immediate`, and for a key version whose identity this install can read.
A store whose checks are not scheduled is never sampled and is never charged bytes.

## Declare a Backblaze B2 replica store

Add an adapter named `b2` to the vault's `stores` list. Supply the exact `bucket`, `bucket_id`, and nonempty `prefix` allowed by its application key. Set the store's `retrieval_class` to `immediate` and `head_recomputes` to `false`. Set `failure_domain` to the location's independent failure domain.

Set `credential_file` to an absolute path on the serving host. Keep `key_id` and `application_key` in that private JSON file. Use a bucket-and-prefix restricted key with read and write access and no delete permission. Keep the bucket's referenced versions available for recovery.

Use the existing replica operation to request copies. A direct B2 store serves replica copies and exact-version restore. Keep the vault's primary eviction store configured separately.

## Protect originals with a storage policy

Add a `protection` object to the vault's media store configuration to request verified copies.
Set `minimum_verified_copies`, `minimum_failure_domains`, and the eligible store names in `replica_stores`.
Give each eligible store a `failure_domain` that identifies where its data can fail together.
To count a verified local copy, set `local_failure_domain`.

Set `copy_bytes_per_month` to the automatic upload allowance, in ciphertext bytes per UTC calendar month.
The worker reserves the bytes before a transfer. An uncertain result keeps its reservation.
An existing confirmed version can settle the original reservation when the worker retries.
Owner-requested copies and repairs use their existing queues.

Automatic policy transfers require a store that can confirm a preassigned version.
The filesystem replica adapter supports this. The store serving eviction gets its copies through offload.

Watch `replicas.protection` in media status for the latest sweep, pending copy reasons and the current month's transfer allowance.
A sweep reports coverage during its observation window. Unreadable bindings and incomplete proof remain unknown.
The worker requires a content proof, usable recovery key and no quarantine finding before it counts a replica.

Set `eviction_authorized` only after you approve automated local eviction under this policy.
The worker still requires verified coverage for every filing of the local original after removal.
Omit the `protection` object to use the ordinary media worker.

## Set the cycle and the read rate

Each replica is checked about once in 30 days, oldest check first.
The worker schedules a round every ten minutes. Each store's share uses its replica count and, when HEAD reads ciphertext, its total bytes.
The read cap defaults to 32 MiB per second. It applies to filesystem HEAD even when the descriptor gives a presence check.
A filesystem size mismatch costs no ciphertext read. If the window ends during hashing, the next round resumes after confirming the same held file version.
A read of unknown size after a store starts work counts toward the cap conservatively. Waits that outlast a round carry forward.
At 500,000 similarly sized replicas and 25 TB in one store, the calculation gives 116 replicas and 5.4 GiB in about 216 seconds per round.
Two such stores use about 432 seconds per round. At this distribution and cadence, both cover their replicas within 30 days.
Larger or unevenly sized objects and slow or unavailable stores can lengthen the cycle.
Raise `EXOMEM_REPLICA_CHECK_READ_MIB_PER_SECOND` if the read cap is the limiting factor.
The store that has waited longest goes first in each round.
Set these two environment variables on the service to change them:

- `EXOMEM_REPLICA_CHECK_CYCLE_DAYS`: the days between two checks of one replica.
- `EXOMEM_REPLICA_CHECK_READ_MIB_PER_SECOND`: the read rate of a content check.

## Set the sample budget of a store

Each store has a monthly byte budget for sampled decrypt checks.
Set `sample_bytes_per_month` in the store's entry of the `stores` list.
The default is 1 GiB (1073741824). The value `0` turns sampling off for that store.
Use an integer from 0 through 9223372036854775807, the media ledger's largest byte count.
An invalid value makes the store unavailable and the status names the field.

1. Open the media store configuration of your vault.
2. In the entry of the store, add the field. For example: `"sample_bytes_per_month": 5000000000`.

Expected result: the next status shows the new `budget_bytes` for that store.

The sample takes the replica whose sample turn came longest ago first.
It charges a replica's bytes to the month before it downloads them, so an interrupted download still counts.
When the sample window ends, the worker stops waiting and the decoder terminates. The reader stops before another chunk.
An outstanding marker probe, HEAD, or sample keeps that store's read slot until the transport or filesystem returns.
The worker gives other stores their turns. A deferred sample spends none of its monthly budget.
A copy checks the destination marker in that same read slot before opening the source.
An edge read applies its deadline to the connection, response, and body.
A replica larger than the remaining budget waits for the next month and counts in `skipped_for_size`.
The month is a calendar month in UTC.
An older release ignores this field.

## Read the status

The `process_media` status result carries a `replicas` section.
Each store, and the totals, report these values:

- `replicas` and `verified_at_upload`: the copies made and the copies that the upload verified.
- `quarantined`: the replicas that a check or a repair found damaged.
- `recoverable`, `not_recoverable` and `not_recoverable_reasons`: whether each replica survives the loss of this machine. A quarantined replica is never recoverable, and its reason is `quarantined`.
- `failed_attempts`: the replicas whose last check or sample could not complete, with what it found. These replicas are not quarantined. The tokens are `binding_unreadable`, `binding_lists_no_replica`, `record_invalid`, `store_read_failed` and `key_refused`.
- `checks`: for `presence`, `content` and `decrypted`, the replicas whose last check proved that level and the time of the oldest.
- `never_checked`: the replicas that no check has reached since upload, or since a rebuild that lost the history.
- `checks_not_scheduled`: the replicas whose store gets no check, with the reasons.
- `sample`: the `budget_bytes` of the month, the `spent_bytes`, the replicas `skipped_for_size` and the replicas `not_sampled` with a reason each.

A value that Exomem cannot read shows as `unknown`, never as zero.
A store that is offline, slow or not answering as itself changes no replica. That includes a disk or NAS that is unmounted while a check or a copy starts: the store reads as unavailable, never as empty.
Unavailable is not proof of loss, so it never makes a finding.

## Read the findings

A replica becomes a finding when its bytes are proved damaged:

- The store says the version is `absent`, or `condemned` (a hide or a newer version made the bucket rule delete it).
- The ciphertext has another size or SHA-256 than recorded (`size_mismatch`, `digest_mismatch`).
- The ciphertext matches its record and decrypts to bytes that are not the original (`plaintext_mismatch`).

These are not findings, because they say nothing about the bytes: a store that is offline or slow, a decryption that a signal stopped, and a replica that one unreadable row of the binding hides.
The status reports them in `failed_attempts`, and the next turn of the replica tries again after a day.
When ciphertext matches its record and your identity refuses it, the status reports `key_refused`.
Check the identity file, then wait for the next sample: a passing sample clears it, and a repair is never blocked by it.
A later failed attempt on the same replica replaces it with that attempt's finding, and a passing check does not clear it.

List the findings with the command line:

1. Run `exomem media-replicas findings --vault <vault path>`.
2. Read each entry: `path`, `store`, `failed_level`, `failure`, `found_at` and `recopy`.
3. When the result has a `next_after` value, run the command again with `--after-path` and `--after-store` set to its two parts.

Expected result: a page of at most 100 findings, in order of path and store.

The `recopy` value says what the worker does about the finding:

- `pending`: the worker copies the replica again into the same store.
- `refused` with the code `no_verified_source`: the original is not on the volume and no other replica is usable. The binding is unchanged.
- `null`: no copy is queued.

The worker writes a new version, from the local original when it still hashes to the binding, or else from another replica that is not quarantined.
A first replica of an evicted original takes its source by the same rule, its pointer included: an entry that is recorded as damaged is never copied into another store, and with no usable entry the request reads `refused` with `no_verified_source`.
A wrong plaintext belongs to the stored bytes, not to one name. A finding of a wrong plaintext records the digest of those bytes, so a copy of them under any other name counts as damaged too: it is no source, eviction does not trust it, and the same finding quarantines its filing with a queued copy, so the status counts it as damaged and not recoverable at once. A finding of another cause, such as an absent version, does not clear that digest, and it moves every quarantined filing of the version to its own cause, so none keeps an older one.
The damaged version stays in its store as an orphan. For the store that serves eviction, its name is also released for collection, unless a retried copy reuses it.
Filings of equal bytes can share one version in the store that serves eviction, and the media allowance counts it once. A finding is about the version, so the status reports every filing that shares a damaged version as a finding, and each has its own queued copy. The worker writes one new version for the first of them, and the others point to it, including after a key rotation. Until each has moved, the allowance counts both versions.
Exomem records a damaged version once, whichever filings list it. Eviction keeps an original's local bytes while its version is recorded, even if the filing that held the finding is gone, and a rebuild quarantines any filing that still lists it and never forgets the record. A new filing of equal bytes never reuses a recorded version: it uploads its own, and the filings that still list the damaged one copy from it. A filing whose original is on the volume and hashes to its binding uploads its own copy under the current key version, and does not take a replacement of an older key; one whose bytes do not verify takes it. A sample picks only a filing that a wrong plaintext holds, and releases it only if no newer finding was recorded since the sample began: a wrong plaintext of the same bytes under any name, or another cause on its version. A version that a later finding found lost or differing is no false alarm: its filings stay quarantined with their queued copies. Eviction that finds a pooled original's version absent or condemned records the finding in the same way.
Eviction and fixity never read a quarantined replica.
A `plaintext_mismatch` finding gets one more sample per cycle, and a sample that decrypts to the original lifts it, for every filing that shares the version or holds a copy of its bytes under another name and is quarantined for a wrong plaintext, and drops their queued copies.
Any other finding stays until a new copy replaces the entry.

When a finding reads `no_verified_source`, bring back the original or another good replica.
The daily rebuild then tries the copy again, a request for a new replica as well as a copy again.
