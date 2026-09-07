<!-- authority:non-specification -->

# Hosted database budget controls

Use this runbook before increasing hosted capacity or admitting more hosted
load. The audit reads Neon management metadata only; it does not connect to
PostgreSQL, wake compute, read credentials, or modify provider state.

## Preflight

Use a Neon CLI installation authenticated for the target project. The default
command is `neonctl`; provide a pinned executable explicitly when needed.

```bash
uv run --frozen python infra/scripts/audit_hosted_database_budget.py \
  --project-id "$NEON_PROJECT_ID" \
  --neonctl /path/to/neonctl
```

The command uses only these Neon v2 GET paths: the project, its endpoints, its
branches, and the organization spending limit derived from the returned
project. It writes a sanitized JSON report. Treat `fail` and `unknown` as a
failed preflight. An unknown result is incomplete provider evidence, not a
safe default.

The provider contracts are Neon's
[project API reference](https://api-docs.neon.tech/reference/getprojectdetails)
and
[organization spending-limit API reference](https://api-docs.neon.tech/reference/setorganizationspendinglimit).
The [branch-list API reference](https://api-docs.neon.tech/reference/listprojectbranches)
is paginated; a report with incomplete branch inventory is non-passing rather
than an assertion that one listed branch is the only branch.

The default control policy requires exactly one endpoint and one default branch,
minimum 0.25 CU, maximum no more than 1 CU, non-negative autosuspend timeout,
and a positive spending alert at or
below 500 cents. A timeout value of `0` is the provider default (currently five
minutes), rather than disabled autosuspend. Extra branches are reported; this
tool never deletes them.

## Read, modify, read

First run the preflight and retain its sanitized JSON result. Make an approved
capacity or alert change in Neon using the target project and the organization
derived from its identity-checked project API metadata. Re-run the same
preflight immediately afterwards and require a passing result before expanding
hosted load. The sanitized report intentionally does not emit the organization
identifier.

For the currently approved posture, set the one endpoint to 0.25 minimum CU,
1 maximum CU, provider-default autosuspend, and the organization alert to 500
cents. The alert configuration is not proof that an email was delivered; it
only causes Neon to notify at its configured thresholds. It does not suspend
compute.

## Optional monthly compute cutoff

A compute quota is a separate, provider-enforced whole-project control. To
verify an already approved 100-CU-hour monthly quota, run:

```bash
uv run --frozen python infra/scripts/audit_hosted_database_budget.py \
  --project-id "$NEON_PROJECT_ID" \
  --monthly-compute-hours 100
```

The audit checks `settings.quota.compute_time_seconds`: 100 CU-hours is
360000 CU-seconds. It never enables, changes, or removes the quota. Zero or a
missing quota is unlimited, and only a positive quota at or below the requested
limit passes this optional check.

Choose a hard quota only with explicit acceptance that exhaustion suspends the
shared database's compute. That stops hosted lifecycle, OAuth, billing and
gateway SQL until the next billing period or an operator changes the quota.
Storage and transfer remain billable, usage accounting can lag, and a compute
quota is not a total-dollar ceiling.

For Vercel-managed Neon organizations, a project-quota update can be rejected
with `action restricted; reason: "organization is managed by Vercel"`. Read the
project again after any rejected or ambiguous write. If the quota remains
absent, the hard stop is **not enabled**, even when the capacity ceiling and
spending alert pass. Resolve the restriction through the integration's account
administration or provider support; do not silently transfer the project,
change its billing plan, or substitute an unverified shutdown mechanism.
[Vercel Spend Management](https://vercel.com/docs/spend-management) excludes
Marketplace integrations, so its application-spend ceiling is not a substitute
for a Neon compute quota.

## Retained baseline

The capacity ceiling does not make this database idle-free. Existing worker
polling, SQL readiness checks and scheduled maintenance keep its compute active
at the 0.25-CU minimum. Reducing that baseline requires a separately approved
cross-service scheduler change; do not claim this budget control removes it.
