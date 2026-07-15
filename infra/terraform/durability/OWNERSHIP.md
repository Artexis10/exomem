# Durability ownership

Owns only private B2 recovery, user-export, and complete-database backup storage
plus purpose-scoped B2 identities. Recovery and database objects use governance
Object Lock; user exports intentionally do not, because product expiry drives
exact deletion and the 31-day lifecycle is only a cleanup backstop. It must never
own or reference the Hetzner server, network, firewall, Cloudflare ingress,
Kubernetes objects, or tenant application records. Production changes use the
`durability/` remote-state key.
