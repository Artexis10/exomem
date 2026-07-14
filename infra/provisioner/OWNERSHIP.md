# Provisioner ownership

Owns the durable `exomem-cell-provisioner.v1` API, external Neon schema,
idempotent operation worker, encrypted provider references, monotonic fences,
and reconciliation checkpoints. It stores no tenant knowledge, Paddle state,
email address, note metadata, filenames, snippets, or long-lived plaintext
credential.
