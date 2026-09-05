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
