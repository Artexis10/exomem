# Platform chart ownership

Owns cluster-wide namespaces, Hetzner CSI, encrypted Retain storage policy,
Cloudflare Tunnel, Traefik policy, provisioner, external scheduler CronJobs,
admission policy, observability, and static SOPS secret references. It never
owns an individual tenant vault or Paddle/Neon application state.
