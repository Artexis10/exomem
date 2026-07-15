# Cell chart ownership

Owns one immutable-image Exomem StatefulSet, one retained 10 GiB PVC, one
Service, one native Secret projection, namespace quotas/policy, and opaque
cell-scoped routing labels. Values cannot select another cell's volume, Secret,
Service, namespace, image tag, or filesystem roots.
