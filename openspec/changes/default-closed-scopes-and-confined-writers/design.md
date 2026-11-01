## Context

Governance decides each page from the scopes that select it. A page that no scope selects gets the open ceiling. The connector boundary (`governance/connector_boundary.py`) adds a host-owned floor per client binding. Today it lets a limited writer create only under the global `capture_paths`, and edit any existing page it may read if the result avoids its denied scopes. Without a connector config, nothing confines a non-owner writer.

## Goals / Non-Goals

**Goals:**
- Let the operator close unclassified material to every non-owner audience and fenced connector, with no reliance on agents citing sources.
- Confine every restricted writer, on every write door, to scopes the host config grants it, plus a private inbox.
- Keep a vault without a residual scope or `writable_scope_ids` byte-for-byte unchanged.

**Non-Goals:**
- Restriction that follows citations or links. An earlier draft walked declared derivation edges at read time. It protected only cited pages, opened a name oracle through the walk, and failed open on rename.
- Write permissions in policy documents. Vault content must not confer write authority.
- Exomem Cloud wiring. Cloud cells do not configure a connector boundary yet.

## Decisions

**D1. Residual scope, not a decision mode.** A scope document with `residual: true` and no selector or exclude fields is assigned when nothing else selects an item. It is an ordinary scope id, so rules, grants, release grants, connector denied sets, explain and private vocabulary instances work unchanged. A mode applied in the lattice to empty scope sets was rejected: denied sets and grants name scope ids, so it would need a parallel connector flag, a new explain shape and a new way to share, and an empty scope set would mean two things.

**D2. Exemptions.** The residual scope never selects a directory, so folders still collapse by their contents. Public vocabulary storage (overlays and registry history) counts as classified with no scope, as a v2 binding already answers; `instances.public_storage` gives the same answer for unarmed and v1 vaults. Nothing else is exempt: the skill and conventions are read without admission, `_Governance` is excluded structurally, and capture inboxes are classified by scopes.

**D3. Reach.** Non-owner audiences meet the residual scope through `default_deny`; the owner stays exempt. Fenced connectors meet it through their denied sets. The host refuses a connector config whose default denied set omits a declared residual scope. The personal connector opts in by omitting it from its client entry.

**D4. Write authority comes only from the host config.** A restricted writer is a resolved non-owner under a non-empty policy, or the owner through a connector with a non-empty denied set. Administrative ingress, an unfenced client, and every caller on a vault with an empty policy and no connector config are not restricted. Writable scopes come only from the matched client entry's `writable_scope_ids`; a caller with no entry has none. The host refuses writable scopes that are not policy scopes, meet the client's own denied set, sit on a client with an empty denied set, or are readable by the default context or another fenced client. The default context is therefore read-only. A rule or grant `write` field was rejected because vault content would confer authority.

**D5. Inboxes keep the global `capture_paths`.** A writer may create in a capture path when that path's membership is non-empty, lies within its writable scopes and is readable by it in full. Exclusivity (D4) makes each inbox private. For every writer, the owner included, a capture page's membership must equal its path's membership; this replaces "visible under the default denied set" in `require_write` and `verify_capture_namespaces`. Per-client `capture_paths` were rejected: an inbox scope must exist anyway, and the requirement format would need a version bump.

**D6. One gate.** `connector_boundary.write_ceiling(root, who)` feeds `require_create` and `require_write`. Outside an inbox, a write is admitted only for an existing page that the writer reads in full and whose scope set stays non-empty and within its writable scopes, before and after the write. A move destination and a reclassified path are judged as creations. Every refusal is `WRITE_REFUSED: target is unavailable`, decided before any stat or name allocation, so withheld, absent and read-only targets answer alike. `_Schema/**` needs no special case: its pages are unscoped or residual, which are never writable.

**D7. Source and recap destination.** For a restricted writer, `add.admit_source_destination` places every Source capture under `<inbox>/<kind folder>`. The inbox is the first capture path, in sorted order, where the writer may create; with none, the capture is refused. One recap-folder helper replaces the hard-coded episodes folder in its consumers, so recaps in an inbox are listed and superseded only there. Recaps the owner records stay in the shared episodes folder and take the residual scope.

**D8. No projector change.** v4 publication and read time call the same membership functions, so they receive the completed membership. A policy with a residual scope has a fingerprint that no earlier binary compiles, so no stale namespace exists.

**D9. Caches stay page-local.** Completion depends only on the selected scopes, the registry binding, the policy fingerprint and whether the path is a directory. Page-local admission verdicts stay valid, and the write gate adds no cache.

## Risks / Trade-offs

- [Fenced connectors lose write-down into open pages and unscoped inbox creation on upgrade] → Deliberate; the scaffold skill documents inbox scopes and `writable_scope_ids`.
- [Owner capture writes in an armed vault become stricter: metadata that changes a capture page's scopes is refused] → Deliberate; the invariant keeps every inbox private.
- [A scope document edited outside Exomem, for example by sync, can declassify residual pages while serving] → The requirement pins only protected documents; pinning every scope would force stopped maintenance for every classification. Reported as a known limit.
- [Two connectors that share one episode key could shadow each other in recent context] → Recaps are grouped per audience token; reported as a known limit.
- [An older binary refuses `residual`] → It blocks the policy, which fails closed.
