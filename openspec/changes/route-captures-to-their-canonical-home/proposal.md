# Proposal

## Why

A dogfood turn on 2026-11-23 shows a capture losing its provenance even though the agent tried to save it properly.

- **A handed applied file.** The person handed over a saved export of a settings file and called it the applied one. The agent kept it as a Source by its exact bytes. It then added a compact observation with a `derived_from` link to that Source. `add_observation` refused with `COMPACT_RELATIONS_REQUIRE_RICH_KIND`, the agent dropped the link, and the note never cited the Source. Nothing in the save told the agent how to apply the file either, so the note kept the earlier value as current and recorded the applied value as unverified.

The relation registry already says where such a link belongs on a page: `derived_from` declares `frontmatter_fields: {sources: forward}`. The refusal ignores that declaration.

A second gap seen on 2026-11-25, a prose note written where an existing Records collection fitted, is not fixed here. A write-time routing fallback was measured on a private vault copy and dropped (see design). That gap stays with recall.

## Decision

Correct provenance at the write seam, in the tool result the agent reads when it acts, not in more bootstrap or server text. Agents treat server text as background, and the compact core has no headroom, so this change adds no core bytes. A durable spec helps because the bootstrap contract currently promises that compact observations never carry typed relations. This change narrows that promise to one exception.

## What Changes

- **A compact observation can cite its Source.** When every relation sent with a compact `add_observation` carries the registry's `sources` behaviour, its core relation binds the page's `sources` field, and its target is captured material (a Source or Evidence page), the write commits compact. Each target goes into that page field without duplicates, whatever form the page already cites it in. The outcome names the cited page, and the compact reply's `user_summary` repeats it. Any other relation still refuses whole, and a unit that is already rich keeps its relations on the unit.
- **Saving a Source says how to apply it.** A committed `save_source` reply carries a `next_action`: if the person presents the file as the current state (for example an applied or final export), update the note's current statement now and cite this Source; the earlier value stays as dated history.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `semantic-write-contract`: compact observations lift source citations into page frontmatter.
- `agent-bootstrap-contract`: the semantic-unit-language promise names that exception.
- `durable-source-capture`: a committed Source save names how to apply the file.

## Impact

`observe_memory.py`, the terminal projection, the Source capture reply, the semantic authoring text and its generated copies, tests. The compact bootstrap core is unchanged: measured on the rebased branch and on its base alike, the worst case is 15,000 of 15,000 bytes (claude-code, maximal, populated vault) and 14,684 on an unpopulated vault. The 2026-11-23 case stays open until a real-model replay passes on a release.
