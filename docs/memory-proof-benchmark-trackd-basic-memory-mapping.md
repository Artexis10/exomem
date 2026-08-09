# Track D journeys on Basic Memory — event → command mapping (doc only)

Status: published mapping table for task 7.4 of `add-memory-proof-benchmark`.
No Basic Memory execution happens here or in CI (the `bm` executable cannot
run in the sandbox); this table is the contract a future user-run Track D
lane would follow. Command names are the Basic Memory MCP tool surface
(`write_note` / `edit_note` / `search_notes` / `recent_activity`, plus
`read_note` where it is the faithful counterpart), verified against the
read-only sibling checkout (`src/basic_memory/mcp/tools/`).

Honesty rule (same as everywhere in membench): an event with no faithful
counterpart is scored **unsupported — never zero**. "Nearest approximation"
below is documentation of what a user could do manually; the benchmark does
not emulate it on the product's behalf.

## Journey event kinds → Basic Memory commands

| Journey event kind | Used by | Basic Memory command | Fidelity notes |
| --- | --- | --- | --- |
| `init_vault` | J1 J2 J3 | `bm project add <name> <dir>` (CLI; no MCP equivalent needed) | Project registry replaces vault init. |
| `remember` (create compiled note) | J1 J2 J3 | `write_note` | Frontmatter + `- [category] fact #tag` observations map 1:1; exomem `note_type`/`project` fields ride as frontmatter/tags. |
| `remember` with `## Relations` typed bullets | J3 | `write_note` (relations inline: `- relation_type [[Target]]`) | The relation GRAMMAR maps; the commit-gating relation *disposition* contract does not (see unsupported). |
| `capture_source` (raw, immutable source) | J2 J3 | `write_note` into a captures folder | No first-class source/inbox altitude: a capture is just a note; immutability and `ingested_into` back-fill are not enforced. |
| `replace_memory` (supersede, keep lineage) | J1 J2 | `write_note` (new revision) + `edit_note` (annotate the old note) | No typed supersession primitive; lineage degrades to prose/frontmatter convention. Old-note `status: superseded` semantics are a manual convention, not product behavior. |
| `ask_memory` (ranked retrieval) | J1 J2 | `search_notes` | Hybrid search maps well; exomem's prefer-active ranking (superseded pages rank below active) has no counterpart — both revisions rank purely by relevance. |
| `read_memory` (page + frontmatter) | J1 J2 | `read_note` | Direct counterpart. |
| `review_memory --mode evolution` (version chain) | J1 | **unsupported** | No version-chain review. Nearest approximation: `recent_activity` shows recently changed notes without lineage or ordering guarantees. |
| `maintain_memory --mode audit` (corpus audit) | J2 | **unsupported** | No corpus audit/lint surface. Nearest approximation: `recent_activity` for "what changed", manual reading for the rest. |
| `review_memory --mode stale` (dormancy queue) | J3 | **unsupported** | No staleness model (no review gate over age/inbound-degree/access). `recent_activity` shows recency only — the inverse signal, not a queue. |
| `review_memory --mode contradiction` | J3 | **unsupported** | No contradiction sweep. (In exomem's deterministic lexical profile this queue is also unsupported — embeddings-gated — so the J3 comparison on this queue is unsupported-vs-unsupported, honestly reported on both sides.) |
| `review_memory --mode unprocessed-sources` | J3 | **unsupported** | Without a source altitude there is no "captured but never compiled" state to queue. |
| `review_memory --mode attention` (open-loop union) | J3 | **unsupported** | No attention/triage queue; `recent_activity` is the only sweep-shaped primitive and carries no categories. |
| relation disposition / bootstrap open-loop | J3 | **unsupported** | Basic Memory never blocks a commit for a missing qualifying relation, so the planted open-loop (bootstrap relation debt) cannot exist. |

## What this implies for a user-run Basic Memory Track D lane

- **J1** runs end to end through `write_note`/`edit_note`/`search_notes`/
  `read_note`, but its two lineage checks (ordered 3-state evolution chain;
  chain anchors) score unsupported, and the "current page ranks first"
  check measures raw relevance, not lifecycle-aware ranking.
- **J2** runs its write/search/read spine; the audit check and the
  superseded-status checks score unsupported.
- **J3** reduces to seeding only: every review queue scores unsupported.
  That is itself the published result — the weekly-review workflow is the
  differentiating surface, and this table is the evidence trail for scoring
  it honestly instead of zeroing it.
