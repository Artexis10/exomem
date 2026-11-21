# original-replica-custody Specification

## Purpose
Keep verified replicas of each original in configured stores, report how protected each original is, and make every verified replica available to repair.

## Requirements

### Requirement: Each store has a capability descriptor

The media store configuration SHALL declare each store in an optional `stores` list beside the per-vault `store` key, which names the store that serves eviction. An entry SHALL have a name, an adapter, the connection fields of its adapter and a capability descriptor of three fields:

- failure domain, a string;
- retrieval class, one of the protocol values `immediate`, `minutes` or `hours`;
- whether `head` recomputes the stored content, a boolean.

A store entry whose descriptor fails this schema SHALL be unavailable, and only that store. So SHALL a store entry whose connection fields are invalid, and a `media-edge` entry that is not the store serving eviction, because the edge is one connection for the process and has none of its own. The owner status SHALL name the missing or invalid field, or the reason. Code SHALL branch only on descriptor values, never on a store's name.

A vault entry with no list of stores SHALL read as one store, as it does today, and a vault entry that has the list SHALL keep its `store` key. That store's retrieval class is `immediate`, and its other descriptor fields are unknown unless the list gives them. An unknown field SHALL show as unknown in the status and SHALL NOT make the store unavailable.

#### Scenario: A complete descriptor is accepted

- **WHEN** the configuration declares a `filesystem` store with all three descriptor fields
- **THEN** the store is available for replicas
- **AND** the status lists its failure domain and retrieval class

#### Scenario: An incomplete descriptor is refused

- **WHEN** a store entry in a list of stores omits its failure domain
- **THEN** no replica is made in that store
- **AND** the status names the store and the missing field

#### Scenario: A malformed connection field

- **WHEN** a store entry gives a connection field that its adapter cannot read, such as a size limit that is not a number
- **THEN** only that store is unavailable, and the status names the reason
- **AND** the store serving eviction and every other store keep working

#### Scenario: A Cloud cell's existing configuration

- **WHEN** a cell's configuration names one `media-edge` store and no list of stores
- **THEN** the cell's stored-object pointers are the replicas of that store, with retrieval class `immediate`
- **AND** the status shows that store's failure domain as unknown

### Requirement: A supported filesystem adapter serves a local disk or a mounted NAS

The `filesystem` adapter, which also answers to the name `filesystem-test`, SHALL write versioned ciphertext objects under its root and SHALL expose no deletion. It SHALL NOT create its root. The root SHALL carry a store-identity marker that names the configured store and holds a random identity. Only an explicit owner declaration SHALL write the marker, and it SHALL refuse a root on the vault's own device. The declaration SHALL record the marker's identity in the media ledger, and a later declaration SHALL refuse a root whose marker holds another identity. When the root or its marker is absent, names another store or holds an identity other than the recorded one, the adapter SHALL refuse uploads, reads and `head`, and the store SHALL be unavailable. The adapter SHALL read the marker through the same held view of the root that the operation then reads or writes, so that a volume that goes away in between leaves an unavailable store and never an empty one. An unavailable store SHALL NOT be taken as proof that a replica is gone.

#### Scenario: An unmounted NAS is unavailable, not empty

- **WHEN** a `filesystem` store's mount point exists but the NAS is not mounted, so the marker is absent
- **THEN** no upload writes under the mount point
- **AND** its replicas keep their recorded state, and the status shows the store as unavailable

#### Scenario: A volume that leaves as an operation starts

- **WHEN** the volume of a `filesystem` store goes away after its marker was last read and before an upload, a read or a `head` holds its root
- **THEN** the operation reports the store as unavailable, a version is not reported absent, and nothing is written under the empty mount point
- **AND** a check quarantines no replica of that store

#### Scenario: Another disk answers to the same name

- **WHEN** a root's marker names the configured store but holds an identity other than the one that the declaration recorded
- **THEN** the store is unavailable and no upload reaches that root

#### Scenario: An owner declares a store

- **WHEN** the owner declares a store whose root is a mounted directory on another device
- **THEN** the root's marker names that store
- **AND** a root on the vault's own device is refused and gets no marker

### Requirement: An original's binding lists its replicas

An original's binding SHALL list each replica held outside the original's own path. The `stored_object` pointer SHALL stay the pointer of the store that serves eviction, with its present fields. A sibling key `replicas` SHALL list the replicas in every other store, each with the same fields as the pointer: store, object, version, key version, ciphertext size, ciphertext SHA-256 and format. The binding's logical list is the pointer, then `replicas`, earliest first. A binding with only a pointer SHALL read as a list with that one entry. A writer SHALL keep every entry it does not change. A release that does not read `replicas` SHALL still read the pointer. A replica in a store other than the one that serves eviction SHALL NOT change the original's state (`local`, `cached`, `evicted` or `missing`).

#### Scenario: A single pointer reads as one replica

- **WHEN** a binding written before this change holds one stored-object pointer
- **THEN** the original has one replica with that pointer's store, object, version and key version

#### Scenario: A release without replicas reads the binding

- **WHEN** a release that does not know `replicas` reads a binding that lists a second replica
- **THEN** it reads the same pointer and the same original state as before the replica existed

#### Scenario: A second replica keeps the first

- **WHEN** an original with a Cloud or legacy pointer gains a replica in a second store
- **THEN** the binding lists both replicas, with the earlier one first, the pointer unchanged and the new replica in `replicas`
- **AND** the original's state is unchanged

### Requirement: A replica is verified at upload and recoverable only with a surviving key

A replica SHALL count as verified at upload when both existing checks passed:

- the original's plaintext hashed to its binding's SHA-256 before encryption;
- the store confirmed the ciphertext size and SHA-256 of the published version.

Every published stored-object pointer meets both checks, because offload publishes a pointer only after the store confirms it. A replica of an evicted original is a copy of the ciphertext that the store serving eviction holds, hashed against that pointer's recorded digest; the plaintext checks were made at that upload.

A verified replica SHALL count as recoverable only when its key version's decryption identity survives the loss of the primary machine:

- On a personal install, the owner has confirmed an export of that identity outside the machine, once for each key version, and the media ledger records the confirmation.
- On a Cloud cell, platform key custody wraps the identity, and the configuration says so.

The owner status SHALL show every other replica of a personal install as present but not recoverable, and SHALL give the reason. A replica whose custody the configuration does not state SHALL show recoverability as unknown. Recoverability is a status about surviving the loss of the primary machine. It SHALL NOT stop fixity from repairing from a verified replica whose identity this install can read. A quarantined replica SHALL NOT count as recoverable, in a store's totals or in an original's view: it SHALL count as not recoverable, with the reason `quarantined`, whoever holds the key.

#### Scenario: A replica with an unexported key

- **WHEN** an original on a personal install has one verified replica in a second store, and the owner has not confirmed the key export
- **THEN** the status shows zero recoverable replicas and one replica that is not recoverable, with the reason "key not exported"
- **AND** the status reports the local original separately

#### Scenario: The owner confirms the export

- **WHEN** the owner confirms the export of the key version
- **THEN** the status counts that replica as recoverable

#### Scenario: A quarantined replica is not recoverable

- **WHEN** an original has two replicas with a confirmed export, and one is quarantined
- **THEN** the status counts one recoverable replica and one not recoverable replica with the reason `quarantined`, and the original's view shows the same for each replica

### Requirement: An owner operation copies originals to a store in bounded background batches

One owner operation, the `replicate` operation of `process_media`, SHALL make a replica in a named declared store for each original that lacks one. It SHALL refuse a caller that is not the vault owner. It SHALL run in bounded background batches and SHALL report the counts queued, copied, refused and remaining. It SHALL hold the vault mutation guard only for its short protection and publication commits, never while it hashes, encrypts or uploads. A retry after a lost reply SHALL reuse the recorded object name. It SHALL refuse the store that serves eviction, because offload fills that store, and a store that is unavailable. A repeat while a request for the store is running SHALL change nothing and SHALL return the current counts; once the request has finished, a repeat starts a new one. The worker SHALL check that a store answers as itself before it pins, hashes, encrypts or downloads for that store. A store that does not answer SHALL pause only its own queue, spend no attempt and write no refusal.

#### Scenario: Copying an archive to a NAS

- **WHEN** the owner copies 10,000 originals to a `filesystem` store
- **THEN** each original's binding gains one replica entry for that store
- **AND** foreground note writes proceed during the copy without a busy refusal caused by the copy

#### Scenario: An evicted original

- **WHEN** the owner copies an original whose bytes are not on the volume
- **THEN** the replica is the ciphertext that the store serving eviction holds, and the original stays evicted

#### Scenario: A repeat while the copy runs

- **WHEN** the owner repeats `replicate` for a store whose copy is partly done
- **THEN** the counts of copied originals stay and no new scan of the vault starts

#### Scenario: A store drops off during the copy

- **WHEN** a store's mount disappears while originals wait to be copied
- **THEN** the worker reads no original and encrypts nothing for that store
- **AND** its queued originals stay pending with their attempts unspent, and the other stores' queues go on

#### Scenario: A lost reply

- **WHEN** an upload lands and its reply is lost
- **THEN** the retry uses the same object name

### Requirement: The owner sees each original's protection

The owner status SHALL report these values, per store and in aggregate:

- replicas;
- verified replicas;
- recoverable replicas;
- distinct failure domains, unknown when a store that holds replicas declares none;
- the oldest verification time;
- the replicas whose last check proved `presence`, `content` or `decrypted`, and the oldest time of each;
- the replicas never checked since upload or since the last rebuild that lost their history;
- the quarantined replicas;
- the replicas whose last check or sample attempt could not complete and found no damage, by what it found (`binding_unreadable`, `binding_lists_no_replica`, `record_invalid`, `store_read_failed`, `key_refused`);
- the replicas of a store whose checks are not scheduled, with the reason;
- the sampled bytes spent this month against the budget, and the replicas skipped for size.

A per-original view SHALL page through results by path, and SHALL show each replica's last check, its level, any finding and any failed attempt. A paged list of findings SHALL name each quarantined replica's original, store, failed level, time and re-copy state. The aggregate SHALL be one bounded query over indexed verification state, and SHALL NOT walk the bindings. A value that the status cannot read SHALL show as unknown, never as zero. The status SHALL NOT read a store: it reports whether each store answered as itself as the background worker last observed it, with the time of that observation. A replica that is known not to survive and a replica whose custody is unstated SHALL be reported apart. A replica that a rebuild restored from its binding has no recorded upload time, and the status SHALL count it as verification time unknown. A replica that passed only a presence check SHALL NOT be reported as content-verified. The status is for the vault owner and SHALL NOT reach another audience.

#### Scenario: A lost verification table

- **WHEN** the verification table is missing
- **THEN** the status shows every count and time as unknown
- **AND** a background rebuild restores the table from the bindings, and the status reports the counts once it finishes
- **AND** every replica then reads never checked until its next check

#### Scenario: A finding reaches the owner

- **WHEN** a check quarantines a replica
- **THEN** the status counts it as quarantined, and the findings list names the original, the store, the failed level and the re-copy state

### Requirement: Stored replicas are checked on a cycle with the proof their store allows

The replica worker SHALL check every replica in a store with a declared descriptor on a cycle, with a default of 30 days. It SHALL run each check round after its bounded copy drain, whether or not that drain settled work, so that a copy backlog never holds checks back. The check SHALL use the store's `head`. For a store whose descriptor gives `head_recomputes: true`, the check SHALL be a content check: the returned size and SHA-256 SHALL equal the entry's ciphertext size and ciphertext SHA-256. For a store whose descriptor gives `head_recomputes: false`, the check SHALL be a presence check of the same values, and its result SHALL be labelled `presence`. A store whose descriptor does not state `head_recomputes` SHALL get no scheduled check, and the status SHALL report it as not scheduled, with the reason. Each round SHALL take, for each store, the replicas whose turn came longest ago first, never-checked first, through an index, a bounded pick at a time, until the store's share of the cycle (its replicas and, for a content check, its bytes, each times the round interval over the cycle) is spent or the round's time is. The stores of a round SHALL share one read-rate cap, and the round's time SHALL follow the byte shares of all its content stores together, so that the default settings check every replica of two stores of 500,000 replicas and 25 TB each within 30 days. The store that has waited longest, by its oldest due replica, SHALL go first, so that a round that ends early never leaves the same store last. A content check SHALL read under a read-rate cap, which bounds the average rate over time: an object is read at the store's speed, and only what remains of its share of time after the read is waited out. A read whose wait outlasts its round SHALL leave that wait owing, and a later round SHALL NOT read until it is paid. A read that fails after the store answered SHALL be charged to the cap as a read of the whole object, because the bytes it moved are not known; a read that the store refused before it read anything SHALL cost nothing. A check SHALL NOT hold the vault mutation guard. Code SHALL branch on the descriptor value, never on a store's or an adapter's name.

A replica whose binding cannot be read, that its binding no longer lists, or whose read fails once the store has answered as itself SHALL be recorded with what was found, SHALL NOT be quarantined, and SHALL be tried again after a bounded delay, not before; the round SHALL go on to the rows behind it. A store whose reads fail for three replicas in a row SHALL end its own round. A store that stops answering as itself during a round SHALL end its own round as an outage, with no finding recorded for the replicas it did not read and no replica put back for a day.

#### Scenario: A rotted replica on a NAS

- **WHEN** one byte of a replica's ciphertext changes in a `filesystem` store whose `head` recomputes content
- **THEN** the next check of that replica quarantines it, and the status reports the finding with the level `content`

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

#### Scenario: One unreadable binding

- **WHEN** one replica's binding names a version that the store cannot address, and the store holds other replicas that are due
- **THEN** the status reports that replica with the finding `record_invalid`, and the other replicas of the store are checked in the same rounds

#### Scenario: A store larger than one pick

- **WHEN** a store's share of a round exceeds one pick
- **THEN** the round checks the whole share, a pick after another, within its time

#### Scenario: Two stores of the sizing target

- **WHEN** two stores each hold 500,000 replicas and 25 TB, with the default cycle and read rate
- **THEN** a round reads both shares under the one cap within its time, and every replica is checked within 30 days

#### Scenario: A store goes away during a round

- **WHEN** a store stops answering as itself after the round has read some of its replicas
- **THEN** the round leaves that store, the replicas it did not read keep their turn, and the status records no finding for them

#### Scenario: A read larger than a round

- **WHEN** one content read takes more of the read rate than a round's time allows, and the next round is due
- **THEN** the next round reads nothing until the first read's time at the cap has passed, and the average rate over time stays at or below the cap

#### Scenario: A copy backlog and a replica that is due

- **WHEN** the copy queue holds more work than one drain settles, and a replica's check is due
- **THEN** the worker round that settled copies also runs the check round

### Requirement: A sampled check decrypts a replica within a monthly budget

The replica worker SHALL download, decrypt and hash sampled replicas, and SHALL compare the plaintext with the binding's `binary_sha256` and `binary_size`. Each store's sampled checks SHALL stay within a monthly byte budget. The descriptor MAY give it as `sample_bytes_per_month`. The default SHALL be 1 GiB, and zero SHALL turn sampling off. The media ledger SHALL record the bytes spent per store and calendar month in UTC. A sample SHALL take the replica whose sample turn came longest ago, never-sampled first, among those that fit the remaining budget, and the status SHALL count the replicas skipped for size. A sample SHALL charge a replica's bytes to the month before it downloads them, so that a sample that stops part-way is still paid for; a sample that the store refuses before it reads anything, because it cannot address the version, SHALL NOT be charged. A sample SHALL run only where checks are scheduled, by the same rule, and only for a replica that repair could read: a store whose retrieval class is `immediate`, and a key version whose identity this install can read. The status SHALL give the reason for any other replica, a store whose checks are not scheduled included. No sampled plaintext SHALL be written to disk.

A sample that cannot complete, or that the key refuses, SHALL be recorded as a finding that is not damage and SHALL NOT quarantine the replica. A binding that cannot be read now SHALL NOT mark the replica as one that no sample can read. A sample SHALL be tried again after a bounded delay.

#### Scenario: A sample proves the key

- **WHEN** a sampled replica decrypts and its plaintext hashes to the binding
- **THEN** the replica's last check reads `decrypted` with its time

#### Scenario: The budget is spent

- **WHEN** a store's sampled checks have used its monthly budget
- **THEN** no further sample reads that store until the next calendar month

#### Scenario: A replica the install cannot decrypt

- **WHEN** a replica's key version has no identity this install can read
- **THEN** it is never sampled, and the status gives the reason `identity_unreadable`

#### Scenario: A store with no declared descriptor

- **WHEN** a vault entry declares no list of stores, as a Cloud cell's does
- **THEN** no sample reads that store and no bytes are charged
- **AND** the status gives the reason that its checks are not scheduled

### Requirement: A failed check quarantines the replica, reports it and copies it again

A replica SHALL be quarantined only when its bytes are proved damaged. The ciphertext is damaged when its store answers that the version is absent or condemned (a hide or a newer version made the bucket rule delete it), or when the size or SHA-256 of the ciphertext that a read returns differs from what the entry records. The plaintext is damaged when ciphertext that matches its record decrypts to bytes that do not match the binding. A read that decrypts SHALL prove damage in one place. A sample and a repair, which may quarantine, SHALL hash the whole ciphertext even when decryption stops early; hydration and export quarantine nothing and SHALL stop downloading when decryption stops.

These SHALL NOT quarantine a replica: a store that is unavailable, does not answer as itself or times out; a decryption that a signal stopped; and ciphertext that matches its record while the decryption identity refuses it. The last is a distinct finding, `key_refused`: the status SHALL report it, a repair or copy of the original SHALL NOT be blocked by it, and it SHALL stay until a sample decrypts the replica or a later failed attempt records another finding in its place; a check that passes SHALL NOT clear it.

A quarantined replica SHALL NOT be a repair candidate. Damage is a fact of the stored version, whichever filings list it. A finding SHALL record the version in the derived ledger's damaged-version table, keyed by store, object and version, in the same transaction that quarantines the replica, and eviction SHALL NOT drop an original's local bytes while its pointer's version is in that table. A rebuild SHALL quarantine and queue each filing that lists such a version, whether or not an earlier row of it was lost. A rebuild SHALL NOT forget a recorded version: its walk skips a binding whose retirement is unconfirmed and never enters the Trash, so a version that no row lists may still be listed by a filing, and eviction trusts the record. Only the release of a plaintext mismatch removes it. The filings' rows and queued copies are projections of the records. A wrong plaintext is a fact of the ciphertext, so its authority is kept for the digest, in a table of its own, with a generation that every finding of a wrong plaintext of those bytes, on any version of them and through any filing, replaces with a new random value. A release SHALL read that generation when the sample picks and SHALL commit only if it is still current, in the transaction that releases; a publication SHALL run only if no finding recorded the version that it lists as damaged, by name or by ciphertext, since it was chosen; nothing releases or publishes on a filing's own request token alone. When eviction's own `head` of the store serving eviction does not match the pointer, or answers that the version is absent or condemned, eviction SHALL record that finding for the filing that holds the pointer, keep the local bytes, and queue the copy again; the status SHALL report the finding. A store without a scheduled check has no other way to find a lost version. The object's facts SHALL name one holder for each distinct pointer of a pooled object, so that the filing under eviction, which may hold another pointer, is never the one that decides whether the damaged pointer is recorded. Eviction SHALL read the table once more inside the transaction that unlinks the local bytes, so that a finding recorded while it asked the store keeps them. That read SHALL be one statement of primary-key probes for all of an object's pointers, because a pooled object collects a pointer for every filing and re-copy.

Offload SHALL NOT reuse a version that the table holds for a filing of equal bytes: it SHALL take the next live filing of equal bytes that holds another version, and else upload its own. An offload whose reuse candidate fails its `head` with a size or digest mismatch SHALL record that finding. A candidate that the store lost SHALL be recorded too when its filing has no bytes on the volume, because nothing else will find it and the version that offload publishes then heals it. Otherwise, and for a condemned candidate, it SHALL be passed over without a finding, as eviction of its filing releases it. Offload SHALL read the census once, take its list of candidates once, and walk that list once, passing over each candidate that the store does not confirm, and else upload its own: the cost of the walk is bounded by the candidates, and a retry SHALL NOT read the census or scan the candidates again. Offload SHALL read which candidate versions the table holds in one statement over one connection, however many candidates there are. An offload that skipped a damaged version SHALL name its filing on the version's record, so that the filings that still list it can copy from it. A reused version SHALL take the publication check below. The write of a filing's row SHALL inherit the damage of its version: when the table holds the version, or holds its ciphertext as a wrong plaintext, the row SHALL be written quarantined and its request queued, in the same transaction. A retry that uploads its own version SHALL drop the protections that an earlier attempt of the filing left on a reused version it does not publish.

The worker SHALL then write a new version into the same store. The source SHALL be the local original when its bytes hash to the binding (an original that is short, unreadable or of other bytes is no source, and the worker goes on to the next), or else another replica that is not quarantined and whose store has the retrieval class `immediate`, copied as ciphertext and hashed against its own recorded digest. The first replica of an evicted original SHALL take its source by that second rule, its pointer included, and SHALL be refused as `no_verified_source` when no entry of the binding verifies: a pointer that is recorded as damaged is never copied into another store. The publication SHALL replace exactly the quarantined entry in the binding, compared by object and version, under the vault mutation guard. Several filings may list the pointer of the store serving eviction, and the allowance counts their one version once. The finding that records the damaged version SHALL quarantine every other filing that lists it, with the same finding, and queue a request to copy again for each, in the same transaction: two statements over the filings that are not yet quarantined, read from an index that holds only those, however many filings share the version and however often a finding repeats. Each filing then keeps eviction's gate, the status and its request across a busy boundary, a restart and a rebuild. The request of such a filing SHALL first look for a replacement that another filing published, which the version's record names. It SHALL point to that replacement, and upload nothing, only when that filing's binding is read again under the vault mutation guard and names the same bytes, the version is neither damaged nor condemned, and the store confirms it; otherwise it SHALL upload its own. A filing whose bytes on the volume hash to its binding uploads its own copy under the current key version and SHALL NOT take a replacement of an older key, which this install may not decrypt; that decision follows the hash of the bytes, not the filing's state. A filing whose bytes do not verify, or that has none, takes the replacement of any key version, or copies ciphertext, which keeps its source's key version. A wrong plaintext is a fact of the ciphertext: every copy of those bytes under another object name decrypts as wrong, and a copy keeps the digest of its source. A finding of a wrong plaintext SHALL therefore record the ciphertext digest on the version's record, and the table SHALL answer for a stored version by its name and by that digest, one probe of each, however many versions are asked about. Each replica row SHALL carry the digest of its ciphertext, written by every row write and filled by the rebuild for a row of an earlier release, and indexed for the rows that are not quarantined. The same finding SHALL, in its own transaction, record as damaged with that cause the version of every filing of those bytes under any name and quarantine each with its queued copy, reading them from the row digest and from the finding's own version, and only those not yet quarantined, so that a finding that repeats costs the same however many filings hold the ciphertext, and so that a filing of an earlier release, whose row has no digest yet, is quarantined at the finding when it lists the version. A row is thus a projection of the records already when the finding lands, so the status, the findings list, the recoverability and the per-original view count such a copy as damaged without a rebuild. Every choice of a source, a replacement or a reused version SHALL use that one answer, the first replica of an evicted original included. A finding of another cause (an absent or differing ciphertext) is a fact of one stored version and is not carried to a copy, and it SHALL NOT clear a digest that the version holds. One publication check SHALL serve a reused version of offload and the copy again: under the vault mutation guard, before anything is written, it asks that answer for the stored version that the new entry reuses or publishes, and SHALL stop as busy, publishing nothing and leaving the request pending, when a finding recorded it since it was chosen. The media ledger is not held across the sidecar write, so a finding may still land between the check and the row. The write of the row of a published version SHALL therefore inherit it: when the table holds the version's digest as a wrong plaintext, the version SHALL be recorded as damaged with that cause, and its row written quarantined with its request queued, in the same transaction. A rebuild SHALL do the same for each filing whose entry has such a digest, in its last transaction. A finding that lands after the row transaction quarantines that row, through its digest, in its own transaction. A reused version is protected for the filing from the guarded re-read until the binding changes, and a reuse that stops first SHALL drop that protection. A worker that publishes a new version, which the media allowance's census has not seen, SHALL note it in the media ledger before its first write, so that an offload batch that holds a census reads it again before its next admission. Each request to copy SHALL carry a token that every write making it pending renews, and the worker SHALL settle, retry or close only the request whose token it picked, so that a finding recorded for the new version while the copy runs keeps its own queued request. When the replaced entry is the pointer of the store serving eviction and the new version keeps another object name, the old object name SHALL be released for collection before the binding changes; a retried copy that reuses the damaged name SHALL NOT release it. The damaged version SHALL stay in its store until then, and the finding SHALL name it. With no verified source, the replica SHALL stay quarantined, and the finding SHALL read `no_verified_source`. A rebuild SHALL make every request refused as `no_verified_source`, a first replica's as well as a copy again's, pending again, because a source may exist now: the original came back, another replica did, or a pointer found wrong was repaired. Check results SHALL live in the derived replica table. A rebuild SHALL keep them for a row whose object and version are unchanged.

A quarantine whose ciphertext matched its record and whose plaintext differed (`plaintext_mismatch`) SHALL stay due for a sample once per cycle, and a sample that decrypts to the binding SHALL release it and drop its queued copy. A finding that changes the cause of a version's record, from any cause to any other, SHALL in its own transaction move each quarantined filing of the version to the new cause, reading them from the version's own index, so that a row never says a cause that the version's record does not, and no release of the record can leave a quarantined filing with no record for the eviction gate. A repeated finding of the same cause SHALL read no filing. A release SHALL NOT delete a version's record while a filing of that version is still quarantined. A sample SHALL pick only a row that says a wrong plaintext, in the order of an index that holds only such rows, so that the pick costs the same however many filings a later finding moved on. It SHALL release only the damage that it picked, which the digest's generation names: a finding of a wrong plaintext of the ciphertext after the pick, on any version of it and through any filing, SHALL leave every filing of the ciphertext quarantined with its queued copy, and so SHALL a finding of another cause on the picked version, which moves the filing out of the rows that a sample may release. A release proves the ciphertext, so it SHALL also, in the same transaction, release every filing of that digest, under any object name, that is quarantined for a wrong plaintext, and drop their queued copies, so that none uploads a copy of bytes just proven good, and SHALL remove the records of a wrong plaintext of it; a filing that holds another cause keeps its row and its queued copy, and its record loses only the digest. It SHALL read those filings from the records of that digest, so that it costs the filings of its ciphertext however many other replicas are quarantined. A check SHALL NOT release it: the ciphertext matching its record proves nothing about the plaintext. A quarantine of absent, condemned or differing ciphertext SHALL stay until a new copy replaces its entry.

#### Scenario: The drill's damaged stored copy

- **WHEN** a replica in the store serving eviction is damaged while the local original is intact, and eviction or a check reads it
- **THEN** the pointer is quarantined and the status reports the finding
- **AND** the worker uploads a new version from the local original, and the binding's pointer names it

#### Scenario: An evicted original with a second replica

- **WHEN** the pointer of an evicted original is quarantined and a replica in another store is not quarantined
- **THEN** the worker copies that replica's ciphertext into the store serving eviction, and the pointer names the new version

#### Scenario: A version that two filings share

- **WHEN** two filings of equal bytes list one stored version, a check quarantines it for one of them, and the allowance equals that version's bytes
- **THEN** both filings are quarantined and queued before the worker writes, the worker uploads one new version, the second filing's request points to it, and the bytes counted against the allowance are unchanged
- **AND** an offload batch that held a census before the copy reads the census again at its next admission

#### Scenario: The worker stops after the first filing

- **WHEN** the worker publishes the new version for one filing and stops before the next filing's request runs
- **THEN** the next filing is still quarantined with its request queued, and eviction of it is refused until its request settles

#### Scenario: The replacement cannot be reused

- **WHEN** the filing that published the replacement was deleted, holds other bytes, or the store has condemned the version, before another filing's request runs
- **THEN** that request uploads its own version, and the filing never points to the condemned one

#### Scenario: A shared version after a key rotation

- **WHEN** two evicted filings share a damaged version, and the configured key version changed before the worker copies them from another replica
- **THEN** the worker uploads one new version, which keeps the key version of its source, and both filings point to it

#### Scenario: Equal bytes filed while a version is damaged

- **WHEN** a filing of equal bytes is offloaded while a finding holds a live filing's version as damaged, and the store's `head` still answers well
- **THEN** the offload uploads its own version, and the filing that holds the damaged version copies from it

#### Scenario: A filing that no row lists

- **WHEN** a filing lists a damaged version, no row of the table lists the filing, and the filing that held the finding is repaired
- **THEN** eviction of the filing is still refused, and the next rebuild quarantines it and queues its request

#### Scenario: A rebuild that skips a filing

- **WHEN** a rebuild's walk skips the one filing that still lists a damaged version, because its retirement is unconfirmed
- **THEN** the version stays recorded, and the next rebuild that reads the filing quarantines it

#### Scenario: One sample proves the version for every filing

- **WHEN** two filings share a version that a plaintext mismatch quarantined, and a sample decrypts it to the binding
- **THEN** both filings leave quarantine, their queued copies are dropped, and nothing is uploaded

#### Scenario: A finding lands while offload publishes

- **WHEN** a finding records the version that offload chose to reuse after the store confirmed it, before the binding is written
- **THEN** offload stops as busy and publishes nothing, and its retry uploads its own version
- **AND** a finding that lands between the binding's write and its row leaves the row quarantined with its request queued

#### Scenario: The first candidate was lost

- **WHEN** the first live version of equal bytes is gone from the store, its filing still has bytes on the volume, and nothing recorded it
- **THEN** offload reuses the next live version of those bytes, and uploads nothing

#### Scenario: A filing with local bytes and a replacement of an older key

- **WHEN** the replacement of a damaged version has an older key version than the configured one, and the next filing's original is on the volume and hashes to its binding
- **THEN** that filing uploads its own version under the configured key version

#### Scenario: A filing whose local bytes are damaged and a replacement of an older key

- **WHEN** the same filing's original on the volume no longer hashes to its binding
- **THEN** that filing points to the replacement of the older key, and the two filings stay one version

#### Scenario: The source of a copy is found damaged before it publishes

- **WHEN** a copy again has copied a replica, or chosen a replacement, and a finding records that version before the publication
- **THEN** the publication stops as busy, the binding is unchanged, and the request stays pending

#### Scenario: The source of a copy is found wrong while the copy publishes

- **WHEN** a copy again, or a first replica, has copied the ciphertext of an entry, and a finding of a wrong plaintext for that entry lands after the publication check, before the row is written
- **THEN** the binding lists the new version, and its row is quarantined with a wrong plaintext, with its request queued
- **AND** eviction does not trust the new version

#### Scenario: A replica copied from a version that is now found wrong

- **WHEN** a replica was copied from the pointer, and a finding of a wrong plaintext for the pointer lands afterwards
- **THEN** the pointer's own copy again does not copy the replica back, and its request reads `no_verified_source`
- **AND** the same finding quarantines the replica's filing with a wrong plaintext and queues its copy
- **AND** the status, the findings list and the per-original view count that replica as damaged and not recoverable before any rebuild
- **AND** a later finding of another cause on the pointer's version does not make the replica trusted

#### Scenario: Offload walks many lost candidates

- **WHEN** eight live versions of equal bytes are gone from the store and nothing recorded them
- **THEN** offload reads the census and the damaged-version table once, visits each candidate once, and uploads its own version

#### Scenario: A lost candidate of a filing with no bytes on the volume

- **WHEN** offload finds the version of an evicted filing gone from the store
- **THEN** it records the finding, uploads its own version, and the evicted filing's request points to it

#### Scenario: A pooled version that the store lost

- **WHEN** eviction of a pooled original finds its pointer absent or condemned
- **THEN** the local bytes stay, the pointer is quarantined, and a request to copy again is queued

#### Scenario: No verified source

- **WHEN** a quarantined replica's original is not on the volume and no other replica is usable
- **THEN** the binding is unchanged, and the finding reads `no_verified_source`

#### Scenario: A first replica of an evicted original whose pointer is found wrong

- **WHEN** a replica is requested for an evicted original whose only entry is a pointer that the table holds as damaged
- **THEN** no replica is written and the request is refused as `no_verified_source`

#### Scenario: A rebuild during quarantine

- **WHEN** the daily rebuild runs while a replica is quarantined and its entry is unchanged
- **THEN** the replica is still quarantined after the rebuild

#### Scenario: The identity is wrong

- **WHEN** a sample finds ciphertext that matches its record and the decryption identity refuses it
- **THEN** the replica is not quarantined, the status reports the finding `key_refused`, and no copy is queued
- **AND** after the identity is corrected, a repair or copy of the original succeeds and a later sample clears the finding

#### Scenario: A decryption is stopped

- **WHEN** the decryption process is stopped by a signal during a sample
- **THEN** no replica is quarantined and the sample is tried again later

#### Scenario: A sample is overtaken by a newer finding

- **WHEN** a sample of a plaintext-mismatch quarantine decrypts to the binding, and a finding of any cause is recorded for the same version through another filing before the sample settles
- **THEN** every filing of the version stays quarantined, and each queued copy stays

#### Scenario: A version that is now recorded as lost

- **WHEN** a filing is quarantined for a wrong plaintext, and a finding of another cause records the same version through another filing
- **THEN** each filing of the version reads the newer cause, a sample releases no filing of the version, and each keeps its queued copy

#### Scenario: A false plaintext alarm leaves a version that is found lost

- **WHEN** two filings share a version found wrong, a finding of another cause records the version, and a sample decrypts a copy of the same ciphertext under another name to the binding
- **THEN** the copy's filings are released and their queued copies dropped
- **AND** each filing of the version that is found lost stays quarantined with its queued copy

#### Scenario: A finding on a copy of the ciphertext after the pick

- **WHEN** two filings hold one ciphertext under different names, each with its own record, a sample picks one and decrypts it, and the other's wrong plaintext is then found again before the sample settles
- **THEN** the sample releases neither filing and removes no record, and both stay quarantined with their queued copies

#### Scenario: The pick of a quarantine to release beside many moved filings

- **WHEN** many filings of one version were quarantined for a wrong plaintext and a finding of another cause then moved them on, and one other filing is quarantined for a wrong plaintext
- **THEN** the sample's pick reads only the rows that say a wrong plaintext, and costs what it costs with few such filings

#### Scenario: A finding of a wrong plaintext that repeats beside many holders of the ciphertext

- **WHEN** many filings of one ciphertext are quarantined for a wrong plaintext, in one version or each in a version of its own, and a filing's attempt records the finding again
- **THEN** the finding reads none of the filings that it already holds quarantined, and costs what it costs with few of them

#### Scenario: A request refused for want of a source after the pointer is repaired

- **WHEN** a first replica of an evicted original was refused as `no_verified_source` because its pointer was found wrong, and a sample then releases the pointer, and a rebuild runs
- **THEN** the request is pending again, with no refusal, and the worker writes the replica without a new request

#### Scenario: A false plaintext alarm clears a copy under another name

- **WHEN** a sample of a plaintext-mismatch quarantine decrypts to the binding, and another filing holds a copy of the same ciphertext under another object name, quarantined for the same finding
- **THEN** both filings are released, their queued copies are dropped, and no record of the ciphertext is left

#### Scenario: A false plaintext alarm clears

- **WHEN** a quarantined replica's ciphertext matched its record, and a later sample of it decrypts to the binding
- **THEN** the replica leaves quarantine and its queued copy is dropped

#### Scenario: The new version is found damaged before its copy settles

- **WHEN** a copy again publishes a new version, and a check or eviction quarantines that version before the worker settles the copy
- **THEN** the new version's own request to copy stays queued, and the worker repairs it
