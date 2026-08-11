# Planning

Planning is human-authored intended future state. A collection lives under
`Knowledge Base/Planning/` and stores one readable Markdown item per plan; no
database, dashboard, or generated view is canonical.

Use `plan_memory` with exactly `inspect`, `create`, `query`, `add`, `update`,
or `triage`. Minimal capture creates a candidate work item in the authored
`inbox` horizon. `triage` is the explicit path for kind, status, priority,
commitment, horizon, area, and parent changes; time never moves an item.

Kinds are area, outcome, initiative, and work-item. Areas are ongoing
membership. The goal hierarchy is outcome → initiative → work-item. Planning
may keep opaque Records evidence descriptors and opaque external execution
pointers, but does not resolve them or infer progress. OpenSpec and the
repository remain software execution truth.

Queries are bounded derived output, tagged with their canonical snapshot and
source versions. Direct editor changes are immediately visible; inspection
reports audit gaps and never repairs human-owned files. Review, dashboards,
charts, forms, and TUI workflows are intentionally deferred.
