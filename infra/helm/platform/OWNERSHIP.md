# Platform chart ownership

Owns cluster-wide namespaces, Hetzner CSI, encrypted Retain storage policy,
Traefik policy, external scheduler CronJobs, admission policy, observability,
Exomem Cloud's cellctl and gateway workloads, and static SOPS secret
references. It never owns an individual tenant vault or Paddle application
state.

The optional memory health runner uses a dedicated synthetic cell. The chart
owns only `exomem-memory-health-ingress` in that cell's pre-existing namespace.
This policy admits the distinct platform runner label on TCP 8765. The chart
does not change cellctl's runtime policy, customer cell ingress or admission.

The platform deployment owner provisions the synthetic cell, mutable page,
bearer, restricted database role and Secrets. The Source defaults keep the
runner, public health rewrite and HTTP health jobs disabled. The gateway image
must contain the reviewed memory runner before the owner enables it in an
approved rollout window. This Source change grants no rollout authority.
