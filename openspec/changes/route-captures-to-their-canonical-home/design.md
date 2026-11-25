# Design

## Context

`add_observation` refuses any typed relation on a compact unit (`observe_memory.py`, `COMPACT_RELATIONS_REQUIRE_RICH_KIND`). Compact units also refuse `as_of` and `until`. The relation registry already declares a page projection for source citations: `derived_from` has `behaviours: [sources, associative]` and `frontmatter_fields: {sources: forward}`. `create_note` and `supersede_note` already take `sources`, and the commit's source closure checks a compiled page's `sources` field against captured material.

## Decisions

**Source citations lift to the page.** A relation lifts only when all of these hold, each read from the registry or the page and never from a relation key:

1. The relation has the closed `sources` behaviour. `supersedes` and `links_to` declare page fields too, but they are not citations, so they keep the refusal.
2. Its core relation declares a forward page field. A vault extension takes its core parent's field, because only the shipped pack binds fields; a `sources` relation with no field, such as `cites`, keeps the refusal.
3. Its target resolves to captured material (a Source or Evidence page). Anything else keeps the refusal that names the rich form, because the commit's closure refusal would tell the agent to drop a link the rich form accepts.
4. The unit stays compact. A rich unit that keeps its kind on an update carries its own relations, so an update that omits `kind` never lifts them.

The unit stays compact and the page field gains each target in its canonical link form. A target the field already cites, in any form the link normaliser reads as the same page (a title, a path with an alias, a stable memory reference), is not added again. A mix of liftable and non-liftable relations refuses as a whole, so a call never half-applies. A target under Sources or Evidence that the commit's source closure then rejects (a wrong page type, or a page the writer may not read) refuses with `UNRESOLVED_SOURCE_CITATION` and writes nothing.

The outcome sentence says which page the frontmatter cites. A `validate` preview words it in the future tense, because nothing is written yet. The compact reply appends the sentence to `user_summary`, the same place an update's dropped relations appear.

Alternatives rejected: auto-selecting a rich kind silently changes the unit's semantics, and a new `sources` argument on `add_observation` adds schema bytes to every client when the agents already send `links`.

**The Source reply teaches application.** The reply's `next_action` is the place an agent reads right after the save. The sentence is conditional on the person presenting the file as current, so a file kept only for custody does not trigger an edit. It does not mark the file's claims as verified; it tells the agent to record the person's statement and cite the file.

**No semantic write-time routing.** A fallback that compares the written unit with each Records collection's profile was calibrated on 2026-11-25 on a private vault copy, with the served recall encoder and held-out cases. At the precision needed to stay quiet on writes that fit no collection, it recalled a small share of the writes that did. On the case it was meant for, it ranked unrelated collections first. It would stay silent where it is needed, so it is dropped. The agent already has the instruction "Items compared or tracked: Records rows, not a note."; the gap is in seeing the collection before the write, which belongs to recall.

## Risks

- Lifting a relation to the page loses unit-level precision: the page says it draws from the Source, not which unit. That is the meaning `sources` already has on `create_note`, and a rich kind remains available for unit-level edges.

## Proof

Each part has a test that fails on the code before the change: the lift, the refusal of a non-citation relation, the refusal of a non-captured target, the unchanged rich-unit edit, the duplicate-free citation, and the summary line. A real-model replay of the 2026-11-23 case needs operator approval for spend: the note cites the Source and states the applied value as current.
