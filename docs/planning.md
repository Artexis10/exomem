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
pointers, but `plan_memory` itself does not resolve them or infer progress.
OpenSpec and the repository remain software execution truth.

Queries are bounded derived output, tagged with their canonical snapshot and
source versions. Direct editor changes are immediately visible; inspection
reports audit gaps and never repairs human-owned files. Dashboards, charts,
forms, and TUI workflows are intentionally deferred.

## Planned versus recorded

`review_memory(mode="plan-progress")` is the read-only review that resolves
those evidence descriptors. It selects active, committed items that carry
`progress_evidence`, runs each bound Records saved view through the ordinary
governed read path, and presents authored intent next to the exact number of
records each view matched. Pass a Planning collection selector as `path` to
scope it, and `limit` to cap reviewed items.

It measures and stops there. It never writes `health`, never mutates a plan or
a record, and never computes a score, ratio, percentage, or ranking — items are
ordered by identity, and divergence is a block of exact integers left to your
judgment. An evidence target that is missing, withheld, wrong-profile, or names
an unknown view reports a bounded reason instead of a number, and missing and
withheld targets are deliberately indistinguishable.
