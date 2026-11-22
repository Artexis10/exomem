# Spec Delta

## ADDED Requirements

### Requirement: Stored replicas are checked on a cycle with the proof their store allows

The replica worker SHALL check every replica in a store with a declared descriptor on a cycle, with a default of 30 days. The check SHALL use the store's `head`. For a store whose descriptor gives `head_recomputes: true`, the check SHALL be a content check: the returned size and SHA-256 SHALL equal the entry's ciphertext size and ciphertext SHA-256. For a store whose descriptor gives `head_recomputes: false`, the check SHALL be a presence check of the same values, and its result SHALL be labelled `presence`. A store whose descriptor does not state `head_recomputes` SHALL get no scheduled check, and the status SHALL report it as not scheduled, with the reason. Each round SHALL take the replicas checked longest ago first, never-checked first, through an index, and SHALL be bounded in time and bytes. A content check SHALL read under a read-rate cap. A check SHALL NOT hold the vault mutation guard. Code SHALL branch on the descriptor value, never on a store's or an adapter's name.

A round SHALL read the configured stores' budgets and earliest due times in one indexed ledger snapshot. Before updating the projection, a refresh SHALL compare the live binding under the publication boundary and match the picked replica row. It SHALL preserve any newer publication and receipt. A filesystem check SHALL read the held length before hashing. A mismatch SHALL require no ciphertext read. The hash pass SHALL stop at the expected length and check its round deadline between reads. If the window ends mid-hash, the worker SHALL retain the in-memory digest and resume only after it reopens and confirms the same held file version. Read pacing SHALL count ciphertext read by the adapter, including filesystem HEAD for a presence check, and a size refusal before reading SHALL cost zero bytes.

The replica worker SHALL run each due check round independently of active copy transfers. It SHALL keep one active copy batch per destination within the process copy limit and rotate through pending destinations. A stalled transfer keeps its slot until the transfer finishes and cleans up. A stopped worker SHALL leave a transfer queued if its guarded publication had yet to start.

#### Scenario: A copy stalls after preflight

- **WHEN** a destination accepts its probe and stalls during a copy
- **THEN** the copy retains its slot while another destination can finish and due verification proceeds
- **AND** stopping that worker leaves the copy queued if its guarded publication has yet to start

#### Scenario: A rotted replica on a NAS

- **WHEN** one byte of a replica's ciphertext changes in a `filesystem` store whose `head` recomputes content
- **THEN** the next check of that replica quarantines it, and the status reports the finding with the level `content`

#### Scenario: A store does not return its marker or HEAD

- **WHEN** a scheduled marker probe or HEAD call outlasts the store’s turn
- **THEN** the worker stops waiting and gives the remaining stores their turns
- **AND** only one read can stay in flight for that store
- **AND** a late result changes neither the evidence nor quarantine

#### Scenario: A store that does not recompute

- **WHEN** a replica's store declares `head_recomputes: false` and the version exists with its recorded size and digest
- **THEN** the replica's last check reads `presence` with its time, never `content`

#### Scenario: A store with no declared descriptor

- **WHEN** a vault entry declares no list of stores, as a Cloud cell's does
- **THEN** no check reads that store
- **AND** the status reports its replicas as not scheduled, with the reason

#### Scenario: An unmounted store

- **WHEN** a store does not answer as itself when its check is due
- **THEN** no replica of that store changes state, and the check tries again on a later round

### Requirement: A sampled check decrypts a replica within a monthly budget

The replica worker SHALL download, decrypt and hash sampled replicas, and SHALL compare the plaintext with the binding's `binary_sha256` and `binary_size`. Each store's sampled checks SHALL stay within a monthly byte budget. The descriptor MAY give it as `sample_bytes_per_month`. The value SHALL be an integer from 0 through 9223372036854775807. The default SHALL be 1 GiB, and zero SHALL turn sampling off. The media ledger SHALL record the bytes spent per store and calendar month in UTC. A sample SHALL take the replica whose last decrypt proof is oldest, never-proven first, among those that fit the remaining budget, and the status SHALL count the replicas skipped for size. A sample SHALL run only for a replica that repair could read: a store whose retrieval class is `immediate`, and a key version whose identity this install can read. The status SHALL give the reason for any other replica. No sampled plaintext SHALL be written to disk.

#### Scenario: A sample proves the key

- **WHEN** a sampled replica decrypts and its plaintext hashes to the binding
- **THEN** the replica's last check reads `decrypted` with its time
- **AND** the replica does not count as never checked, even if no `head` check has reached it

#### Scenario: A sampled read does not return before its deadline

- **WHEN** a pending store read outlasts the sample window
- **THEN** the worker ends its wait and records an incomplete read without quarantining the replica
- **AND** the pending operation holds that store’s maintenance read slot until its handles close
- **AND** a later sample deferred by that slot spends no budget

#### Scenario: The budget is spent

- **WHEN** a store's sampled checks have used its monthly budget
- **THEN** no further sample reads that store until the next calendar month

#### Scenario: A replica the install cannot decrypt

- **WHEN** a replica's key version has no identity this install can read
- **THEN** it is never sampled, and the status gives the reason `identity_unreadable`

### Requirement: A failed check quarantines the replica, reports it and copies it again

A replica SHALL be quarantined when its store answers that the version is absent, when the store returns another size or SHA-256 than the entry records, or when a sample's plaintext does not match the binding. A store that is unavailable, does not answer as itself or times out SHALL NOT quarantine a replica. A quarantined replica SHALL NOT be a repair candidate, and eviction SHALL NOT drop an original's local bytes while its pointer is quarantined. When eviction's own `head` of the store serving eviction does not match the pointer, eviction SHALL quarantine that pointer and the status SHALL report the finding.

An offload census SHALL index live reuse by plaintext digest, size and key version. One attempt SHALL probe at most eight matching versions and exclude known failures from later picks in the same census. The allowance SHALL still count each excluded version. A fresh copy SHALL require room under the current allowance.

The worker SHALL then write a new version into the same store. The source SHALL be the local original when its bytes hash to the binding, or else another replica that is not quarantined, copied as ciphertext and hashed against its own recorded digest. During replication or re-copy, a source read that confirms the version is absent SHALL quarantine that version. An unusable locator SHALL leave any remaining candidates eligible. After a verified source is found, a failed target upload SHALL end the attempt. The publication SHALL replace exactly the quarantined entry in the binding, compared by object and version, under the vault mutation guard. The damaged version SHALL stay in its store, and the finding SHALL name it. With no verified source, the replica SHALL stay quarantined, and the finding SHALL read `no_verified_source`. Check results SHALL live in the derived replica table. A rebuild SHALL keep them for a row whose object and version are unchanged.

Schema recovery SHALL restore missing damage records from surviving quarantines and surviving digest evidence before the ledger gates eviction or reuse. A confirmed version failure SHALL remain attached to that stored version until replacement. A later plaintext finding SHALL renew the shared digest evidence and preserve the version failure when a sample later clears that digest. When an exact legacy filing lacks a digest link, the finding or recovery SHALL restore the link from surviving exact evidence.

A pending first-copy job SHALL keep its request membership when a finding turns it into a repair. The owner status SHALL count the same member pending until the repair completes, a sample clears its finding, or the repair refuses. After loss of the queue, the worker SHALL rescan surviving copy demands and requeue surviving quarantines. Only the current scan SHALL report completion.

Before settling a first copy or a repair, the worker SHALL reconcile the exact current binding and clear the exact sealed protection for the published object. It SHALL use the publication guard and compare the current pending request token. Every worker retry or refusal SHALL also match the pending request and token. A ledger failure SHALL keep the copy retryable. A sample that clears a finding SHALL keep the repair pending when the exact published object has a sealed protection. Guarded settlement SHALL clear that protection. The worker SHALL keep an already published replacement and credit upload time only when the ledger has a receipt for that version.

#### Scenario: A damage table is lost

- **WHEN** a damage table is lost and its quarantine evidence survives
- **THEN** schema recovery restores the evidence to the eviction gate
- **AND** a matching read does not by itself allow eviction of the quarantined version

#### Scenario: A first copy needs repair before its request settles

- **WHEN** a finding turns a pending first-copy job into a repair
- **THEN** the owner still sees that member pending on the same request
- **AND** when the repair settles or a sample clears the finding, the owner sees that member's result

#### Scenario: A replacement is published before its row can be written

- **WHEN** a repair publishes a replacement but its replica row is unavailable
- **THEN** the worker leaves the job retryable
- **AND** the retry projects the replacement already in the binding and settles the job under its current token

#### Scenario: The drill's damaged stored copy

- **WHEN** a replica in the store serving eviction is damaged while the local original is intact, and eviction or a check reads it
- **THEN** the pointer is quarantined and the status reports the finding
- **AND** the worker uploads a new version from the local original, and the binding's pointer names it

#### Scenario: An interrupted offload still has a pointer

- **WHEN** an offload resumes after publishing a version that a check or eviction has since found damaged
- **THEN** offload leaves the pointer in place for the repair worker
- **AND** it does not charge the allowance for a second copy

#### Scenario: An evicted original with a second replica

- **WHEN** the pointer of an evicted original is quarantined and a replica in another store is not quarantined
- **THEN** the worker copies that replica's ciphertext into the store serving eviction, and the pointer names the new version

#### Scenario: No verified source

- **WHEN** a quarantined replica's original is not on the volume and no other replica is usable
- **THEN** the binding is unchanged, and the finding reads `no_verified_source`

#### Scenario: A rebuild during quarantine

- **WHEN** the daily rebuild runs while a replica is quarantined and its entry is unchanged
- **THEN** the replica is still quarantined after the rebuild

## MODIFIED Requirements

### Requirement: The owner sees each original's protection

The owner status SHALL report these values, per store and in aggregate:

- replicas;
- verified replicas;
- recoverable replicas;
- distinct failure domains, unknown when a store that holds replicas declares none;
- the oldest verification time;
- one level per replica with a successful check: the latest `presence`, `content` or `decrypted` proof, and the oldest time of each level;
- the replicas never checked since upload or since the last rebuild that lost their history;
- the quarantined replicas;
- the replicas of a store whose checks are not scheduled, with the reason;
- the sampled bytes spent this month against the budget, and the replicas skipped for size.

A per-original view SHALL page through results by path, and SHALL show each replica's last check, its level and any finding. A paged list of findings SHALL name each quarantined replica's original, store, failed level, time and re-copy state. The aggregate SHALL read one ledger snapshot from indexed state. Its work SHALL be bounded by the stores and returned dimensions, independent of the number of replicas, queued copies and historical keys. The aggregate SHALL NOT walk the bindings. A rebuild in progress SHALL show as unknown. A completed rebuild that skipped unreadable bindings SHALL show as partial and report how many it skipped. A value that the status cannot read SHALL show as unknown, never as zero. The status SHALL NOT read a store: it reports whether each store answered as itself as the background worker last observed it, with the time of that observation. A replica that is known not to survive and a replica whose custody is unstated SHALL be reported apart. A replica that a rebuild restored from its binding has no recorded upload time, and the status SHALL count it as verification time unknown. A replica that passed only a presence check SHALL NOT be reported as content-verified. The status is for the vault owner and SHALL NOT reach another audience.

The exact-path status SHALL read the current binding, replica row, damage authority and applicable key-export receipts. It SHALL use one ledger snapshot. Surviving exact damage evidence SHALL remain visible when a filing row is missing. The status SHALL read a surviving quarantine of the same stored version through its index. Missing damage or key-export evidence SHALL read as unknown. A surviving key-export receipt SHALL still establish custody when export coverage is incomplete. Paged coverage and copy progress SHALL report unknown while their source projection is incomplete. Status reads SHALL leave schema initialization to the writer. A writer that times out while another writer initializes the ledger SHALL report a retryable busy outcome.

The exact and paged views SHALL label a replica with only surviving decrypt proof as `decrypted`. The `verified-at-upload` label SHALL require upload proof. The transfer time and the time of a direct check SHALL remain separate. A successful decrypt SHALL leave an exact receipt even when every current filing of that version already has upload proof. That receipt SHALL survive a rebuild of the filing rows.

An empty ledger or binding rebuild SHALL NOT establish complete key-export history. A replica without surviving plaintext proof SHALL report unknown recoverability even with a confirmed key. Encryption from verified local plaintext and a successful exact decrypt establish that proof. A ciphertext copy or a reused version SHALL carry the surviving proof of its exact source. Confirming a ciphertext upload SHALL record its time independently of plaintext proof. Reusing a version SHALL leave a new filing's upload time unknown. For healthy filings that lack upload proof, a successful sample SHALL record one shared proof of their exact stored identity and update their recoverability counts without rewriting those filings. The sample SHALL credit a successful check time only to the sampled filing. A later sample SHALL NOT turn unknown upload verification into verified-at-upload status. Surviving findings SHALL still establish quarantine.

#### Scenario: A lost verification table

- **WHEN** the verification table is missing
- **THEN** the status shows every count and time as unknown
- **AND** a background rebuild restores the table from the bindings, and the status reports the counts once it finishes
- **AND** every replica then reads never checked until its next check

#### Scenario: A newer proof replaces an earlier proof

- **WHEN** a successful HEAD check follows a successful decrypt check of the same replica
- **THEN** the status counts that replica once at the level of the HEAD check
- **AND** when the successful proofs have the same time, the status counts the decrypt proof
- **AND** a failed attempt leaves the last successful proof in place

#### Scenario: A publication lands during a rebuild

- **WHEN** a publication replaces a replica after the rebuild read its binding
- **THEN** the rebuild keeps the published version and includes it in the counts it finalizes

#### Scenario: A finding reaches the owner

- **WHEN** a check quarantines a replica
- **THEN** the status counts it as quarantined, and the findings list names the original, the store, the failed level and the re-copy state
