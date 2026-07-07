# productize-cognition-layer — design

## Context

Exomem already has the deep pieces: governed writes, typed pages, sources,
evidence, supersession, audit/attention, media extraction, hybrid search,
overview, setup, bootstrap, and a registry-driven command surface. The product
problem is that those pieces are visible as machinery instead of being composed
into a simple experience.

The design principle is: expose verbs, not ontology.

## Goals / Non-Goals

**Goals:**

- Make Exomem legible as a durable cognition layer, not just a notes/search
  tool.
- Make onboarding an existing vault safe, bounded, and obvious.
- Provide a simple front-door tool vocabulary for agents.
- Keep internal page types and governance intact.
- Introduce knowledge packs as extensible schema/workflow bundles.
- Clarify Evidence as proof/case-bound material, not a generic raw-input bucket.

**Non-Goals:**

- No automatic rewrite of existing vaults.
- No server-side LLM classification or summarization.
- No full SaaS/multi-tenant hosting build in this change.
- No removal of existing advanced tools.
- No attempt to match Basic Memory's note grammar.

## Decisions

1. **Adoption is scan-first and read-only by default.**
   The first-run path for an existing vault is `overview` plus an adoption
   report/manifest. The default mode is no writes outside `Knowledge Base/`,
   and no writes at all until the user chooses setup/adoption actions.

2. **A sidecar/adoption manifest is the product bridge.**
   The manifest records what was found: folder clusters, file counts,
   frontmatter coverage, wikilinks, likely domains, media, broken links,
   duplicates, stale areas, and suggested pack mappings. It is deterministic
   and re-runnable. It does not claim to understand the content semantically.

3. **Knowledge packs compose primitives.**
   A pack defines recommended page types, frontmatter extensions, routing
   hints, evidence/case semantics, review checks, and example prompts. It does
   not hard-code a separate storage engine or require a new top-level folder for
   every domain.

4. **Evidence is a role, not a dumping ground.**
   A Source becomes Evidence when preserved or cited for a claim, case, dispute,
   warranty, legal matter, insurance issue, medical record, purchase record, or
   other proof-bearing context. The product may call this "proof" or "case
   files" in user-facing docs.

5. **The MCP surface gets a front door, not a replacement.**
   Existing typed operations remain the authoritative implementation. The
   product layer adds readable intent operations/aliases and stronger tool
   descriptions so agents can route user intent without exposing all internals.

6. **Docs split by audience.**
   Human docs explain the mental model and workflows. Agent docs explain when
   to save/search/prove/review/update. Developer/admin docs explain schemas,
   packs, storage, and command surfaces.

## Pack Shape

Initial pack metadata should be small and inspectable, for example:

```yaml
id: legal-warranty
name: Legal / warranty
description: Cases, purchases, disputes, receipts, correspondence, and proof.
actions:
  save:
    prefer: source
  prove:
    prefer: evidence
  update:
    prefer: decision
fields:
  case:
    required: [title, status]
  evidence:
    required: [case, why_preserved]
review:
  - open_deadlines
  - missing_receipts
examples:
  - "Save this receipt for the laptop warranty case."
```

The exact file format can evolve, but it must stay declarative and generic.

## Front-Door Tool Mapping

The simple verbs are allowed to be aliases or thin orchestration leaves at
first:

- `save` routes to `add`, `note`, `link`, or `preserve` depending on explicit
  intent and pack guidance.
- `ask` wraps `find`/`get`/`pack` retrieval guidance and returns cited material
  for the agent to reason over.
- `prove` finds or creates case-bound evidence links.
- `review` fronts `attention`, `audit`, and unprocessed-source queues.
- `update` routes to `edit`/`replace` with an explicit reason.
- `adopt` fronts existing-vault scan and optional manifest/proposal creation.

Where ambiguity matters, the tool should return a short clarification request
or candidate actions rather than guessing.

## Risks / Trade-offs

- **Ontology sprawl:** packs could become a new complexity layer. Mitigation:
  packs compose durable primitives and expose simple verbs.
- **Over-promising adoption:** scan/manifest is not semantic migration.
  Mitigation: docs and output say "suggested mapping" and "proposed
  compilation," not "understood everything."
- **Tool duplication:** front-door verbs could overlap with typed tools.
  Mitigation: registry metadata marks simple tools as primary and advanced tools
  as secondary; implementation reuses the existing leaves.
- **Evidence naming:** "Evidence" may sound legalistic. Mitigation: keep the
  internal term, but allow user-facing "proof" / "case file" language.

## Open Questions

- Should `ask` be a real MCP tool, or should bootstrap teach agents to compose
  `find(pack=true)` + `get`? The first tranche can specify behavior and defer
  implementation if it risks server-side reasoning.
- Should adoption manifests live under `Knowledge Base/_Adoption/`, in a hidden
  sidecar directory, or only in command output until the user chooses to save?
- What is the minimum useful first pack set: legal/warranty, creative,
  technical, health/athletic, business, personal records?
