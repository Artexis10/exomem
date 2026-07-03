---
tags: [operations, incidents, oncall, curated]
---

# Incident Severity Levels (SEV1–SEV4)

This handbook page lives OUTSIDE Knowledge Base/. It is curated, read-only
material kept in a sibling folder, used by tests to verify that a `scope="kb"`
query auto-widens to reach out-of-KB content.

## Severity definitions

- **SEV1** — critical: full outage or data loss, all-hands, immediate paging.
- **SEV2** — major: a core feature is broken for many users; page the on-call.
- **SEV3** — minor: degraded or partial impact with a workaround; handle in hours.
- **SEV4** — low: cosmetic or internal-only; handle in the normal queue.

Declare the severity from customer impact, not from how hard the fix looks. When
in doubt between two levels, pick the higher one and downgrade later.
