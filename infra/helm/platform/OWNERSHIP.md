# Platform chart ownership

Owns cluster-wide namespaces, Hetzner CSI, encrypted Retain storage policy,
Traefik policy, external scheduler CronJobs, admission policy, observability,
Exomem Cloud's cellctl and gateway workloads, and static SOPS secret
references. It never owns an individual tenant vault or Paddle application
state.
