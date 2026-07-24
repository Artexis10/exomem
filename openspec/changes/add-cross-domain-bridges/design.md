# Design: add-cross-domain-bridges

## Context

Bridges reuse existing machinery. A bridge note is authored through the normal
compiled-note write path (`remember`/`replace_memory`) — approval *is* the owner's
confirmed write plus an approval event, so no new authoring surface is needed. The
content-hash bind reuses `get_page`'s already-computed `content_hash`
(`get_page.py:137`) — the check is a zero-extra-IO string compare. Provenance
stripping extends the release gate's projector. Re-review reuses the review/Inbox
queues (`attention.py`, review studio). L2 constraint strings are scope-registered
micro-bridges the evaluator already understands (level L2 in the ladder).

## Goals / Non-Goals

Goals: release an abstraction where the raw source is withheld, with provenance
into the restricted scope stripped; bind approval to content so an edited bridge
re-reviews; a re-review lifecycle tied to source changes; honest handling of the
"some representation must cross" reality.

Non-Goals: automated abstraction generation (a local model may draft but never
approve), a general-purpose LLM in core, preset bridge template libraries (later),
enforcement redesign (the gate from `add-release-gate` is reused).

## Decisions

### D1 — Bridge = compiled note + governance frontmatter + approval grant
Frontmatter `bridge_of: [exomem://…]`, `bridge_scope`, `bridge_review`. The note's
own scope membership (usually none/open) governs its release; the evaluator
releases it even when `bridge_of` targets are restricted. Approval is a
`kind: release` grant in `_Governance/grants/` carrying `{path, content_hash,
to_audience, options: {strip_provenance: [...]}, released_at, why}`.

### D2 — Approval binds content_hash
At release, `get_page`'s computed hash is compared to the grant's
`content_hash`; mismatch → withhold with `RELEASE_STALE` (re-approval required).
Zero extra IO (the hash is already computed). This kills the approve-then-edit
laundering channel: new restricted content cannot ride an old approval.

### D3 — Provenance stripping covers all channels
For a released bridge, strip: frontmatter `sources`/`evidence`, history, and
`## Relations` edges pointing into restricted scopes (in `op_get`/`annotate_page`),
and hit-level `graph.seed`/`relation_match`/`superseded_by`/`parent_ref` naming
restricted paths (in the gate's `annotate_hits`). Provenance is metadata, not
content, so it is stripped at annotation, not in the raw serializer.

### D4 — L2 constraint strings are scope-registered micro-bridges
For the "do not assume unlimited evening capacity" case, a constraint string is
registered directly on a scope and released at L2 wherever the rule allows — no
note ceremony, no `bridge_of` note. The evaluator already has L2 in the ladder;
this change wires the constraint text through the projector.

### D5 — Re-review lifecycle on the existing queue
`bridge_review` dates surface as due items in the review/Inbox queue; restricting
or deleting a `bridge_of` source flags dependent bridges for re-review (the bridge
is the owner's own file — never silently deleted or altered). Approval and expiry
are receipt events (from `add-disclosure-receipts`) with provenance back to source
content hashes.

## Risks / Trade-offs

- **Approval washing via embedded quotes**: a bridge body could paste restricted
  text. Mitigation: approval binds `content_hash`, so the owner reviews the exact
  bytes; a later edit re-reviews. The system cannot prevent the owner from
  approving a leaky abstraction — that is a human review responsibility, made
  visible, not automated away.
- **Local-model drafting quality**: out of scope for correctness — a draft is a
  suggestion the owner must approve; the non-approving-self rule is a hard
  constraint.
- **Source deletion vs dependent bridge**: flag for re-review, never cascade-
  delete the owner's authored bridge.

## Migration Plan

Additive frontmatter + grant kind. Existing bridge-shaped notes gain governance
only when the owner approves them. No data migration.

## Open Questions

None blocking. Preset bridge templates (medical workload-constraint, legal
de-identification) land with `add-professional-presets`.
