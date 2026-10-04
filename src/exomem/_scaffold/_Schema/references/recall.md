# Recall, semantic units, and retrieval diagnostics

## Search

`ask_memory` is the normal product command for recall. Underneath, `find` runs in
**hybrid mode** by default: BM25 + local vector embeddings
(a multilingual model on a personal server, `BAAI/bge-m3`, 1024-dim) fused via
reciprocal rank fusion.
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

A `budget` block on the response means the request deadline left no room for the
stages it names under `skipped` and `truncated`: the result is complete for every
stage that ran and is not an error, so re-ask with a narrower option set when you
need what was left out.

A `latency` block on a `bootstrap` response means this client's own recent recalls have been slower than the service's ceiling; its `dominant_spans` name the stage responsible, and it is absent when nothing is slow.

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

## Context activation packets (`activate_context`)

Use for initial context when the live engagement policy from `bootstrap` warrants
recall. Pass the current user's words verbatim, not a search query; skip if a hook
already injected this turn's working set. For a known information gap, use
`ask_memory`, then `read_memory` on selected refs. Activation is read-only and
abstains rather than guessing.

Only if needed to resolve the turn, pass relevant `conversation`: up to six
recent excerpts and 2,400 characters total, `focus` up to 240 characters and
`refs` up to twelve. Never send full history or unrelated personal data. If a
hook missed the subject, retry with `conversation.focus`; on ambiguity, choose
an `anchor`. See the engagement reference for entry bounds and attachment cues.

**`recent_context`** leads every packet, abstained ones included. Up to eight
pages recently worked on (edited, read, captured as a session, recorded as a
conversation recap, or left open in Planning), each with title, why it is recent
(`why`), the contact date (`as_of` dates the contact, not the event described)
and the page's own `status` or `summary` line when present. It is chosen by
recency, not by the turn, so it never queries a Records collection the turn did
not name; `current_state[]` carries governed state for resolved anchors. A recap
entry (`why: "episode"`) is the newest revision of one conversation's
`episode_memory` record and carries its `episode` key; follow it with
`read_memory`. With `session` or `workspace`, that conversation's pages come first.

**Abstention.** `abstained: true` with a reason: `unresolved` (nothing resolved),
`ambiguous` (two competing senses, listed under `ambiguity`, no role lane runs),
`index_warming`, `disabled`. Resolve by calling again with `anchor` set to the ref
you mean. `roles`, `units`, `pointers` and `current_state` are empty on an
abstained packet; `anchors` (partial, `retrieval_named`, competing candidates),
`ambiguity` and `missing` may still be filled.

**How an anchor was reached** (`generation.carried_by`, anchor `status`):
- `agent_choice`, `status: "resolved"`: you named it with `anchor`.
- `recency`: a turn naming nothing ("continue", "where were we") resumes the
  thread your `continuity` token names, else the last work (picked with `anchor`
  or named by a recorded episode), in this conversation first, then its
  workspace, then the vault. Reads rank below writes; a maintenance batch counts
  as nobody's work. A single ordinary page is `kind: "page"`, `status: "resolved"`,
  evidence `["recency"]`; a tie abstains `ambiguous`. Without `session`,
  `workspace` or `continuity`, other conversations' work orders `recent_context`
  but is never the referent.
- `follow_up`: a short follow-up naming nothing new ("and the results?") with one
  page clearly ahead in this conversation's thread is carried as one `partial`
  anchor; if two are close, both go under `ambiguity`.
- `retrieval`, `status: "retrieval_carried"`: no anchor was named but the turn's
  distinctive words clearly reach one compiled page. Nothing was resolved; recall
  alone put it there, and the continuity token now names it. A turn with nothing
  distinctive abstains `unresolved`.
- `retrieval_named`: several pages reached this way; nothing is carried, the
  packet abstains `unresolved` and lists them under `anchors[]`. Unlike
  `ambiguity` (two anchors that both resolved), nothing resolved. Pass one as
  `anchor`.

**`continuity`.** Every packet returns one token (abstentions too) identifying
the conversation; only a salted hash is stored and it lapses after six idle
hours. `generation.continuity` is `applied`, `stale` (another vault index or role
registry) or `absent`; `generation.continuity_thread` is `applied`, `stale`
(answered as a new conversation, never refused) or `absent`.
`generation.hot_profile` reports the recent-work projection `state` (`current`,
`partial`, `seeded`, `behind`, `empty`) and `session_start`. Only salted hashes of
`session`/`workspace` are stored, locally.

**`episode_due`** (MCP door): after several activations with no `episode_memory`
record from this caller, asks for one at the next decision or stopping point.
Advice, at most once per half hour, absent when proactive capture is off.

**`upkeep`** (at session start): at most one item the background pass proposed,
such as two notes that could be connected. It carries its own `route`, a
`context_route` to read first and a `dispose` route (`triage_memory` dismiss or
snooze). Consideration does not authorize mutation: act through the route under
its own rules, or dispose of it.

**`learning`** may ride on an `anchor` call whose turn never named the chosen
page: an advisory naming the writer that would teach the vault the user's words
(`edit_memory` adding to `learned_aliases`, or `schema_memory save-conventions`
adding a referential cue) with the `expected_hash` it needs, plus `turn_terms`.
It writes nothing; act only if those words should reach that page, else dismiss
its `review` ref with `triage_memory`.
