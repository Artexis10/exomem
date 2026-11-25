# Proposal

## Why

Two dogfood incidents show captures landing in the wrong place, or with their provenance missing, even though the agent did try to save. This change fixes the first; the measurement below shows why the second needs recall, not a write-time router.

- **A handed applied export (2026-11-23).** A real-model turn on installed v0.199.0 kept the file as a Source by its exact bytes. It then tried to add an observation with a `derived_from` link to that Source. `add_observation` refused with `COMPACT_RELATIONS_REQUIRE_RICH_KIND`, the agent dropped the link, and the note never cited the Source. Nothing in the save told it how to apply the file either. It left the stale 15-minute value as current and recorded the applied 5-minute value as "unverified". The reference scored the turn 2 of 4.
- **Opinions filed as prose (2026-11-25).** On a hosted chat client, an opinion about an album and one about a track went into a prose note instead of the existing Records collection for opinions. The note write's `records_routing` advisory routes only when at least two claim words overlap the page's title and tags. An opinion about a work rarely shares words with a collection's claims, so the write said nothing about the collection that fits.

The registry already says where a provenance relation belongs on a page: `derived_from` declares `frontmatter_fields: {sources: forward}`. The refusal ignores that declaration.

## Decision

The decision is where a capture's routing and provenance are corrected: at the write seam, in the tool result the agent reads at the moment it acts, not in more bootstrap or server text. Measured Exomem changes found that agents treat server text as background, and the compact core has no headroom (14,953 of 15,000 bytes). A durable spec helps because the bootstrap contract currently promises that compact observations can never carry typed relations. This change narrows that promise.

## What Changes

- **A compact observation can cite its Source.** When every relation sent with a compact `add_observation` declares a forward `frontmatter_fields` mapping in the relation registry (such as `derived_from` → `sources`), the write commits compact. Each target is added to that page frontmatter field, without duplicates, and the outcome line names the cited page. Any other relation still refuses, and the refusal names the admitted kinds.
- **Saving a Source says how to apply it.** A committed `save_source` reply carries a `next_action`: if the person presents the file as the current state (for example an applied or final export), update the note's current statement now and cite this Source; the earlier value stays as dated history.

A semantic fallback for write-time Records routing was considered and measured, then dropped (see design). The 2026-11-25 routing gap is left to the recall side of that incident, which method activation owns: an agent that sees the fitting collection before it writes already has the instruction "Items compared or tracked: Records rows, not a note."

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `semantic-write-contract`: compact observations lift provenance relations into page frontmatter.
- `agent-bootstrap-contract`: the semantic-unit-language promise names that exception.
- `durable-source-capture`: a committed Source save names how to apply the file.

## Impact

`observe_memory.py`, the relation registry reader, the Source capture reply, bootstrap text, tests. The 2026-11-23 incident stays open until a real-model replay passes on a release.
