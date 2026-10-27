## Why

Context maps group a vault's material by topic. A topic is a domain from the vocabulary registry. Today an entry joins a topic in three ways (`compile-context-maps-and-profiles`, D3):
- through its own `topics` field;
- through the `domain` field of its project key in `project-keys.yaml`;
- through an authored membership relation.

Few vaults declare any of these. Without them the map is one recency list on every turn.

A census of one dogfood vault shows that declarations on a few shared carriers would file most of what a map can show:
- project keys could file about 60% of the notes;
- tags could file most of the notes that sit in note-type folders without a project key, about a quarter more;
- topic folders, which group notes by subject rather than by note type, could file most of the rest.

None of the vault's project keys declared a domain.

Today a person must edit YAML by hand to declare any of this. Declaring topics should be ordinary agent work: the agent infers them from the vault, declares them through a governed route, and the person can see and correct them, both at onboarding and later.

## What Changes

- **Three carriers of topic declarations:** a project key, a folder and a tag. Every declaration that Exomem writes lives in one new overlay registry, `topic-carriers`, and names the domains its carrier's entries join. A project key's hand-written `domain` field stays a read input.
- **Topic membership** also comes from:
  - the `project` carriers of the entry's project keys;
  - the folder carriers that contain an entry;
  - the tag carriers that match the entry's tags;
  - the home folder that holds the entry.

  It stays declared: no word list, text pattern or model call files an entry.
- **Governed writes:** `schema_memory` subject `topic-carriers` uses the generic registry contract (`inspect`, `propose`, `save`, `history`, `restore`) with `expected_hash` and `why`.
- **Origin and rejection.** Every declaration records its origin, `inferred` or `reviewed`. A person's rejection is remembered, so the agent does not propose that domain again for that carrier.
- **A vocabulary family, `topic-carrier/v1`,** on the agent-led vocabulary workflow (`activate-agent-led-vocabulary-evolution`). It offers a bounded consideration for each carrier whose declaration would file unfiled entries.
  - The server supplies evidence and ranks carriers by the unfiled entries they would file.
  - The agent judges the meaning.
- **Domain review in onboarding.** Adoption lists the inferred declarations for review. The person can accept, replace or reject each one. Bootstrap advertises the route.
- **The skill scaffold teaches the workflow.**

## Capabilities

### New Capabilities

- `topic-carrier-declaration`: carriers, membership through carriers, inference by the agent, governed declaration, origin and rejection records, and review.

### Modified Capabilities

None. `context-catalogue` membership and `agent-vocabulary-workflow` are still unarchived changes, so this change states its additions as requirements of the new capability instead of modifying them.

## Impact

- Code:
  - a new `topic_carriers` registry module, and `homes.py` for resolved home topics;
  - `context_views.py`, for the cached map key and caller-view admission;
  - `context_catalogue.py`, which stores entry tags in place and joins the carriers; one background fold fills the tags after upgrade;
  - the vocabulary family and its producer;
  - adoption next actions;
  - the skill scaffold under `src/exomem/_scaffold/_Schema/`.
- Vocabulary lives in registries. Code branches only on the closed carrier kinds, origins and decisions that this change's schemas declare.
- No vault is changed by this proposal. The first live use on a personal vault runs through the governed route after release, coordinated with the owner of that vault's configuration hold.
