# Proposal: add-cross-domain-bridges

## Why

A protected source cannot semantically influence work outside its scope without
*some* representation crossing the boundary — pretending otherwise is dishonest.
The sanctioned crossing is a **bridge**: a reviewed derived representation that may
be used outside its source's restricted context. "Preserve proven process
boundaries during the first migration" can inform general engineering without the
client names behind it ever leaving the vault; "there is a health issue
contributing to fatigue; do not assume unlimited evening capacity" can inform
workload planning without the raw medical values.

This change makes bridges first-class so the release gate can release an
abstraction where it must withhold the raw source — turning "no cross-domain
disclosure" and "maximum useful connection" from a contradiction into a governed
workflow. It is the last core wave; the kernel, gate, tools, and receipts precede
it.

## What Changes

- A **bridge is an ordinary compiled note** (living where its type belongs) plus
  governance frontmatter — `bridge_of: [exomem://…]`, `bridge_scope: <scope>`,
  `bridge_review: <date>` — and an approval event.
- The release gate releases a bridge note per its own (usually open) scope **even
  when its `bridge_of` sources are restricted**, and strips provenance pointing
  into restricted scopes at egress (frontmatter `sources`, history, `## Relations`
  edges, and hit-level `graph.seed`/`relation_match`/`superseded_by`) so the
  bridge never leaks a restricted title.
- **Approval binds content**: a bridge approval is a `kind: release` grant carrying
  the note's `content_hash` at approval time; at release, `get_page`'s
  already-computed hash is compared and a mismatch withholds with `RELEASE_STALE`.
  An edited bridge requires re-approval — closing the "approve once, then edit in
  restricted quotes" laundering channel.
- **L2 constraint strings** are micro-bridges registered directly on a scope (no
  note ceremony) for the workload-constraint case.
- **Lifecycle**: `bridge_review` dates surface in the existing review/Inbox
  queues; restricting or deleting a source flags dependent bridges for re-review
  (never silent). Creation paths: manual authoring, LLM-proposed + user-reviewed
  (the existing propose→confirm write flow), deterministic templates, and
  promotion from a compiled note. An optional local model may *draft* an
  abstraction but never approves its own output — approval is the owner's confirmed
  write plus the approval event. No general-purpose LLM is required in core.

## Capabilities

### New Capabilities

- `cross-domain-bridges`: reviewed, content-hash-bound derived representations
  (bridge notes and L2 constraint strings) that the release gate may release
  outside a restricted source's scope with provenance into that scope stripped,
  with a re-review lifecycle tied to source changes.

### Modified Capabilities

- `attention-queue`: due bridge re-reviews and source-change-triggered re-reviews
  surface in the existing review queue.

## Impact

- Code: `src/exomem/governance/{policy,decisions,egress}.py` (bridge frontmatter,
  `kind: release` grants, hash-bound release, provenance stripping),
  `governance/tool.py` (bridge propose/approve/re-approve lifecycle);
  `src/exomem/commands.py` (`op_get` bridge annotation + provenance strip);
  review-queue integration for `bridge_review`.
- Tests: `tests/test_governance_bridges.py`; egress provenance-strip extension.
- Explicitly NOT in scope: automated abstraction generation quality (a local model
  is an optional non-approving assistant), preset bridge templates beyond the
  workload-constraint example (those ship with `add-professional-presets`).
