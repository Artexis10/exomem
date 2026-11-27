# Recall, semantic units, and retrieval diagnostics

The short recall loop in `SKILL.md` covers ordinary recall. Read this for filtered, unit or media
recall, for a packet that leaves the turn open, and for retrieval diagnostics.

## Search

`search_memory` runs hybrid search by default: lexical and vector lanes, plus graph neighbours,
fused into one ranking. A natural-language query reaches pages that do not contain its words.

- **Exact lookups.** For a name, a code identifier or an exact phrase, use
  `search_memory(query="<term>", options={"mode": "keyword"})`. It returns no hit rather than a fuzzy one.
  The `vector` mode is a diagnostic aid.
- **Scope.** Search covers the Knowledge Base. `options={"scope": "vault"}` covers the whole vault,
  and `options={"widen_outside_kb": true}` keeps some places for pages outside it. Neither overrides
  governance.
- **Records and Planning rows are not in search.** Search finds a collection, not its items. For an
  event, a state or a planned item, use `query_records` or `query_plan_items`
  ([Planning and Records](planning-records.md)). A search miss says nothing about a collection's rows.
- **A miss is not absence.** It means "not found in what I searched". Try adjacent terms, the vault
  scope, or `read_page` on a page you suspect. A refusal because the index is warming is not a miss.
- **Filter-only lookups.** An empty query with `filters` lists the matching pages by recency.

Use a rare knob only when its need is clear:

| Need | Call |
|---|---|
| A superseded version, such as "what did I used to think" | `search_memory(query="<subject>", options={"prefer_active": false})` |
| Raw Sources ahead of compiled notes | `options={"prefer_compiled": false}` |
| One kind of file, or all but one | top-level `file_types`; `options={"exclude_file_types": ["<kind>"]}` |
| Recordings where one person speaks | `options={"speakers": ["<name>"]}` |
| Single claims instead of pages | `options={"result_level": "unit"}`, or `"mixed"`; `categories` and `kinds` narrow it |
| Pages joined by one typed relation | `options={"relations": ["<relation>"]}` |
| A packed evidence bundle to reason over | `search_memory(query="<question>", deep=true)`; add `options={"graph_enrich": true}` for typed neighbourhoods |
| Precision at the cost of latency | `options={"rerank": true}`; `rerank_max_candidates` bounds the reranker's input, not its time |
| Why a hit ranked where it did | `options={"explain": true}` |
| Where the time went | `options={"include_timings": true}` |

The `filters` field takes typed `page.*`, frontmatter and `unit.*` predicates.
`read_instructions(topic="search_memory")` lists the fields.

Read diagnostics with care:

- Leave `rerank` unset in normal use. The service chooses by compute mode.
- An `explain` value is not a confidence. A lane that did not take part has no score, not a zero.
- Read timings with the compute mode, embedding backend, cache state and rerank flag the reply names.
  One slow CPU rerank does not make the service slow. `read_instructions(profile="diagnostics")`
  describes the compute setup.
- A `budget` block means the deadline left no time for the stages it names. The result is complete
  for every stage that ran. Ask again with fewer options when you need the rest.
- A `latency` block on a `read_instructions` reply means this client's recent recalls were slow.
  It names the stage responsible.
- A relation filter can be refused while the typed graph rebuilds. Retry, or drop the filter.
  A graph rebuild never stops ordinary recall.

### Media hits

A hit on a recording, image or document names the original file. Treat the file as the result and
the matched transcript or text as the reason. A purely visual hit matched the picture, not text. A
video hit names the time of the matching frame. To see it, call
`inspect_memory(operation="media.video", arguments={"path": "<video>", "start_sec": 0, "end_sec": 30})`
around that time, after one overview call. To see an image, use `media.image`. To give the person
the original, see [tools](tools.md#recover-a-stored-original).

## Read what the packet leaves open

Use relevant held context first. Activation selects bounded working context; its `roles`, `missing`
and `budget` say what it left out. An omitted role does not show that the vault lacks the answer.

- Use a focused `search_memory` for each remaining question, and `read_page` when a source's
  qualifications or whole contents matter.
- A unit read is not the whole page. To read a directory page, call `read_page(page="<page>")`
  without `part="units"`.
- Before you recommend an older action, check for newer outcomes of that action.
- Keep the date of an event apart from the date its page was edited.
- An unresolved conflict stays unresolved. Retrieval rank does not decide which claim is true.
- When the user gives an identity that recall could not resolve, follow
  [entity resolution](writing.md#entity-resolution).

## Semantic units

Unit recall returns an exact unit id. `read_page(page="<page>", part="units")` lists the ids of one
page; pass an id as `unit_ref` to `connections.context` for the graph around one claim. A compact
observation's category does not imply a typed edge: traversal follows authored relations only.
A category filter matches a rich unit by its heading when the unit names no category; with no alias
for that heading, the unit's kind stands in. Category, kind, tags, context and relations are
separate axes. Graph context comes back bounded. Check its truncation before you call it complete, and keep its
`depth` small unless the user asks for more.

## Topic brief

Use `read_page(page="<page or subject>", part="brief")` when the user wants the whole compiled body of
one subject: what its notes conclude, which Sources they cite, and what stays open. A page fixes the
subject. Free text picks the best compiled note and lists the other candidates; read one of them to
switch subject.

The brief takes the compiled notes one link from the subject, and a few one link further. A cut is
counted in `missing`, never silent. A superseded or closed claim is left out and counted. The server
writes no summary: write the prose yourself from the brief. `changed_since` compares it with the
last brief you read to its end.

## Context activation packets

Activation with `recall_context` is read-only and abstains rather than guessing. For a known gap
after activation, use `search_memory` and then `read_page` on selected refs.

- **Epistemic status.** A page read or anchor may carry `epistemic_status`. Treat it as evidence for
  review; it never authorizes a write. When the evidence is incomplete, its counts may be partial.
- **Maps.** Read `exomem://map` for the paginated index and follow its domain maps for material
  beyond the packet. A map lists what exists; it does not claim that its items fit the turn. A hook
  shows the index once per generation and a pointer on later turns.
- **Recorded positions.** A `positions` block holds the person's own recorded positions on the pages
  in reach. When the turn bears on one, follow the block's `about` passage. An empty or unavailable
  block does not mean that the person holds no position.
- **Recent context.** `recent_context` leads every packet, abstained ones too. It is chosen by
  recency, not by the turn. Its date marks the last contact, not the event. A recap entry carries its
  `episode` key; open it with `read_page`. Pass `session` or `workspace` so that this conversation's
  pages come first; without them, another conversation's work is never the referent.
- **Abstention.** Call again with `anchor` set to the ref you mean. For an ambiguous packet the
  choice is yours, and a guess is not a choice.
- **Related pages.** Pages listed under `related` are listed, never loaded. Read one when it bears on
  the turn.
- **Carried anchors.** A turn that names nothing, such as "continue", resumes the thread of the
  `continuity` token, else the latest work. A short follow-up carries one partial anchor. When the
  turn's words reach several pages, nothing is carried: pass one of them as `anchor`.
- **Continuity.** Echo the token unchanged. It lapses after six idle hours; a stale token is
  answered as a new conversation, never refused. Neither continuity field alone proves that the
  subject resolved.
- **Episode reminder.** An `episode_due` block asks for a recap at the next stopping point. It is
  advice, not permission ([engagement](engagement.md)).
- **Learning.** A `learning` advisory is handled in
  [engagement](engagement.md#activation-conventions-and-learning-from-corrections).

## Refresh within a turn

Refresh with `trigger="task_refresh"` only when new evidence or a task change makes held context
incomplete. Keep the human `turn` verbatim and put the task in `conversation.focus`
([engagement](engagement.md#passing-the-conversation-to-activation) gives the bounds). The packet
follows that focus, and `intent_resolution` qualifies the human request separately. A focus is your
cue: an ambiguous human referent stays ambiguous until you have evidence for a visible choice.

Before consequential output, or after a continuation is restored, check held episode open items and
Planning refs against newer closure evidence. Refresh only for a gap that remains. Memory replies
and rephrased summaries never justify another refresh. Tool-only clients start refresh on a best
effort; if refresh is unavailable, keep ordinary reads within their bounds.

### Native checkpoint controller

A hook installed with an explicit memory binding reports a checkpoint controller: its arguments, an
opaque `scope_id`, the limits and the coverage.

1. Send the declaration on standard input through `controller.argv` and `controller.declare_args`.
   Expect a checkpoint association.
2. Make the memory reads the gap needs. Expect each covered read to reserve its allowance first.
3. End with `scope_id` and `checkpoint_id` through `controller.argv` and `controller.end_args`.
   Expect the association to clear; totals do not renew.

The declaration is JSON with `schema_version: 1`, `kind: "checkpoint"`, the `scope_id`, a
`task_revision`, a `basis`, `boundary_ids` and at most twelve `source_versions`. Add no prompt,
focus, summary or result. The basis is `external_evidence`, `source_correction`, `task_transition`
or `continuation_restore`. Name only boundaries that the hook observed in this scope. A memory reply
never creates an external boundary.

An exhausted association refuses only covered reads and says how to end it. Writes, imports and
reads outside the association stay available. Missing metadata reports accounting as unavailable;
it creates no gate. The controller cannot prove what a task revision means or that a turn came
from a person.
