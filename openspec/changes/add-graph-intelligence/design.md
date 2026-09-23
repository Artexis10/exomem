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
An unavailable snapshot (disabled, warming, catching up) yields
`{available: false, reason: "graph_unavailable"}` and never a zero.

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
  eligible page. `by_status` splits them into `core_specific`, `core_generic`
  (`relates_to`), `extension`, `alias`, `deprecated`, `unregistered` and
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

### 3. Egress: admit, then count
A node is admitted when its page passes the caller's `release_walk_filter` predicate and
structural exclusion (the excluded tier, `_Governance/`, Records placeholders); unit nodes
inherit their page. An edge is admitted only when both endpoints and its authoring page
are admitted, so an edge authored on a withheld page never counts, even between two
visible pages. Every number is therefore a function of the caller's admitted subgraph,
and a test compares twin vaults that differ only in withheld material byte for byte. The
graph generation counts every write, withheld ones included, so it is reported only to an
unrestricted caller.

### 4. Surfaces
- `schema_memory(subject="relations", operation="census", detail="counts"|"keys")` with
  optional date bounds. `keys` adds per-predicate counts and unused extension keys.
- `exomem relations census` asks the managed service (`managed_service_target`, with the
  REST key) first, so it reads the live published snapshot over a read, never
  out-of-process index work. With `--vault`, `--sample`, or no answering service, it
  opens the sidecar read-only under the local release filter.
- Doctor adds one informational line, `relations.census`: pass when available, warn when
  not. Doctor is the owner's read-only preflight, so it counts the whole cohort and
  records no release receipts.

### 5. The judged sample
`--sample N` writes a seeded sample of specific authored edges, stratified by relation
family (one per family first, then by share), as refs only: page path and anchor at each
end. The owner's agent reads both ends and records one verdict per item: `precise`,
`too_specific`, `wrong_direction`, `wrong_predicate` or `should_be_generic`.
`review_item_context` has no edge-ref family, so the refs are read with `read_memory`.
`--judged FILE` folds the verdicts into counts with a 95% Wilson interval on the false
share; an unknown verdict is refused.

### 6. `infer` delegates its census
When the snapshot is current, `infer`'s census comes from the snapshot with its keys
unchanged, and on resolved targets its values match the Markdown count. Two cases keep
the Markdown count: the graph is unavailable, since `infer` stays non-authoring and must
answer; and a project-scoped call, since the snapshot records one project per page.

### 7. The observed-deletion guard protects registered vocabulary
The guard's purpose is to stop a save deleting an extension in use. It now protects the
canonical key of every observed label that resolves to a currently registered extension,
plus the alias when the label was one. Core labels and unregistered labels, in any case,
are nothing a registry save can delete, so they no longer block. The delta route,
`save-relations`, never consulted observed labels and is unchanged.

## Controls

| Control | What it prevents | Cost when it fires wrongly | Who pays |
|---|---|---|---|
| Census counts only admitted edges | counts, hops or ranks derived from withheld pages | a restricted caller sees a sparser graph than the owner | nobody: that is the caller's true view |
| Graph generation hidden from restricted callers | inferring withheld write activity | a restricted caller cannot pin a census to a generation | the restricted caller, in reproducibility only |
| Unknown verdict refused in `--judged` | a malformed file silently skewing the rate | the agent fixes one value | the agent |

## Risks / Trade-offs

- Graph and Markdown definitions differ at the edges: the graph drops unresolved body
  wikilinks and keeps one edge per target where Markdown kept one observation per anchor.
  The census is the definition going forward; `infer` keeps its keys.
- Two snapshot opens (census, then sample) can straddle a write. The sample records its
  own generation and registry hash.

## Measurement

The census is one pass over the snapshot's nodes and edges with in-memory reductions.
Measured on a synthetic vault (embeddings off, one WSL2 desktop): 0.19 s for 3,600 pages
and 20,291 edges, and 5.25 s after inflating the same snapshot to 10^6 edges, with a
process peak of 621 MB that includes building the graph. Both sit inside the design's
estimates (under 2 s, and 5-10 s).
