## Context

Two shipped detectors try to find compiled pages that have outgrown one subject. The lexical write-time detector (`structure_promotion.py`) reads unit tags against the page's declared identity. The audit detector (`structure_promotion_semantic.py`) clusters stored unit vectors. Both were measured on 40 large compiled notes from one personal vault. Model judges labelled the notes: 16 overloaded, 22 coherent long and 2 hubs. The owner later approved splitting 13 of the 16. The lexical detector flagged 8 overloaded and 7 coherent notes, and every note it flagged had 13 or more units. The semantic detector flagged none, with every unit embedded and routing switched off.

When a user asks a connected agent whether a note is overloaded, the agent reads the note and answers correctly. Exomem has no write-time model call, and the local sensors are pairwise NLI instruments. No local instruct model has yet passed the sensors programme's own questions.

## Goals / Non-Goals

**Goals**

- Ask the writing agent one fixed question when a page has grown enough for the question to matter.
- Record a "keep whole" answer, so a coherent page stays quiet until it grows again.
- Route every split through the curation lane and the user's exact plan confirmation.
- Make pages that are already past a step visible as a review backlog.
- Leave one documented slot where a future server-side sensor reading can attach.

**Non-Goals**

- Exomem does not judge meaning itself, and it makes no model call.
- No refusal, no forced split and no automatic restructuring.
- No new state store. Decisions use the existing review-state store.

## Decisions

### D1. The connected agent is the judge

The agent already holds the page and the user's context, and it runs on a frontier model. The runtime measures size and asks; the agent decides. A local model is not an option today: the sensors programme's local candidates failed its simpler questions, and Cloud cells run no sensors.

### D2. A size step schedules the question; it asserts nothing

A page is eligible when its note type has role `compiled`, it is not declared breadth (the existing `hub` and `snapshot` breadth tags), and it is not `index.md` or `log.md`. Its step is the largest value in the sequence 9, 14, 21, 32, 48, … that its semantic-unit count reaches. Each value is the previous one multiplied by 1.5 and rounded up. A page below 9 units has no step.

- The base is 9 because the smallest owner-confirmed overloaded note in the labelled set had 9 units.
- The factor of 1.5 bounds how often a coherent page is asked: about log₁.₅ of its growth, so 6 times between 9 and 100 units.
- Both constants are PROVISIONAL cadence parameters, not vocabulary. A blind coverage check on smaller pages can lower the base later.

Size alone may schedule a judgement, because the item claims nothing about the page. This replaces the old rule that size may never trigger a suggestion, which existed because the old suggestion did claim overload.

### D3. One item per page, fingerprinted by step

The finding uses the page's memory ref as its target and no review partition, so `item_id` is stable per page. Its `signal_version` is the step value. A dismissal therefore binds to the step: the page stays quiet within that step and reopens when it reaches the next step. The existing composer (`due_state._entry`, `review_state.component_fingerprint`) builds the identity for the write path and the projection alike. A second composer would break dismissal, as it did for an earlier family.

### D4. The write response asks while the item is open

A compiled write that commits to a page with an open `note_cohesion` item carries one `structure_suggestion` with `kind: note_cohesion`. This includes the first write after the page reaches a step, and any later write while nobody has answered. The agent has the page in hand at that moment, and one call answers the question.

The write path builds the same finding and identity as the projection. It reads the review state to decide whether the item is open. When the review state is unreadable, the response omits the suggestion, as the due-state serve already does. Asking on every write when decisions cannot be read would turn the question into noise.

The payload carries `kind`, `units`, `step_units`, `next_step_units`, `ref`, `fingerprint` and `question`. The response layer re-validates it against bounds declared for this kind, as it does for `source_classification_debt`.

### D5. The backlog lives in the due-state projection

`note_cohesion` joins `PROJECTION_CATEGORIES`, `DELTA_CATEGORIES` and `PAGE_DELTA_CATEGORIES`, and the page-recheck table. A page write then settles its own item. The family stays out of the default attention union for the reason `question_aging` does: the threshold is the system's choice, not an authored date.

In the served order the family sorts after dated obligations and before the Source backlog. An item's `due_since` is the page's authored `updated` or `created` date, and the computation date when the page has neither. Counts are always exact.

### D6. Answers use existing surfaces

- Keep whole: `triage_memory` decision `dismiss` with `why: "intentional: <the one question the page answers>"` and the item's fingerprint as `expected_fingerprint`.
- Split: the curation lane (`repair_memory` `curation-propose`, then preview and apply with the user's exact plan-id and fingerprint confirmation). A split that drops a page below its step clears the item by state.
- Hub by design: a governed edit that adds a breadth tag removes the page from the family.

After a split, a remainder or a new child that still reaches a step gets its own open item. The agent that ran the split answers those items in the same pass. This costs a few extra calls during a split and needs no special case.

### D7. The sensor slot is a reserved contract field

A future sensor reading can attach to an item as `evidence_refs`, a list of `{reading_id, content_hash}`. A reading whose content hash no longer matches the page is ignored. The item is complete without any reading, and this change adds no producer or reader. The sensors owner agreed to this shape. Selecting a server-side model belongs to that programme and to the owner.

### D8. Removals

- Delete `structure_promotion_semantic.py` and the `scope_divergence_semantic` audit category, check, finding composer and descriptions.
- Delete the lexical detector from `structure_promotion.py`. Keep `_terms`, `_is_term`, `FUNCTION_WORDS`, `_STOPWORDS`, `BREADTH_TAGS` and `NAVIGATION_BASENAMES`, which other modules import.
- Delete the tests that cover only the removed detectors.
- Existing review decisions recorded against `scope_divergence_semantic` remain as inert records.

## Risks / Trade-offs

- **Prose-heavy pages are undercounted.** A note whose body is mostly prose has few semantic units and may never reach a step. Accepted: units are what writes deposit. A blind check can show whether a second measure is needed.
- **Agents may ignore the question.** The question arrives on every write while open, and the backlog stays visible in due-state counts. If agents still ignore it, a later change can surface it in recall.
- **Agents may split coherent notes.** The question defaults to keeping coherent notes whole, and every split needs the user's exact confirmation.
- **Breadth tags are a list in code.** `BREADTH_TAGS` is old debt reused here, not added.

## Migration Plan

No data migration. The release notes name the removed kinds. Rollback means reverting the release; no stored state depends on the new family except review decisions, which an older release ignores.

## Proof Plan

- Outcome check: on a disposable vault served by the branch, a headless real-model agent session receives a realistic task that appends to a known overloaded note and to a known coherent long note. The write responses must carry the question. The agent must keep the coherent note whole by dismissal and propose a split for the overloaded note through the curation lane. The owner-confirmed splits supply the positive cases.
- Guard tests: step boundaries and reopening after a dismissal; a write that drops below the base clears the item; a breadth-tagged page never asks; a compiled write never carries the removed kind; a capture with classification debt still carries its own suggestion.
