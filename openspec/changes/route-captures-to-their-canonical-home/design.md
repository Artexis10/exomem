# Design

## Context

`add_observation` refuses any typed relation on a compact unit (`observe_memory.py`, `COMPACT_RELATIONS_REQUIRE_RICH_KIND`). Compact units also refuse `as_of` and `until`. The relation registry already declares a page projection for provenance relations: `derived_from` has `behaviours: [sources, associative]` and `frontmatter_fields: {sources: forward}`; `evidenced_by` maps to `evidence`. `create_note` and `supersede_note` already take `sources`.

Write-time Records routing (`semantic_writes._records_routing_from_terms`) feeds the page title, page tags and unit tags to `collection_claims.route`, which needs at least two claim terms to overlap, or the collection's `claims.match` predicates to hold.

## Decisions

**Provenance relations lift to the page.** The test is a declared registry attribute: a forward entry in the relation's `frontmatter_fields`. The code never names `derived_from` or any other entry key, so a vault overlay that declares a new provenance relation gets the same behaviour (C4). The unit stays compact, and the page field gains the target in its canonical link form. A mix of liftable and non-liftable relations refuses as a whole, so a call never half-applies. A target that does not resolve follows the rules `create_note` applies to `sources`: it is kept as written and the reply carries the normaliser's warning.

Alternatives rejected: auto-selecting a rich kind silently changes the unit's semantics, and a new `sources` argument on `add_observation` adds schema bytes to every client when the agents already send `links`.

**The Source reply teaches application.** The reply's `next_action` is the place an agent reads right after the save. The sentence is conditional on the person presenting the file as current, so a file kept only for custody does not trigger an edit. It does not mark the file's claims as verified; it tells the agent to record the person's statement and cite the file.

**No semantic write-time routing.** A fallback that compares the written unit with each collection's profile was calibrated on 25 November 2026 on the maintainer's vault (read-only copies, BAAI/bge-m3, the served recall encoder). The vault had 81 active Records collections. Positives were 739 unique `why` texts of committed Records writes, each labelled with its collection; negatives were 672 random compiled-note observations. The threshold and margin were chosen on one hash half and reported on the other:

- Profile = title, claims and field names: at the selected point the held-out precision was 0.91 with recall 0.17 (61 of 368).
- Adding the collection's description: held-out precision 0.94 with recall 0.18.
- Assuming fits are a tenth of writes: at precision of 0.83 to 1.0, recall was 0.11 to 0.13.
- The incident's own two opinions ranked two unrelated collections first, and the opinions collection was not in the top three for either.

The fallback would stay silent on the case it exists for, so it is dropped. Because the agent already has the instruction "Items compared or tracked: Records rows, not a note.", the gap is in seeing the collection before the write. That belongs to recall.

## Risks

- Lifting a relation to the page loses unit-level precision: the page says it draws from the Source, not which unit. That is the meaning `sources` already has on `create_note`, and a rich kind remains available for unit-level edges.

## Proof

Red-then-green tests for each part. A real-model replay of the 2026-11-23 case, which needs operator approval for spend: the note cites the Source and states the applied value as current.
