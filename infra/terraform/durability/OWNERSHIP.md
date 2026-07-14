# Durability ownership

Owns only versioned/Object-Locked B2 recovery storage, database-backup storage,
and purpose-scoped B2 identities. It must never own or reference the Hetzner
server, network, firewall, Cloudflare ingress, Kubernetes objects, or tenant
application records. Production changes use the `durability/` remote-state key.
