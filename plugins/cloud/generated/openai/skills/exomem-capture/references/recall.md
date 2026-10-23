# Recall, semantic units, and retrieval diagnostics

## Search

`search_memory` is the normal product command for recall. Underneath, `find` runs in
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
- **`RETRIEVAL_INDEX_WARMING` is a refusal, not a miss.** A maintained index is
  not ready: retry the same call after `retry_after_ms`. With `relations=` or
  `relation_of=`, the typed graph may be rebuilding: retry, or drop the filter.

### Referents

When recall returns a `referents` block, name only its `resolved` entities.
For `partial`, say how many people remain unresolved; for `ambiguous`, ask the
user to disambiguate; for `unresolved`, never guess. When the user supplies a
missing identity, run `connect_memory(operation="resolve-entity")` first, then
create the durable entity or use `edit_note` to add a reviewed alias.

Additional knobs exposed through `search_memory`/`find`: `graph=true` (default; expands
1-hop neighbours of strong matches through the typed graph sidecar when it is
available — typed and provenance relations rank ahead of plain wikilinks, and a
hit surfaced this way carries a `graph` annotation naming the relation type,
direction, and the seed page it came from; without a sidecar the lane falls back
to plain wikilink expansion, unannotated. The graph lane only adds neighbours and
reorders, so a graph rebuild is not a recall outage),
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
- Normal lookup: `search_memory(detail="compact", rerank=false)`.
- Reasoning context: `search_memory(deep=true)` when you need a compressed evidence bundle;
  add `graph_enrich=true` only when you need typed graph neighborhoods alongside
  the normal pack contract.
- Diagnostics: `search_memory(include_timings=true)`; add `rerank=true` only when you are
  intentionally measuring reranking or spending latency for precision. Interpret
  timing output with the returned compute mode, embedding backend, cache state,
  rerank flag, and search profile.

### Investigate what the packet leaves open

Use relevant held context first. Activation selects bounded working context;
`roles`, `missing` and `budget` describe its limits. An omitted role or skipped
task lookup does not show that the vault lacks the answer. Restricted readers
retain categorical baseline selection without supplemental task matching.

Use focused `search_memory` for a remaining question. Use `deep=true` when a bounded
evidence pack helps compare sources. Read a selected source when its qualifications
or complete contents matter. A directory requires `read_memory(path=<parent page>)`
without `unit_ref`. A unit read is not the whole page or directory.

Before recommending an older action, reconcile newer supported outcomes about that
action. Keep event time separate from the page's edit time. Treat an unresolved
conflict as unresolved; retrieval rank does not decide which claim is true.
Do not repeat an unchanged search or treat delivered text as proof of answer use.

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

## Context activation packets (`recall_context`)

Use for initial context when the live engagement policy from `bootstrap` warrants
recall. Pass the current user's words verbatim, not a search query; skip if a hook
already injected this turn's working set. For a known information gap, use
`search_memory`, then `read_memory` on selected refs. Activation is read-only and
abstains rather than guessing.

A packet may instead carry a map: an index of what exists, with titles, refs,
descriptions and current state when available. Read the map and pull by ref
with `read_memory`, or query `search_memory`, before answering; cite the refs you use.
Read `exomem://map` for the paginated index and follow its domain-map refs for
material beyond the packet. A map is not a claim that its items fit the turn.
Read an anchor's profile through its `profile` ref.
The retrieve hook shows the index once per generation. Later turns with the
same generation show a pointer to the map; a changed generation shows it again.

An unresolved packet can mean an unidentified subject or missing facts about a
known subject. Use the turn and relevant held context to distinguish them. For an
unidentified subject, ask for clarification instead of surveying unrelated records.
A retrieved candidate alone does not establish what the user meant. For a known
subject, continue targeted recall; abstention does not prove that its facts are absent.

Preserve each source's subject and scope when answering. If a record might concern
an entity but does not establish that link, state the uncertainty. A past event
alone does not establish a current condition, approval, or required procedure. Attribute
such requirements only when the source supports them.

An agent can refresh context within the same human turn when new evidence or
a task change makes held context incomplete. Use the exposed
`trigger="task_refresh"`, set `initiator="agent"`, keep the original human
`turn`, and supply bounded `conversation.focus`. The task packet follows that
focus; `intent_resolution` separately qualifies the human request. An
ambiguous human referent remains ambiguous until the agent has evidence for
a visible choice. The label describes the caller and grants no authority.

Before consequential output or after continuation restoration, inspect the
relevant held episode open items and Planning refs against newer closure
evidence. Refresh only for a remaining gap. Memory responses and rephrased
summaries alone do not justify another checkpoint. Reuse bootstrap policy;
request missing or changed sections instead of repeating full instructions.
Follow the client's reported operation and context bounds. Tool-only clients
have best-effort initiation; this guidance does not prove native enforcement.
If refresh is unavailable, keep ordinary source inspection usable under its
existing bounds and the action's own evidence requirements.

### Native checkpoint controller

Accounting requires an explicit profile-local memory MCP binding selected during
hook installation. Legacy profiles retain their activation and reminder behavior.
The hook reports its local controller arguments, opaque `scope_id`, limits, and
coverage. Local command metadata does not verify a remote server's version.

1. Submit the declaration through `controller.argv` plus `controller.declare_args` on standard input; expect a checkpoint association.
2. Use associated memory reads for the gap; expect each covered native leaf to reserve its allowance before dispatch.
3. Send `scope_id` and `checkpoint_id` through `controller.argv` plus `controller.end_args`; expect association to clear without renewing totals.

The declaration is JSON with `schema_version: 1`, `kind: "checkpoint"`, the
reported `scope_id`, an unsigned `task_revision`, `basis`, `boundary_ids`, and
`source_versions`. Add no prompt, focus, summary, result, or conversation hash.
Each of at most twelve source entries has `ref`, `version`, and `version_basis`.
The basis is `content_hash`, `evidence_version`, `external_version`, or
`unavailable`; unavailable versions use `null` and report partial deduplication.
The end payload contains only `scope_id` and `checkpoint_id`.

The checkpoint basis is `external_evidence`, `source_correction`,
`task_transition`, or `continuation_restore`. External and restoration boundaries
must be IDs that the hook observed in this scope. A source correction names a
caller-visible version. An increased task revision declares a transition; the
controller cannot prove its meaning. Memory replies, including mixed-tool and
unknown memory responses, cannot create external boundaries.

The default cumulative scope allowance is eight associated static reads and
three compiler-default activation character allowances. Installation can change
the limits. Failed or missing receipts retain reservations. Repeating a
declaration, ending an association, and declaring new evidence never renew totals.
Compaction clears association and retains existing scope totals. A new native
correlation creates another scope without proving human origin.

An exhausted association denies only covered static reads and explains how to
end it. Mixed operations, writes, imports, and ordinary inspection outside the
association remain available. Missing metadata, unsafe state, and uncovered
dispatch report unavailable accounting. They create no general action gate.
Codex stock code-mode leaves use their native hook path; an outer wrapper does
not count its children. External wrappers remain uncovered. Before-final checks
remain agent guidance; these hooks do not create a Stop continuation gate.

An optional `relevant_due_refs` candidate names due work on released current material.
Judge relevance and raise at most one due item per maintained client conversation, remembering prior mentions through compaction and credential renewal.
Use `review_item_context(ref, expected_fingerprint)` before one applicable writer or one triage dismissal; see [engagement](engagement.md#relevant-due-advice).

Only if needed to resolve the turn, pass relevant `conversation`: up to six
recent excerpts and 2,400 characters total, `focus` up to 240 characters and
`refs` up to twelve. Never send full history or unrelated personal data.
Reuse held material before calling again. If the conversation establishes a
follow-up's subject and another activation is needed, select its visible `anchor`.
Otherwise, provide grounded subject cues through `conversation.focus` when exposed.
Focus is an agent cue, not the user's words; it cannot settle ambiguity in the human turn.
A continuity token alone does not select a subject. Keep `turn` verbatim.
See the engagement reference for entry bounds and attachment cues.

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
`related`, `ambiguity` and `missing` may still be filled.

**`related`.** An `unresolved` turn that named no page may list up to three pages
under `related[]` (`path`, `title`, `projects`, `evidence: ["retrieval",
"vector_band"]`): current pages the turn's words reach and whose stored text is
also semantically close to it. They are listed, never loaded: nothing is served
for them, so `read_memory` one if it bears on the turn. The list is for the owner
only. `generation.related_evidence` says whether it ran: `ready`, or why not
(`cold` or `space_mismatch` for the stored text, else the state that
`generation.semantic_evidence` reports for a turn that was not encoded, such as
`uncalibrated` or `audience_restricted`); a cut list adds `{"role": "related",
"reason": "lane_truncated"}` to `missing`.

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
- `retrieval`: the turn's distinctive words reach one compiled page; the
  continuity token names it. Named by its title it is `resolved`
  (`lexical_overlap`, `retrieval`), else `retrieval_carried` (recall alone).
  Entities its served units name and link are `partial`
  (`carried_link`, `via` that page). Nothing distinctive abstains `unresolved`.
- `retrieval_named`: several pages reached this way; nothing is carried, the
  packet abstains `unresolved` and lists them under `anchors[]`. Pass one as
  `anchor`.

**`continuity`.** Every packet returns one token (abstentions too) identifying
the conversation; only a salted hash is stored and it lapses after six idle
hours. `generation.continuity` reports whether prior refs qualified this
activation: `applied`, `stale` or `absent`. `stale` can mean that the refs did not
contribute, even while `generation.continuity_thread` is `applied`.
`generation.continuity_thread` separately reports whether the token identifies
the conversation: `applied`, `stale` (answered as a new conversation, never
refused) or `absent`. Neither field alone proves that the subject resolved.
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
(`edit_note` adding to `learned_aliases`, or `schema_memory save-conventions`
adding a referential cue) with the `expected_hash` it needs, plus `turn_terms`.
It writes nothing; act only if those words should reach that page, else dismiss
its `review` ref with `triage_memory`.
