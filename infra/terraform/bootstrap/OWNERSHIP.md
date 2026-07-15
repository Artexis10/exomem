# Backend bootstrap ownership

One-time bootstrap for the versioned B2 Terraform-state bucket and its scoped
state identities. It uses local state only during bootstrap, produces an
encrypted escrow artifact, and is not part of normal foundation/durability
planning. Re-running against an existing bucket must converge without replacing
or deleting it.
