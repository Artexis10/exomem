---
name: exomem
description: Governed long-term memory for relevant project, decision, and evidence work.
required_tools: [ask_memory, read_memory, remember, observe_memory, edit_memory, connect_memory, record_memory, capture_source, preserve_artifacts, review_memory, maintain_memory]
---

Keep client-native memory for preferences, routing, and immediate working context; use Exomem for durable project and domain knowledge.

## How much to use it

Hosted Exomem runs at **maximal** prominence by default. Treat what follows as standing instruction rather than advice: nothing outside this conversation will remind you to check, and instructions like these tend to fade as a thread grows. Holding to them late in a long conversation is the whole point of the level.

The user can lower it by adding a line to their assistant's custom instructions, for example "Exomem prominence: light". Honour that immediately when you see it: `light` means retrieve only on an outright recall question and save only when asked; `off` means explicit invocation only. Absent any such line, use the maximal behaviour below.

## Recall

Search before answering any substantive turn, not only the ones that obviously reference past work. Assume Exomem may hold something relevant until a search says otherwise. Quietly use `ask_memory` first, then `read_memory` for a page worth opening in full. Skip only pure chit-chat and short control messages.

The search itself is quiet; the result is not. Cite a useful retrieved note in the answer rather than describing the lookup that found it. An empty result means "no coverage yet" — a reason to capture, not to disengage. Never present a miss as proof that something does not exist.

## Capture

Save at every stepping stone, and keep the bar low: a clear reusable decision, a solved problem, a diagnosed failure, a reusable pattern, a research finding, or a durable fact about a recurring person, project, or organisation. When torn between saving and letting it pass, save. Use `remember` for a compiled conclusion and `observe_memory` for a single durable observation on an existing page.

Write a concise compiled outcome, never raw conversation transcripts. Do not save trivial, speculative, redundant, or sensitive-without-purpose material. Capture at the landing, not during the flight — a conclusion that has actually landed, not mid-thought exploration.

## Reporting

Say what you did. Name what you recalled, and report one line after each write, as `Saved -> <path>`.

Treat the final mutation result as authoritative. A response reporting a committed write means the write succeeded, whatever warnings or diagnostics appear beside it. A first call that asks for review is not a failure — complete the review step and read the final result. Never infer or invent an error code the server did not return, and never report a completed write as failed.

## Durable personal context

Stable preferences, recurring routines, historical baselines, and durable affiliations count as stepping stones even when they arrive casually. Eligibility needs both discriminators: the fact is stable or recurrent, and it is useful for later comparison, interpretation, or decisions. Tone, proper nouns, and ordinary detail establish neither.

Route by meaning. A stable facet or affiliation of a uniquely resolved entity belongs on that entity, through `edit_memory` for a facet or `connect_memory` for a relation the user confirmed. Other eligible context becomes one concise compiled observation. An observed event or measurement belongs in an existing compatible collection through `record_memory`. Ambiguity never creates an entity or a collection implicitly.

A fleeting preference, a one-off activity, isolated trivia, or a tentative claim is not a baseline. Let it pass without a write.

## Adopted generated artifacts

When exactly one generated file is selected to keep, adopt those exact bytes: reasoning input goes through `capture_source`, supporting material through `preserve_artifacts`. Adopt the selected file only, never a sibling draft or a different variant. Selection establishes eligibility, not write consent.

Without a real file handle, say so and stop; do not claim a save that did not happen and do not describe a remote reference as proof of the bytes. Report a delivery only against a committed receipt.

## Recurring identities

Once per session, after the primary work of a turn and before the final response, you may read attention candidates with `review_memory` for the `entity_recurrence` category, bounded to three candidates. Resolve exact names and aliases first. Stop on ambiguity rather than guessing. Hydrate the one existing match before creating anything, and promote a new identity only when nothing matches. A general mutation permits one recheck; each hydration batch needs its own fresh confirmation and stays on the same identity.

## Governed curation

Structural repair runs through `maintain_memory` in curation mode. Proposing, previewing, and reading status are read-only and advisory. Applying, resuming an approved plan, and applying a compensation are execution: each needs explicit confirmation against the exact reviewed plan. An unknown or omitted curation action fails closed, and so do the fix, reconcile, and backfill modes.
