# Cell-data ownership

Owns only the Backblaze B2 cell-data member account's storage and keys: the locked
cell backup bucket, the export and import staging bucket, and three purpose-scoped
application keys. It never owns another account's resource, the Hetzner server,
Cloudflare, Kubernetes objects, or tenant records. Production changes use the
`cell-data/` remote-state key, and its credentials belong to the member account only.

The backup bucket has compliance-mode Object Lock. Its lifecycle expires only
noncurrent versions, so a current restic pack never expires. A compliance lock
cannot be shortened or removed, so a longer `backup_retention_days` is hard to undo.
The staging bucket has no lock; its lifecycle bounds the life of each object.

The key custodian key creates keys, and the provider cannot limit it to a bucket,
so it can mint any capability within this account. The out-of-cluster key watcher
(`infra/scripts/b2_key_watcher.py`) pins its key ID, the lock and lifecycle
settings, and every per-cell key shape. A change to a key capability or a bucket
setting here needs a matching change to the watcher's pinned set. The `cellctl`
keys cannot create keys: one reaches only the staging bucket, and one only lists
backup files.

Keys are created here only for the three roles above. The watcher's own list-only
keys, and the key that applies this root when it is not the account's master key,
are operator-created and are listed in the watcher's pinned set.
