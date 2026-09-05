# Recall, semantic units, and retrieval diagnostics

## Search

`ask_memory` is the normal product command for recall. Underneath, `find` runs in
**hybrid mode** by default: BM25 + local vector embeddings
(BAAI/bge-base-en-v1.5, 768-dim) fused via reciprocal rank fusion.
Natural-language queries reach pages that don't contain the literal terms.

Modes:

- `mode="hybrid"` (default) — BM25 + vector + graph + keyword fused via RRF. A
  strict superset of keyword: hybrid never returns fewer results than keyword for
  the same query. Falls back to BM25-only if the embedding sidecar is missing.
- `mode="keyword"` — strict case-insensitive substring matching, sorted by
  `updated:`. Use for precision-only lookups (exact phrase, entity name, code
  identifier) where you'd rather get zero results than fuzzy ones.
- `mode="vector"` — vector-only. Diagnostic aid.

Empty queries degrade to filtered-most-recent regardless of mode.

**Scope — the vault is bigger than the KB:**
- `scope="kb"` (default) searches `Knowledge Base/` first and **auto-widens to
  the whole vault** when the KB doesn't fill `limit`. Content in sibling folders
  is reachable, not silently invisible. Widened hits carry `outside_kb: true`.
- `scope="vault"` always walks the whole vault. `scope="kb-only"` is the strict
  opt-out (KB only, never widens).
- **Never report a search-miss as absence.** An empty result means *"not found in
  what I searched,"* not *"it doesn't exist."* If you're sure something exists,
  try `scope="vault"`, vary the query terms, or `read_memory` a path you suspect.

### Referents

When recall returns a `referents` block, name only its `resolved` entities.
For `partial`, say how many people remain unresolved; for `ambiguous`, ask the
user to disambiguate; for `unresolved`, never guess. When the user supplies a
missing identity, run `connect_memory(operation="resolve-entity")` first, then
create the durable entity or use `edit_memory` to add a reviewed alias.

Additional knobs exposed through `ask_memory`/`find`: `graph=true` (default; expands
1-hop neighbours of strong matches through the typed graph sidecar when it is
available — typed and provenance relations rank ahead of plain wikilinks, and a
hit surfaced this way carries a `graph` annotation naming the relation type,
direction, and the seed page it came from; without a sidecar the lane falls back
to plain wikilink expansion, unannotated),
`rerank=true` (CrossEncoder re-sort, explicit precision spend),
`prefer_compiled=true` (default; favours compiled types over raw `source`),
`prefer_active=true` (default; soft-demotes superseded pages), `file_types` /
`exclude_file_types` (scope to or drop artifact kinds: `note`, `pdf`, `image`,
`audio`, `video`, `docx`, `xlsx`, `pptx`, `html`, `text`, `email`, `calendar`,
`csv`, `json`, `tsv`), and `speakers` (restrict to diarized media whose
`speakers:` frontmatter names a given person). Leaving `rerank` unset is
mode-aware auto: CPU steady-state modes keep it off; accelerated/performance
mode may auto-rerank when lanes strongly disagree or the query is long.

When reranking is enabled or selected automatically,
`rerank_max_candidates` optionally bounds only the fused prefix sent to the
reranker. It must be an integer from the effective normalized result `limit` up
to 300. The retrieval profile reports `candidate_limit_requested`,
`candidate_limit_effective`, `scorer_input_count`, and `unscored_tail_count`.
The tail keeps fused order. A candidate count bounds scorer work, not wall-clock
time; model warm-up, hardware, and text length still affect latency.

Performance presets:
- Normal lookup: `ask_memory(detail="compact", rerank=false)`.
- Reasoning context: `ask_memory(deep=true)` when you need a compressed evidence bundle;
  add `graph_enrich=true` only when you need typed graph neighborhoods alongside
  the normal pack contract.
- Diagnostics: `ask_memory(include_timings=true)`; add `rerank=true` only when you are
  intentionally measuring reranking or spending latency for precision. Interpret
  timing output with the returned compute mode, embedding backend, cache state,
  rerank flag, and search profile.

**Semantic units are first-class.** A compact observation uses
`- [category] content #tags (context) ^anchor`; its governed kind is always
`observation`, while category remains open vocabulary. Rich `## Kind` blocks use
a governed non-observation kind and may carry typed relation metadata. Use
`observe_memory(operation="add"|"update"|"remove"|"validate")` for one unit
instead of brittle whole-page string surgery. Update/remove must echo the parent
`content_hash` and current unit fingerprint. Compact units cannot carry typed unit
relations: select rich form or author one reviewed note-level relation under
`## Relations`.

For a rich unit without explicit `- category:`, the heading supplies
`category_raw` and the normalized `category_key` before reviewed category-alias
resolution; without an applicable alias, the resolved category falls back to
the governed kind. Rich comma-separated `tags` (without `#`) and single-line
`context` are first-class retrieval fields. Category, kind, tags, context, and
authored relations remain separate axes.

Recall semantic language through `result_level="page"|"unit"|"mixed"`.
`categories` and `kinds` are convenience filters; use bounded `filters` for
typed `page.*`, RFC-6901 frontmatter, and `unit.*` predicates. An empty query
with filters is a filter-only lookup ordered by filtered recency, not a text
match. Use `explain=true` only when ranking interpretation matters. Its bounded
profile distinguishes raw BM25 values, cosine similarity, RRF contributions,
reranker values, and final rank; none is confidence, and unavailable or
nonparticipating lanes must never be invented as zero-valued hit evidence.

Unit recall returns an exact `unit_ref` for `read_memory`. For authored graph
context, pass that reference or category/kind filters to
`connect_memory(operation="graph-context")`. Compact categories do not imply
typed edges: traversal follows authored relations only.
