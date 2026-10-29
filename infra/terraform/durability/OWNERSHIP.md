# Durability ownership

Owns only private B2 recovery, user-export, and complete-database backup storage
plus purpose-scoped B2 identities. Recovery and database objects use governance
Object Lock; user exports intentionally do not, because product expiry drives
exact deletion and the 31-day lifecycle is only a cleanup backstop. It must never
own or reference the Hetzner server, network, firewall, Cloudflare ingress,
Kubernetes objects, or tenant application records. Production changes use the
`durability/` remote-state key.

The optional Cloud media bucket keeps all completed versions without Object Lock.
Its edge key can read and write that bucket, but cannot destroy versions or reach backup buckets.
The edge must filter operations because B2's `writeFiles` capability also permits hiding an object.
Enabling this Terraform option creates storage only; the separate media canary and fleet switches control offload.
