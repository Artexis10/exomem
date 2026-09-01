# Add the entity recurrence sensor (unresolved-identity v1)

## Why

Entity emergence is a flagship no-nudge commitment and a pre-registered
falsification family (f21 `entity_emergence`: an identity recurring with
reusable facts across at least three distinct sources SHALL surface an
entity-candidate signal). Today no recurrence signal exists anywhere:
`resolve_entity_candidate` is exact NFKC title/alias matching, the audit
detects unresolved wikilinks per page (`forward_reference`) but never counts
recurrence across pages, and an identity a user links five times from five
notes accumulates no candidate anywhere. The agent can only notice by luck.

## What Changes

- **A corpus-recurrence sensor over unresolved wikilinks.** A new audit
  category `entity_recurrence` counts, per NFKC-normalised identity, the
  distinct pages whose bodies carry an unresolved wikilink to it. An identity
  reaching the spread gate — and NOT resolving against the entity registry's
  titles and aliases — produces one finding carrying reason
  `unresolved_identity_recurs`, the candidate name, the sorted mentioning
  pages, and up to three registry near-matches (shared identity tokens,
  deterministic) so the agent checks before creating.
- **Advice, never creation.** The finding proposes that the agent consider the
  conservative-capture judgment it already owns (`proactive-entity-capture`:
  creation stays agent-side; a single mention never justifies it — and this
  sensor's spread gate is that rule made mechanical). The runtime creates
  nothing.
- **Resolution stays state-change-only.** Creating an entity page whose title
  or alias NFKC-resolves the identity silences it; so does creating the linked
  page itself (the link stops being unresolved). Deleting either brings the
  finding back. No dismissal memory beyond S6.
- **Delivery through existing machinery.** Registered in
  `EPISTEMIC_REVIEW_CATEGORIES` (opt-in — same posture as
  `scope_divergence_semantic`: a recurrence sensor meets an existing corpus
  where every candidate it will name is already linked). Findings are
  fingerprint-bound review items under S2 suppression and S6 family
  dispositions; attention-queue admission and family registration follow
  derivationally from the category registries, so no delta is filed for them.

## Non-goals

- **The proper-n-gram stream is deliberately deferred.** The architecture
  report named plain-text recurring proper n-grams as a second evidence
  stream; it is exactly the incidental-mention false-positive surface whose
  budgets f21 freezes behind the three-expert calibration study. v1 ships the
  explicit, high-precision stream only; the n-gram stream is a future change
  once calibration lands.
- No embedding, no model call, no new index, no write-time work: identity
  assist is lexical (NFKC identity tokens), because no entity-title embedding
  index exists and the sensor may not add one.
- No bootstrap-contract change: the active `harden-write-and-entity-capture`
  change owns entity-capture teaching.
- No bench-constant changes; f21 stays withheld and calibration-owned.

## Impact

- Affected specs: `action-first-audit` (new category).
- Affected code: new `entity_recurrence.py` (or a sibling section), `audit.py`
  category registration and sweep, tests.
- Falsification: f21 `entity_emergence` (registered, withheld); the
  frequency-matched incidental-mention twin maps to this sensor's
  out-of-scope plain-text stream and the below-spread twin.
