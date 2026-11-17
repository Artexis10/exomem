# Proposal

## Why

A vault can serve one global profile: a page whose lines every maps response carries.
People also hold durable preferences per domain, such as games, music, film or technology. An agent that advises in one domain needs that domain's preferences and none of the others.
Vaults already keep such preferences in long research notes. A typical shape is one hub note for a cross-domain through-line and one note per domain that inherits from it. These notes are too long to send without a search, and nothing selects them by domain. In practice, they reach the agent only when a search happens to rank them, often behind unrelated pages.
Profiles also go stale. Nothing proposes a new line when a new durable preference is saved, or retires a line that a later note contradicts.
Vaults also keep pick-by-problem routing tables: notes whose rows map a problem to the method or source to use. An agent finds such a table today only when an instruction file names it.

## What Changes

- Add a compiled `profile` note type. Every profile lives in one area, `Notes/Profiles`. A profile page whose `topics` name one registered domain is that domain's profile; a profile page with no topics is the global profile.
- Let a domain declare a parent domain, so a narrower profile, such as headphones under audio, inherits its ancestors' profiles.
- Serve, in a new `domain_profiles` block, the profile of each domain that the turn touches, together with that domain's ancestors' profiles. Several profiles can be active at once. Each fits a vault-settable budget, outside the turn's `max_chars`.
- Deliver profiles as session context: send a profile again only when its generation changes, using the continuity token.
- Detect when a profile needs curation: new durable preference or constraint units in its domain since its last update, or a profile line that is superseded, contradicted, expired or past its review date. Surface this as an upkeep item.
- The connected agent makes the curation judgment with its own model. It proposes the exact profile lines; the owner confirms them.
- Evaluate a pick-by-problem routing table as a vault skill against an ordinary note on real models, then specify the carrier that the evaluation supports.

## Capabilities

### New Capabilities

- `domain-profiles`: profile pages scoped to a domain, selected by the turn, delivered as session context.
- `profile-curation`: structural detection of curation needs, and agent-proposed, owner-confirmed profile edits.

### Modified Capabilities

- `command-surface`: a subject domain may declare a parent domain.

The global profile keeps the `stance` block of `add-stance-block-and-served-context`. The `domain-profiles` capability adds that a profile page with no topics supplies stance. It modifies no requirement of that active change.

## Impact

The implementation reuses:

- the stance block's compilation, admission, terminal egress, cache and held-generation pattern;
- the context catalogue and domain resolution;
- continuity generations;
- the dreamer's upkeep families, review-state dismissal and upkeep delivery;
- governed writes.

It adds no server-side model: the curation judgment comes from the agent that the person already connected.
Selection adds a bounded step to each maps response, so a before-and-after latency measurement on the recall latency instrument is part of delivery.
The profile area follows the note-type registry folder, so a later root-native relayout moves it with the other compiled areas.
