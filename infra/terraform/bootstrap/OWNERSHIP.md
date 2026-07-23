# Legacy B2 backend bootstrap ownership

This is the already-applied bootstrap for the versioned B2 Terraform-state
bucket and its scoped state identities. It remains unchanged only so Terraform
continues to track the live bucket and keys until a separately reviewed cleanup.

Do not use this root for new state, do not repurpose its state for HCP resources,
and do not run it as part of normal foundation or durability work. The active
state authority is bootstrapped separately by `../hcp-bootstrap`.
