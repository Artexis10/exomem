# Proposal

This change verifies stored replicas of originals on a schedule. A replica that fails a check is quarantined, the owner sees a finding, and the product copies the replica again from a verified source.

## Why

`add-original-replica-custody` lets an original keep replicas in several stores. Each replica is verified once, at upload. Nothing reads it again until fixity needs it to repair a damaged local original. A drill on a disposable vault (20 Nov 2026) showed the gap: a damaged stored copy produced no finding. The filesystem adapter's `head` rehashes the object, so eviction refused without a word. The media edge's `head` returns the digest declared to the provider, so it cannot see damage at all. A replica can therefore rot for years and read "verified at upload" until the day the owner needs it.

A second, smaller gap: when fixity verifies a replica but cannot put it in place, the retry downloads the same replica again.

## What Changes

- A background check reads each replica on a cycle, with the strongest proof that its store's descriptor allows. A store whose `head` recomputes content gets a content check of the ciphertext against the binding's recorded digest. Any other store gets a presence check of the object's size and declared digest.
- A sampled check downloads a replica, decrypts it and hashes the plaintext against the original's admission SHA-256. It runs within a monthly byte budget per store. This is the only check that proves the key still opens the replica.
- A replica that is absent, or whose size, digest or plaintext does not match, is quarantined. Fixity and eviction stop trusting it, and the owner status reports a finding. The worker then copies the original into the same store again, from the local original or from another verified replica, and the binding names the new version. With no verified source, the replica stays quarantined, and the finding says so.
- Eviction that finds a mismatch in the store serving eviction now quarantines that replica and reports the finding, instead of refusing silently.
- The owner status adds what each check proved, when, the quarantined replicas and the replicas never checked.
- Fixity keeps a replica that it verified but could not put in place, so the retry reads the kept copy and does not download the replica again.
- A store without a declared descriptor, such as a Cloud cell's single `media-edge` store, gets no scheduled check: its descriptor does not say what a check can prove. A Cloud cell starts checks when its configuration declares the store, which is a separate rollout.

This change adds no Cloud `restic check` job, no B2 adapter and no storage policy. Later changes add those.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `original-replica-custody`: scheduled checks of stored replicas, a sampled decrypt check under a budget, quarantine with owner findings and a re-copy, and the extra status values.
- `original-fixity`: repair skips quarantined replicas, quarantines a replica that fails its hash, and reuses a verified copy that it could not put in place.

## Impact

- Code: `media_replicas.py` (check, quarantine, re-copy and status), `media_store.py` (eviction quarantine), `original_fixity.py` (candidate filter and kept copy), `originals.py` (one optional descriptor field).
- State: new columns in the derived `media_replicas` table, and a monthly sample ledger in the media ledger. The binding format is unchanged.
- Cost: a presence check is one `head` request per replica per cycle. A content check reads each replica's bytes once per cycle, under a read-rate cap. A sampled check downloads at most the store's monthly budget.
