# Tasks

Each box carries the proof that closes it. Tests live in `tests/test_media_replicas.py` unless named otherwise.

## 1. Check state and scheduling

- [ ] 1.1 Add the check columns, the `quarantined` state and the `(store, checked_at)` index to `media_replicas`, with the heal path for an existing table, and the monthly sample ledger. Make the rebuild keep check results for an unchanged object and version. Prove that a rebuild keeps a quarantine and that a new version starts unchecked.
- [ ] 1.2 Add the check round to the replica worker: the descriptor picks content, presence or no check; picks are oldest first through the index; rounds are bounded in time and bytes under the read-rate cap. Prove the ladder per descriptor value, that an unavailable store changes no row, and that one pick on 1,000,000 generated rows reads the index with no sort (`EXPLAIN QUERY PLAN` and a timing).

## 2. Sampled check

- [ ] 2.1 Add `sample_bytes_per_month` to the descriptor parser (optional, default 1 GiB, zero is off) and the sampled decrypt check through the existing replica read into a hashing sink. Prove the budget stops samples for the month, a replica the install cannot decrypt is never sampled and gives its reason, and no plaintext file is written.

## 3. Quarantine, finding and re-copy

- [ ] 3.1 Quarantine on absence, size or digest mismatch and failed sample; never on unavailability. Make eviction quarantine and report a mismatch in the store serving eviction. Prove the drill: one byte flipped in a stored replica becomes a finding at the next check, and eviction of that original reports it.
- [ ] 3.2 Re-copy a quarantined replica into its store from the local original or another usable replica, replacing exactly the quarantined entry by compare-and-swap under the guard. Prove both sources, the `no_verified_source` finding, and that a binding edited meanwhile is not overwritten.

## 4. Fixity

- [ ] 4.1 Drop quarantined replicas from repair candidates, quarantine a replica that fails its hash during repair, and keep a verified copy that could not be put in place. Prove in `tests/test_original_fixity.py` that the retry after a failed Trash move reads the kept copy and makes no replica read.

## 5. Status and delivery

- [ ] 5.1 Extend the status, the per-original view and a paged findings list with the new values. Prove that a presence-only replica never reads as content-verified, that a lost table reads unknown and then never checked, and that the aggregate stays one grouped query.
- [ ] 5.2 Update `docs/` for the owner: what each level proves, the budget field and the findings. Run `openspec validate --all --strict` (1.14.0), the privacy gate and the scoped suites, then archive this change.
