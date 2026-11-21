<!-- authority:non-specification -->

# Check the replicas of your originals

A replica is a second encrypted copy of an original file in a store that you declared.
Exomem checks each replica on a cycle, reports a damaged one as a finding and copies it again when it can.
This page says what each check proves, how to set the sample budget and how to read a finding.

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

## Set the cycle and the read rate

Each replica is checked about once in 30 days, oldest check first.
A round runs every ten minutes and takes each store's share of the cycle: its replicas and, for a content check, its bytes, each times ten minutes over the cycle.
A content check reads at up to 32 MiB per second on average: an object is read at the store's speed, and only what remains of its share of time after the read is waited out. An object so large that its wait outlasts a round leaves the wait to the next rounds, which read nothing until it is paid.
A read that fails after the store answered counts as a read of the whole object, because Exomem cannot tell how much it moved.
The stores of a round share that cap. At 500,000 replicas and 25 TB in one store, a round checks 116 replicas and 5.4 GiB in about 216 seconds, which covers the store in 30 days. Two such stores take about 432 seconds a round, and also finish in 30 days.
Three such stores need more than the cap allows within the ten minutes, so their cycle runs a little over 30 days. Raise `EXOMEM_REPLICA_CHECK_READ_MIB_PER_SECOND` to shorten it.
The store that has waited longest goes first in each round.
Set these two environment variables on the service to change them:

- `EXOMEM_REPLICA_CHECK_CYCLE_DAYS`: the days between two checks of one replica.
- `EXOMEM_REPLICA_CHECK_READ_MIB_PER_SECOND`: the read rate of a content check.

## Set the sample budget of a store

Each store has a monthly byte budget for sampled decrypt checks.
Set `sample_bytes_per_month` in the store's entry of the `stores` list.
The default is 1 GiB (1073741824). The value `0` turns sampling off for that store.
A negative value, or text, makes the store unavailable and the status names the field.

1. Open the media store configuration of your vault.
2. In the entry of the store, add the field. For example: `"sample_bytes_per_month": 5000000000`.

Expected result: the next status shows the new `budget_bytes` for that store.

The sample takes the replica whose sample turn came longest ago first.
It charges a replica's bytes to the month before it downloads them, so a sample that stops part-way is still paid for.
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
The damaged version stays in its store as an orphan. For the store that serves eviction, its name is also released for collection, unless a retried copy reuses it.
Filings of equal bytes can share one version in the store that serves eviction, and the media allowance counts it once. The status reports every filing that shares a damaged version as a finding. The worker writes one new version for the first of them, and the others point to it. Until each has moved, the allowance counts both versions.
Eviction and fixity never read a quarantined replica.
A `plaintext_mismatch` finding gets one more sample per cycle, and a sample that decrypts to the original lifts it and drops the queued copy.
Any other finding stays until a new copy replaces the entry.

When a finding reads `no_verified_source`, bring back the original or another good replica.
The daily rebuild then tries the copy again.
