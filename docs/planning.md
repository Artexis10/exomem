<!-- authority:non-specification -->

<!-- authority:implementation-reference -->

# Planning

Product and command usage reference derived from the OpenSpec contract. It is
not a repository task plan or a separate specification authority.

Planning is human-authored intended future state. A collection lives under
`Knowledge Base/Planning/`. File collections keep Markdown authority. An admitted
store collection keeps canonical rows and history in SQLite, with readable
Markdown views at the existing paths.

Use `plan_memory` with `describe`, `validate`, `inspect`, `create`, `query`, `add`,
`update`, `triage`, `revise`, `rebaseline`, or `history`.
Minimal capture creates a candidate work item in the authored
`inbox` horizon. `triage` is the explicit path for kind, status, priority,
commitment, horizon, area, and parent changes; time never moves an item.

Kinds are area, outcome, initiative, and work-item. Areas are ongoing
membership. The goal hierarchy is outcome → initiative → work-item. A vault
adds kinds, statuses, priorities, commitments, horizons, and health values
through `schema_memory(subject="planning-values")`: a status declares its
planning class (`open`, `done`, or `dropped`), and a kind declares the kinds
that may be its parent. Only a done or dropped status may be archived. Planning
may keep opaque Records evidence descriptors and opaque external execution
pointers, but `plan_memory` itself does not resolve them or infer progress.
Planning works standalone by default. A resolved user-authored workflow contract
may declare a companion owner for an execution artifact; the pointer remains
opaque, and neither the declaration nor the pointer proves tool availability or
external state.

Queries are bounded derived output, tagged with their canonical snapshot and
source versions. File collections expose direct editor changes immediately and
report audit gaps. The service applies valid store-view edits as governed
transitions and holds invalid or stale edits. Inspection never repairs files.
Dashboards, charts,
forms, and TUI workflows are intentionally deferred.

Store collections share the [publication, history and backup contract](records.md#collections-in-the-store)
with Records. SQLite commit acknowledges the save; item views, manifests and
aggregate views publish asynchronously. Structured queries see the save immediately.
`get_page` requires a valid physical view. `query(include_agent_history=true)` returns bounded audit
events. `history(plan_id=...)` returns retained prior payloads for one authorized
plan. Rebaseline acknowledges only an exact imported positive gap and preserves
its discontinuity. The store supports one lease holder, not two independent writers.

## Planned versus recorded

`review_memory(mode="plan-progress")` is the read-only review that resolves
those evidence descriptors. It selects active, committed items that carry
`progress_evidence`, `motivation`, or both, runs each bound Records saved view
through the ordinary governed read path, and presents authored intent next to
the exact number of records each view matched. Pass a Planning collection
selector as `path` to scope it, and `limit` to cap reviewed items.

Where an item carries `motivation` — a bounded list of `exomem://memory/`
references to the knowledge it rests on — the review also reports, per
reference, whether the vault still holds it and whether that page has since
been superseded. It names nothing it cannot show you: a reference the vault
does not hold, one it holds twice, a malformed one, and one whose page you may
not read all come back as the same bounded `motivation_unavailable`, with no
path, title, page count, or successor. A superseded citation is a prompt to
re-read the plan, not a verdict on it.

It measures and stops there. It never writes `health`, never mutates a plan or
a record, and never computes a score, ratio, percentage, or ranking — items are
ordered by identity, and divergence is a block of exact integers left to your
judgment. It reports counts, never rows: a bound view's declared aggregate is
withheld entirely, because `latest:` returns a whole record with its identity,
`distinct:`/`group:` return record values, and `avg:` returns a mean. The
matched count is the same under every aggregate, so nothing is lost.

An evidence target that is missing, withheld, wrong-profile, or names an
unknown view reports a bounded reason instead of a number. Missing and withheld
targets are deliberately indistinguishable, so the review cannot be used to
probe for hidden collections. `profile_mismatch` is the intended exception: it
is only reachable once the collection has already been released to you, so it
discloses nothing new and it catches a real authoring mistake — a plan pointing
its evidence at another plan. Do not collapse it into `collection_unavailable`.
