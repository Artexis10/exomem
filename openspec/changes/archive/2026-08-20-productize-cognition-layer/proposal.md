# productize-cognition-layer

## Why

Exomem's technical depth is not yet legible as a product. The current surface
exposes the real architecture — Sources, Notes, Entities, Evidence, audit,
attention, supersession, media extraction, and search controls — but asks the
user and many agents to understand too much of that ontology up front.

Basic Memory is ahead on packaging because its first-run story is simple:
Markdown notes become a searchable graph. Exomem's stronger thesis is broader:
an evolving knowledge base with sources, proof, history, decisions, records,
review, and agent-safe governance. That thesis needs a product layer that makes
the depth feel simple instead of complicated.

The immediate gap is not another retrieval lane. It is seamless onboarding and
adoption:

- a user with an existing vault should see what Exomem found, what stays
  untouched, and what can be safely compiled into the governed layer;
- an agent should have a small set of clear verbs instead of exposing every
  internal operation as a first-choice tool;
- common user domains should be expressible as extensible knowledge packs rather
  than hard-coded folder sprawl;
- docs should explain the simple mental model: built-in AI memory remembers how
  to work with the user; Exomem stores durable governed knowledge.

## What Changes

- Add a first-class **cognition layer** product contract: Exomem is the durable
  knowledge base with sources, proof, history, decisions, records, and review.
  The internal ontology remains, but the public workflow is expressed through
  simple actions: save, import/adopt, ask, prove, review, update, connect.
- Add an **adopt existing vault** workflow that upgrades the existing
  `overview`/setup behavior from "documented safety" to a guided product loop:
  read-only scan, bounded report, safe mode choices, optional sidecar/adoption
  manifest, and proposed compilations into `Knowledge Base/`. Originals are
  never rewritten by default.
- Add **extensible knowledge packs** as a product concept: small schema/workflow
  bundles for common domains such as legal/warranty, creative, technical,
  athletic/health, business, and personal records. Packs compose the durable
  primitives rather than creating a rigid global taxonomy.
- Add a **simple agent front door** over the existing registry: clear intent
  operations or aliases for `save`, `ask`, `prove`, `review`, `update`, and
  `adopt`, backed by the existing typed operations. Advanced tools remain
  available but are secondary.
- Update bootstrap/skill/docs so agents know when to use model memory versus
  Exomem, when to create source/evidence/compiled knowledge, and how to avoid
  leaking ontology complexity to the user.

Pure-substrate note: this change adds routing, manifests, schema metadata,
deterministic scans, and agent guidance. It does not add server-side reasoning.
Classification is rule/schema based unless a human or external agent explicitly
decides what to compile.

## Capabilities

### New Capabilities

- `cognition-layer`: user-facing durable knowledge model, simple actions,
  knowledge packs, evidence-as-proof semantics, and adoption workflow.

### Modified Capabilities

- `guided-setup`: setup must surface the cognition-layer model and route users
  with existing vaults into adoption rather than only init.
- `command-surface`: the registry must expose a simple front-door vocabulary
  for agents while preserving advanced operations.
- `agent-bootstrap-contract`: bootstrap must teach generic agents the simple
  workflow and the complementary relationship with built-in AI memory.

## Impact

- Specs: new `cognition-layer` spec plus deltas for `guided-setup`,
  `command-surface`, and `agent-bootstrap-contract`.
- Likely implementation areas:
  - `src/exomem/commands.py` registry metadata/aliases/front-door operations.
  - setup wizard and/or a new `adopt` command.
  - scaffold `SKILL.md` and operation guidance.
  - README/QUICKSTART/docs concept pages.
  - sample vault showing source → compiled note → evidence/case trail.
- Tests should cover adoption read-only guarantees, registry/alias visibility,
  bootstrap guidance, knowledge-pack loading/validation, and scaffold leak
  safety.
- This change should absorb or supersede the narrower
  `document-existing-vault-onboarding` docs-only change.
