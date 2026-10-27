## Why

Context maps group a vault's material by topic. A topic is a domain from the vocabulary registry, and a project key joins a topic through its `domain` field in `project-keys.yaml` (`compile-context-maps-and-profiles`, D3). Few vaults declare these fields. Without them the map is one recency list on every turn, and a declared value that the domains registry does not resolve joins no topic.

Today a person must edit YAML by hand to declare a domain. Declaring domains should be ordinary agent work: the agent infers them from the vault, declares them through the governed route, and the person can see and correct them, both at onboarding and later.

## What Changes

- Add a vocabulary family, `project-domain`, to the agent-led vocabulary workflow (`activate-agent-led-vocabulary-evolution`). It follows that change's family contract:
  - its own evidence and validation;
  - the project-key registry writer as its persistence owner;
  - the shared fingerprinted review items and decisions.
- Add `schema_memory` operations for project-key domains:
  - `inspect` lists each project key with its declared domains, the origin of each declaration and its resolution state;
  - `propose` previews a delta;
  - `save` applies it under `expected_hash` and `why`.

  A save may also register a new domain key through the existing domains registry writer.
- Record an origin per declaration, `inferred` or `reviewed`. Record a person's rejections, so that the agent does not propose a rejected domain again for the same key.
- Expose a bounded consideration when the catalogue counts project keys without a domain, or declarations that do not resolve. The agent judges the meaning: it reads the key's pages, chooses existing domains or a new one, and saves with origin `inferred`. The server never calls a model and never decides meaning with word lists.
- Make domain review part of onboarding (`existing-vault-onboarding`, `agent-memory-onboarding`). The person sees the inferred domains and can accept, rename or reject each one. An accept sets origin `reviewed`, a rename adds a domains-registry alias, and a reject removes the declaration and records the rejection.
- Teach the workflow in the generic skill scaffold.

## Capabilities

### New Capabilities

- `project-domain-declaration`: inference by the agent, governed declaration, origin and rejection records, and review.

### Modified Capabilities

None. The `project-domain` family and the onboarding review step are requirements of the new capability. `agent-vocabulary-workflow` is still an unarchived change, so this change builds on its family contract instead of modifying it.

## Impact

- Code: `project_keys.py`, the `schema_memory` command leaf, `context_catalogue.py`, the review-item producer, and the skill scaffold under `src/exomem/_scaffold/_Schema/`.
- Vocabulary lives in registries. Code branches only on the closed origin and decision values that this change's schema declares.
- No vault is changed by this proposal. The first live use on a personal vault runs through the governed route after release, coordinated with the owner of that vault's configuration hold.
