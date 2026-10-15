# Design

Reuse the HTTP scheduler, bundled memory runner, persisted proof table, Traefik and existing rollout process.
Add no controller, graph engine or generator.

The canonical schedule file supplies each job's cadence. Helm follows its repository symlink and packages the actual bytes.
One helper selects enabled jobs for workloads, state, permissions and observability. Disabled jobs cannot generate missing-run alerts.
Existing job schedules and alert margins remain unchanged.

The memory job uses the configured gateway image, restricted database function and one synthetic cell's bearer and mutable page.
Its distinct label grants no gateway access. Its network policy allows only the selected cell, DNS and configured database addresses.
A separate chart-owned ingress policy admits the runner only to that synthetic cell. The namespace must already exist.
The controller retains its existing customer policies and admission rules.

Public GET `/healthz` rewrites to `/healthz/canary` through the existing trusted ingress and TLS route.
Direct pod liveness `/healthz` and readiness `/readyz` retain their meanings.

The deployment owner provisions the cell, page, credentials and database roles before enabling the reviewed jobs.
Missing observations remain unavailable and cannot gate customer admission or restart customer pods.
Rollback disables the jobs and functional route; persisted observations expire naturally.
