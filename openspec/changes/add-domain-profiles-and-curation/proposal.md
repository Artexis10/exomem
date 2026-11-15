# Proposal

## Why

A vault can now serve one global profile: a stance page whose lines every maps response carries.
People also hold durable preferences per domain, such as games, music, science or technology. An agent advising in a domain needs that domain's preferences, and needs none of the others.
Today such preferences sit in scattered notes, and the stance contract forbids scoped stance, so no domain profile reaches the agent unless it searches.
Profiles also go stale: nothing proposes a line when a new durable preference is saved, or retires a line that a later note contradicts.

## What Changes

- Add a `profile` note type in the shipped note-type registry, partitioned by domain, so every profile lives in one area with one folder per domain and optional deeper folders.
- Serve the global profile on every maps response, as today, and serve each domain profile whose domain the turn touches. Several profiles can be active at once, each within a vault-settable budget.
- Deliver profiles as session context: send a profile again only when its generation changed, using the continuity token.
- Detect when a profile needs curation: new durable preference or constraint units in its domain since its last review, or a profile line that is superseded, contradicted or past its review date. Surface that as an upkeep item.
- The connected agent does the curation judgment with its own model. It proposes profile edits as a governed preview; the owner confirms them, and promotion authority follows the stance rules.

## Capabilities

### New Capabilities

- `domain-profiles`: profile pages scoped to a domain, selected by the turn, delivered as session context.
- `profile-curation`: detection of curation needs and agent-proposed, owner-confirmed profile edits.

### Modified Capabilities

- `stance-block` (in `add-stance-block-and-served-context`): a page scoped to a domain becomes a domain profile instead of ordinary knowledge.

## Impact

The implementation reuses the stance block, the context catalogue, domain and topic resolution, continuity generations, the dreamer's upkeep families, the review queue and governed writes.
It adds no server-side model: curation judgment comes from the agent the person already connected.
Profile placement follows the vault layout decision; when store-mode layout lands, the area moves with it.
