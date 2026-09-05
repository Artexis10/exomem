# Engagement and capture decisions

## Proactive engagement

This skill is **context-aware, not just request-driven.** It engages on its own
in two situations and stays quiet otherwise. ("Proactive" means the assistant's
own judgment mid-conversation. On clients that support hooks, a capture/retrieve
nudge re-arms that judgment each turn; on clients without hooks this text is the
only prompt to check, so read it as standing instruction rather than advice.)

**Prominence level.** How strongly the two behaviours below apply is tunable.
`bootstrap()` reports the active level under `engagement`; the user changes it with
`exomem prominence <level>`, or by editing the level block in their assistant's
custom instructions. The section below describes **balanced**, the default where
hooks exist. The other levels shift it:

| Level | Shift from the baseline below |
|---|---|
| `off` | Never retrieve or capture on your own. Explicit requests only. |
| `light` | Retrieve only on an outright recall question or an unmistakably on-topic turn; capture only when asked; never narrate. |
| `balanced` | As written below. |
| `maximal` | Retrieve before **every** substantive turn, not only ones that reference prior work; treat the bar for "durable" as low and capture whenever torn; say what you recalled and what you saved. |

`maximal` is the shipped default on clients without hooks — the hosted service,
and assistants configured through a custom-instructions block — because there is
nothing there to re-arm the check, and passive instructions decay over a long
conversation.

**Proactive retrieval (read) — quiet, surface only hits.** When a turn
references something the KB plausibly holds — a project, a domain, a named
entity, or phrasings like "what did I conclude about X," "have I looked at Y,"
"where did we land on Z" — run a quiet `ask_memory` **first** and fold what you find
into the answer. Don't narrate the search; mention the KB only when it returned
something relevant, and cite the page(s) you used. A miss means "not found in
what I searched," never "it doesn't exist" — an empty `ask_memory` result means *no coverage
yet*, which is a reason to consider capturing, not to disengage.

**Stepping-stone capture (write) — then report.** When the conversation reaches
a **stepping-stone** — a durable conclusion lands, a durable recurring entity
accumulates reusable facts, history, or relations, **a method was actually
carried out and the user reports how it went**, **a stated intent or commitment
is made**, or **an observed outcome or event is reported** — capture it:

- A **durable personal baseline** is also a stepping-stone: a stable preference,
  recurring routine, historical baseline, or durable affiliation. Capture it only
  when both stability or recurrence and reusable comparison, interpretation, or
  decision value are clear. Attach a facet or affiliation to a uniquely resolved
  Entity; otherwise save one concise compiled observation. Use Records only for an
  observed measurement accepted by a compatible existing collection. Fleeting
  preferences, one-off activity, incidental associations, trivial metrics, and
  tentative claims stay quiet. Eligibility never creates an Entity, collection, or
  schema: an affiliation relation uses `link_acceptance`; entity creation or substantial curation
  uses confirmed `restructure_execution`; concise observations and narrow additive
  facts follow `proactive_capture` and its active disposition.

- Capture whether or not the KB already holds the topic. A durable conclusion on
  brand-new ground is first-class: it becomes the first page on that topic, which
  is how the corpus grows.
- Raw material -> `capture_source`. A durable conclusion -> draft with
  `remember` or `connect_memory`, run
  `connect_memory(operation="suggest-links")`, use `suggest-relations` when
  directional meaning matters, and run the near-duplicate check first,
  then write and report one line: `Saved -> <path>`.
- Resolve entity candidates against the active entity registry and selected knowledge packs.
  Call `connect_memory(operation="resolve-entity", name=...)` first. If one active page
  matches, use `edit_memory` for a small stable-fact correction or the canonical
  relation workflow for a new connection. If none matches, use
  `connect_memory(operation="create-entity")` only when the identity is stable,
  recurring, central to the conclusion, and useful beyond the current source.
  An unregistered-type finding supplies `proposal` and `expected_hash`; save
  those exact values through the governed
  `schema_memory(operation="save-entity-types")` leaf with `why`, never by
  editing frontmatter around the registry rule.
  A single incidental mention, unresolved identity, or transient participant
  stays in source/note context.
- **Recurring-identity maintenance boundary (balanced/maximal only).** On the
  first user turn after bootstrap, after primary work and before the final
  response, call `review_memory(mode="attention",
  categories=["entity_recurrence"], limit=3)` once per session. Later ordinary
  prompts do not rescan. It returns at most three candidates, each carrying at most eight contexts. Resolve
  exact and alias matches first, stop on ambiguity, hydrate one match before a
  duplicate, and promote only a stable reusable no-match. Open its exact review
  ref with curation `work-item`; plans use only governed steps. An unknown kind
  goes through `schema_memory(operation="save-entity-types")`, then refreshes
  the candidate; never edit the registry through curation.
  One general Entity, accepted-relation, or registry mutation permits one
  recheck. A separately confirmed hydration batch with a terminal receipt
  permits one same-identity curation `work-item` using the same `review_ref` and
  the next `hydration_recheck` ordinal, then pauses for fresh confirmation: at
  most eight mutations and eight rechecks per session. The eighth recheck is
  closure-only, exposes no ninth batch, and leaves any remainder for the next
  session. Off/light are explicit-only. If the active surface lacks the explicit
  review-category call, skip it honestly: no local scan, model, embedding, or
  due-state substitute. The active agent remains the sole semantic decider.
- The guardrails that remain are the ones that matter: dedupe (prefer
  **edit_memory**/**replace_memory** over a parallel page; surface a near-duplicate warning when
  it fires) and clean links.
- A carried-out method is a landing like any other, and it is the one most often
  missed, because it arrives as ordinary conversation rather than as a
  conclusion. It qualifies when all four hold: a concrete method was actually
  executed; the user reports the result; the result is clearly good, bad, or
  diagnostically informative; and the method or the lesson is reusable later.
  Route by what it yielded — a proven method to its own how-to page, a
  parameter comparison to an **experiment**, a diagnosed failure mode to a
  **failure** note. A one-off with nothing reusable stays unwritten.
- A **stated intent or commitment** is a landing too: the user says what they
  will do, commits to a batch or workstream, sequences work ("the next one",
  "the others next time"), or re-prioritises. Resolve posture first, inspect
  Planning, then update a matching item before creating an inbox item.
- An **observed outcome or event** is the mirror class: the conversation reports
  that something happened, was produced, measured, delivered, approved,
  published, or failed. Route it to Records — `record_memory(action="append")`
  into the one compatible collection.
- **Pairing rule.** Append an observed outcome to Records first; it is the
  canonical observation. It never changes Planning automatically. An explicit
  user intent may request a guarded transition; otherwise a
  propose-after-outcome posture may only propose one. A **tentative** claim ("probably posted, not sure") is never
  written as an event — say so in a note field if the manifest offers one — and
  elapsed time is never an outcome.
- Pause and ask only when type or scope is genuinely ambiguous (research vs.
  insight vs. experiment; which `Notes/Research/<scope>`).

Not a stepping-stone: mid-thought exploration, brainstorm tangents, unresolved
questions, or incidental names without durable reusable context. Capture at the
landing, not during the flight.

Do not wait to be asked. "Did you save that?" arriving after a result already
landed is the failure, not the prompt.

## Generated artifact adoption

Generated drafts stay ephemeral. Generation, preview, filename, MIME type,
apparent quality, and abandoned or revised-away variants do not make durable
work. When the user selects, approves, sends, or publishes one offered output,
that exact offered artifact becomes adoption-eligible; the event is evidence of
adoption, not write consent. Agent-initiated adoption therefore follows
`proactive_capture`, while an explicit request to save is an ordinary requested
action. Preserve only the selected handle's exact bytes and write no siblings.

Choose the semantic lane before transport: reasoning material is a Source;
an approved deliverable or proof-bearing output is Evidence. MIME never chooses.
Use `capture_source(..., adoption={key, trigger, selected_file_id})` or
`preserve_artifacts(..., adoption={...})` when a direct handle exists. With no handle,
report a non-committing `handoff_required` or
`handoff_prepared`; a token or description is not a saved artifact.

Delivery is a later fact. Record it only after a committed local Evidence
receipt, through `record_memory(action="append", delivery={...})` and one
compatible existing Records collection whose declared link field names the
Evidence companion. Keep reported remote identity separate and set verified
identity only from matching platform proof; never infer remote byte equality.
A missing collection is `structural_suggestions`; creating or changing it is
confirmed `restructure_execution`. A separately accepted relation remains
`link_acceptance`. Adoption success never waits on any of those later changes.
