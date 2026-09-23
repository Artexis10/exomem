## Context

The graph sidecar already holds what a census needs. File nodes carry `page_type`,
`lifecycle_status`, `access_tier`, `origin_date`, tags and entity-type metadata; edges
carry `relation_type`, `raw_relation`, `registry_status`, `origin`, `source_path` and the
resolver's source and target kinds. `infer` nevertheless counted relations by parsing
every page. The constitution holds throughout: the server measures, the agent reasons;
no model runs in the census, and the optional judged sample is judged by the owner's
agent, locally.

## Goals / Non-Goals

**Goals:** one reproducible, counts-only census whose definitions are fixed, so two runs
compare; honest under a release policy; cheap enough to run on a large vault; and an
`infer` save that stops tripping on labels it cannot delete.

**Non-Goals (group 1):** vocabulary standing (provisional, common, dormant) and
near-duplicate groups, which the vocabulary loop derives; weekly cohorts; any
request-path use of the census.

## Decisions

### 1. The census reads the snapshot, not Markdown
It opens one `_open_read_snapshot()` and reads the relation and entity-type registries.
The census itself parses no Markdown. Two readers around it do touch page bytes: under a
governed policy the owner's release filter reads each page to decide its release, and
`infer`'s fallback count parses Markdown when the graph is unavailable. An unavailable
snapshot (disabled, warming, catching up) yields
`{available: false, reason: "graph_unavailable"}` and never a zero.

Edge rows are reduced as they stream from SQLite, and the two checks that compare an edge
with its reverse (`directed_both_ways`, `inverse_duplicates`) run as `GROUP BY` aggregates
in SQLite over the admitted population, so Python memory follows pages, not edges. The
sample keeps, per family, only the `N` edges with the smallest seeded hash of their
identity, which is one pass and a uniform, reproducible draw.

### 2. Definitions
- **Eligible page**: an admitted Knowledge Base file node that passes the shared
  relation-debt predicate (compiled page types, not a navigation page, not an inactive
  status, the read-write tier). Sources and Evidence are targets, never the cohort. An
  optional `date_from`/`date_to` scopes the cohort by recorded origin date.
- **Structural edges are not relations.** The indexer mints a `derived_from` edge from
  every unit or block to its own page (`semantic_unit`, `semantic_block` origins). The
  census skips them everywhere; they would otherwise inflate specific counts and connect
  every page to itself.
- **Authored edges**: origin `semantic_relation` or `markdown_relation`, authored on an
  eligible page, between admitted indexed nodes. Rows whose target is missing are
  `unresolved_target_edges` instead. `by_status` splits them into `core_specific`,
  `core_generic` (`relates_to`), `extension`, `alias`, `deprecated`, `unregistered` and
  `scope_violation`. `generic_share` is `core_generic / (authored - unregistered)`.
- **Typed edge**: a registered edge that is not `links_to` and not a wikilink, from any
  authoring origin including frontmatter (`sources:` gives `derived_from`). Typed coverage
  counts eligible pages with a typed edge to another page; specific coverage excludes
  `relates_to`.
- **Disconnected**: an eligible page that authored no edge of any origin to another page.
  **Isolated** pages also receive none.
- **Structural checks** run over the typed edges authored on eligible pages. Each reports
  `applicable` and `violations`, so a check that cannot fire is not a pass; a placeholder
  endpoint makes an edge inapplicable. `directed_both_ways` is reported for inspection
  only.
- **Unmeasured until the vocabulary loop lands**: extension standing,
  `near_duplicate_groups`, and `false_precision_judged` without a judged file. Inverse
  duplicates use registered `inverse` keys now.

### 3. Egress: the census is served whole (the D15 precedent)
Link resolution runs against the whole vault's resolver, so a withheld page changes how a
visible page's bare link resolves: a withheld page with the same stem as a visible target
makes the link ambiguous, a withheld page whose stem equals a visible page's title wins
the resolution over the title, and a withheld page with the same title splits the title
match. The snapshot records only the resolved edge. No filter over the snapshot can
recover what the link would have resolved to without the withheld page, so admitting
nodes and edges by the caller's release filter is not enough: counts still move.

The ruling follows D15 (global structures computed with withheld edges cannot be repaired
per item). The census, its sample and `infer`'s census counts are served only as a whole
view:

- under an empty governance policy, to every caller;
- under a non-empty policy, only to an owner-bound principal (resolved, owner audience);
- to every other audience, `{available: false, reason: "audience_restricted"}`, decided
  from the principal and policy before the snapshot or any page is read, so a restricted
  caller also pays no release-filter cost;
- to the owner under a policy that does not compile, `policy_blocked`, not a zero.

Within the served view the admission rules still hold: a node is admitted when its page
passes the release filter (which, for the owner, excludes only tombstones) and structural
exclusion (the excluded tier, `_Governance/`); unit nodes inherit their page; an edge is
admitted only when both endpoints are admitted indexed nodes and its authoring page is
admitted. Placeholders are never admitted: the row is not a connection and counts once in
`unresolved_target_edges`. The graph generation is reported to every caller the census
serves, the owner under a governed policy included.

**For group 2.** The resolution channel is a whole-graph property of how the graph is
built, not of how it is read. G2's admission kernel cannot solve it by filtering either:
a restricted audience's multi-hop results need edges resolved as if withheld pages did
not exist, or a proof that resolution did not depend on them. The kernel's rule that "a
placeholder is admitted when the edge that names it is admitted" has the related
missing-versus-withheld channel. G2 must solve both before serving restricted audiences.

### 4. Surfaces
- `schema_memory(subject="relations", operation="census", detail="counts"|"keys")` with
  optional date bounds, refusing `limit` and every write argument. `keys` adds
  per-predicate counts and unused extension keys.
- `exomem relations census` asks the managed service (`managed_service_target`, with the
  REST key, which is the owner's credential) first, so it reads the live published
  snapshot over a read, never out-of-process index work. When the service refuses the key
  it prints one line saying so. With `--vault`, `--sample`, or no answering service, it
  opens the sidecar read-only as the owner-local caller (`library_scope`).
- Doctor adds one informational line, `relations.census`: pass when available, warn when
  not. Doctor is the owner's local preflight and declares the owner-local caller.

### 5. The judged sample
`--sample N` writes a seeded sample of specific authored edges, stratified by relation
family (one per family first, then by share), as refs only: page path and anchor at each
end. The default file is `relation-census/sample.json` in the vault's machine-local state
directory, never the current directory. The owner's agent reads both ends and records one
verdict per item: `precise`, `too_specific`, `wrong_direction`, `wrong_predicate` or
`should_be_generic`. `review_item_context` has no edge-ref family, so the refs are read
with `read_memory`. `--judged FILE` folds the verdicts into counts with a 95% Wilson
interval on the false share; an unknown verdict is refused.

### 6. `infer` delegates its census
When the snapshot is current, `infer`'s census comes from the snapshot with its keys
unchanged. An authored row counts in `relation_counts` and `zero_authored_relation_rows`
whether or not its target resolves, which keeps the Markdown values.
`zero_body_connections` changes meaning: it counts pages with no wikilink or relation row
that resolves to another page in view, where the Markdown count asked whether any
wikilink text appeared at all. Two cases keep the Markdown count: the graph is
unavailable, since `infer` stays non-authoring and must answer; and a project-scoped call,
since the snapshot records one project per page. A caller the census refuses gets the
refusal in the census field on every path, the Markdown one included, because the
Markdown count is not release-filtered.

### 7. The observed-deletion guard protects registered vocabulary
The guard's purpose is to stop a save deleting an extension in use. It now protects the
canonical key of every observed label that resolves to a currently registered extension,
plus the alias when the label was one. Core labels and unregistered labels, in any case,
are nothing a registry save can delete, so they no longer block. The registry's
meaning-continuity check already refuses dropping a used key or alias, so the guard is
defence in depth. The delta route, `save-relations`, never consulted observed labels and
is unchanged.

## Controls

| Control | What it prevents | Cost when it fires wrongly | Who pays |
|---|---|---|---|
| Census served to the owner only under a governed policy | counts moved by withheld pages through link resolution | a restricted audience gets no census | nobody on the owner's vault today, whose policy is empty; later, restricted audiences, who never needed edge-quality numbers |
| Census counts only admitted edges | counts derived from edges authored on or pointing at pages outside the view | the served view is the owner's true view | nobody |
| Unknown verdict refused in `--judged` | a malformed file silently skewing the rate | the agent fixes one value | the agent |

## Risks / Trade-offs

- Graph and Markdown definitions differ at the edges: the graph drops unresolved body
  wikilinks, so `zero_body_connections` now means "reaches no other page".
  `relation_counts` and `zero_authored_relation_rows` keep the Markdown values.
- Two snapshot opens (census, then sample) can straddle a write. The sample records its
  own generation and registry hash.
- A restricted audience loses the census entirely until G2 solves the resolution channel.

## Measurement

Measured in a fresh process on a synthetic 3,640-page vault (embeddings off, one WSL2
desktop), through the real snapshot: 6.6 s at 10^5 edges and 13.1 s at 10^6 edges, with a
peak RSS of 73 MB in both, so memory no longer grows with edges. About 6.7 s of each is the
snapshot's availability proof over the page corpus, which every graph reader pays. The
reduction alone, at 10^5 file nodes and 10^6 edges with the proof bypassed, takes 11.0 s
at a peak RSS of 132 MB, against 9.9 s and 702 MB when every edge was held in memory. The
census is operator-invoked and never runs on a request path.
