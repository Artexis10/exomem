# Design

## Context

`project-keys.yaml` already accepts `domain: <key>` or a list of keys per project key, and `context_catalogue` counts each value that does not resolve as an unresolved declaration (`compile-context-maps-and-profiles`, D3). Two writers exist today:
- `register_project_key` auto-registers new keys on use;
- `schema_memory` saves the `domains` vocabulary registry with `expected_hash` and `why`.

No governed writer sets a key's `domain`, and nothing records whether a person has reviewed it.

## Decisions

### D1. The agent infers; the server supplies evidence and validates

Inferring a topic from a project's pages is a judgement about meaning, so the active agent makes it. The server gives bounded evidence per key:
- the key's folder, category and page count;
- a sample of member titles with their one-line catalogue descriptions;
- the existing domains, with their descriptions and aliases.

The server validates the save. The server calls no model, and no word list or text pattern proposes a domain.

### D2. One family on the shared vocabulary workflow

`project-domain` is a vocabulary family under the family contract of `activate-agent-led-vocabulary-evolution`:
- **Identifier:** `project-domain/1`.
- **Evidence:** D1.
- **Validator:** the key exists, and each value resolves through the domains resolver or is registered in the same save.
- **Persistence owner:** the project-key registry writer.
- **Decisions:** declare, accept, rename, reject and defer.
- **Authority:** the same mapping as the domains registry save.

Its review items use that change's fingerprints, so unchanged advice is not repeated, and a defer or quiet suppresses it without hiding the state.

### D3. Origin and rejection live next to the declaration

Each project key may carry two fields:
- `domain_origin: inferred | reviewed`, one value per declared domain, written as a map from domain key to origin;
- `domain_rejected: [<domain key>, ...]`.

These are closed values that this schema declares. A declaration without an origin, such as one a person wrote by hand, reads as `reviewed`. The agent never proposes a domain that is listed in `domain_rejected` for that key. A rename registers an alias in the domains registry and keeps the key's declaration pointing at the canonical domain.

### D4. Automatic, then reviewable

The consideration appears in ordinary work when a project key has member pages and no domain, or when a declaration does not resolve. The agent may declare with origin `inferred` without a per-key approval, under the vault's vocabulary authority. The map uses inferred declarations at once. Inferred declarations stay visible in `inspect` and in onboarding review until a person accepts, renames or rejects them. Origin `inferred` never blocks the map.

### D5. Onboarding

`adopt_vault` and the first-session bootstrap run inference for undeclared keys, then show the person the result in one list: key, domain, origin, and a few member titles. The person can accept all, change single rows, or skip; skipping leaves the declarations `inferred`.

## Risks

- **Wrong inference.** A wrong domain groups pages under the wrong topic. The cost is a worse map, not lost data. Review, rename and reject are always available, and rejections are remembered.
- **Write contention on `project-keys.yaml`.** Saves use `expected_hash`, so a stale save is refused, not merged.

## Proof plan

- **Highest-level check:** in a fixture vault with undeclared keys, the agent workflow saves inferred domains through `schema_memory`. The next maps packet then shows topics, and `read_memory` on each topic's domain map returns its members.
- **Tests:**
  - a stale `expected_hash` is refused;
  - a value that does not resolve is refused unless registered in the same save;
  - a rejected domain is never proposed again;
  - a hand-written declaration reads as `reviewed`;
  - a rename adds an alias and keeps membership;
  - the consideration is suppressed when unchanged and returns when the evidence fingerprint changes.
- **Real-model check:** on dogfood vaults, an agent given only the consideration declares domains that a blind judge rates as fitting the key's pages. Include hostile cases: a key with one page, a key mixing two topics, and a key in another language.
