## Context

The graph substrate already indexes wikilinks, canonical note relations, semantic-block relations, and frontmatter provenance. The default attention queue can surface pages with no outbound Markdown connection, but it cannot distinguish a generic-only page from a typed page, measure provenance coverage, or expose explicit relation vocabulary that the registry does not govern. Existing review state already provides portable item references, fingerprint-bound dismiss/snooze decisions, and deterministic RRF composition.

The change must remain a pure substrate. It may parse and count explicit Markdown structure, but it cannot infer that one note supports, contradicts, or causes another. Sources, Evidence, excluded content, read-only content, inactive conclusions, and navigation pages are not activation write targets.

## Goals / Non-Goals

**Goals:**

- Give an existing vault an objective activation baseline with explicit denominators.
- Rank individual, reviewable deficits without changing the normal daily attention queue.
- Route review toward the existing relation-proposal, schema, read, and governed edit operations.
- Reuse stable review identity and triage so the backlog can be worked incrementally.
- Keep the whole read path deterministic, dependency-light, and non-mutating.

**Non-Goals:**

- Automatically inventing, accepting, or writing semantic relationships.
- Treating graph density as truth, authority, confidence, or note quality.
- Requiring every claim to have block-level evidence or every wikilink to become typed.
- Modifying retrieval ranking, graph traversal profiles, or the relation registry.
- Adding a server-side reasoning model or an activation sidecar/database.

## Decisions

### Use a dedicated corpus activation scanner

A new `activation` module will walk the parsed KB once and classify active, read-write compiled pages using the same eligibility conventions as relation-debt review. It will return `AuditFinding`-compatible measurements plus a coverage object. This keeps the general audit response stable while still reusing the existing finding/ranking contracts.

Alternative: add several audit categories and derive coverage from their findings. Rejected because findings cannot express clean denominators, and independent audit checks would repeatedly parse the same page.

### Measure four explicit deficits

The scanner will emit, in fixed priority order:

1. `unregistered_relation`: an explicitly authored relation observation is not governed by the loaded relation registry.
2. `provenance_debt`: a page contains assertion-bearing semantic blocks but has no explicit page-level provenance edge (`derived_from`, `evidenced_by`, or `cites`, including supported frontmatter origins).
3. `typed_relation_debt`: a page has generic graph connections but no registered typed semantic relation.
4. `relation_debt`: a page has no explicit outbound graph connection.

These are structural measurements, not judgments. In particular, page-level provenance is intentionally coarse: its presence does not prove that every block is supported.

### Return counts with denominators, not a quality score

Coverage will report eligible pages, connected pages, typed-relation pages, generic-only pages, disconnected pages, provenance-candidate pages, provenance-linked pages, and unregistered observations. It will not collapse these into a single score. A single score would imply an unsupported quality ordering and would hide why the corpus needs work.

### Reuse attention ranking and review state under a dedicated mode

`review_memory(mode="activation")` will compose activation findings with equal-weight RRF, deterministic category/path tie-breaking, path deduplication, stable references, fingerprint-bound state, and explicit truncation. The default `attention` category set and order remain unchanged. Item lookup and triage will resolve both default-attention and activation-only items.

Alternative: add activation categories to the default attention inbox. Rejected for the first release because a mature existing corpus can produce a large backlog that would drown time-sensitive contradiction, staleness, and source work.

### Put action routes in measured finding metadata

Each reason will carry deterministic `next_actions` describing existing tool calls, such as relation suggestions, schema review, read, or governed edit. Routes are guidance only and do not execute during activation. Model-assisted suggestions remain an explicit downstream request and retain their existing default-off/soft-fail behavior.

### Inherit MCP, REST, and CLI exposure from `review_memory`

No new top-level command is required. Extending the shared `review_memory` leaf makes activation reachable through its generated MCP tool, `/api/review_memory`, OpenAPI schema, and CLI subcommand with no duplicated surface logic.

## Risks / Trade-offs

- [Large legacy backlog makes scans or responses noisy] -> Scan only eligible pages, cap surfaced items with explicit truncation, and keep the mode opt-in.
- [Generic links are sometimes exactly right] -> Label the signal as review debt, not an error, and never auto-convert links.
- [Page-level provenance overstates block-level support] -> Name the metric precisely and state the limitation in the response guidance.
- [Unknown relation syntax may be intentional vocabulary] -> Route it to `schema_memory`; do not silently map it to a core relation.
- [A dismissed activation item changes meaning after edits] -> Bind decisions to content-derived signal fingerprints so changed pages resurface.
- [Unreadable or malformed pages interrupt a corpus scan] -> Reuse tolerant page parsing and skip individual unreadable pages with logging; return the measurements that completed.

## Migration Plan

Ship as an additive `review_memory` mode with no data migration. Existing review-state files remain compatible because identity is path-based and fingerprints already include categories and signal versions. Rollback removes the mode and scanner; no vault content or sidecar requires reversal.

## Open Questions

None for the first slice. Future evidence-block granularity and project-specific activation lenses should be driven by real queue usage rather than hard-coded now.
