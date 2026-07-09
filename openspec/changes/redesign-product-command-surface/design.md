## Context

Current Exomem exposes the same canonical primitives across MCP, REST, and CLI:
`find`, `get`, `note`, `add`, `preserve`, `audit`, `reconcile`, tier-2 file
tools, graph tools, and media helpers. This is technically coherent, but it
pushes internal architecture into the user/agent interface. The previous
`simplify-command-surface` change added an action vocabulary and CLI aliases,
but it intentionally left the MCP/REST primitive surface intact.

The desired product direction is different: MCP, REST, and CLI should expose the
same fully capable product commands. The implementation should still reuse the
same deterministic canonical leaves so governance, validation, binary guards,
append-only rules, and pure-substrate boundaries remain centralized.

## Goals / Non-Goals

**Goals:**

- Replace the default public command surface with product commands that map to
  Exomem concepts: memory, sources, evidence, review, links, adoption,
  maintenance, files, datasets, and artifact transfer.
- Keep MCP fully capable. If a capability exists in REST/CLI, MCP must expose a
  product-command route unless the capability is terminal-local setup/admin.
- Reduce tool-call count by collapsing common multi-step flows into single
  product commands where safety remains explicit.
- Generate MCP, REST, CLI, OpenAPI, docs, annotations, and tests from one
  product command registry.
- Keep canonical leaves as shared internal implementation functions.
- Preserve pure-substrate discipline: server-side models remain deterministic
  measurement/extraction only, default-off where heavy, and soft-failing.

**Non-Goals:**

- No server-side reasoning LLM.
- No confidence/authority floats.
- No hidden destructive writes. Fix, delete, replace, move, and copy/adopt modes
  remain explicit.
- No attempt to copy Basic Memory naming or implementation directly.
- No promise of backward-compatible MCP tool names in this change.

## Decisions

1. Public surfaces use a `ProductCommand` registry.

   Rationale: the product command registry becomes the one public contract for
   MCP, REST, CLI, OpenAPI, docs, and schema fixtures. Canonical primitives stay
   available as implementation leaves, not as separately advertised public
   commands.

   Alternative considered: keep canonical commands visible and add friendly
   aliases. Rejected because that increases tool count and still requires agents
   to choose between product and primitive concepts.

2. Product commands are capability-complete, not minimal.

   Rationale: MCP must have the same practical power as REST and CLI. The goal is
   fewer concepts, not fewer capabilities. A product command may expose an
   `operation` or `mode` parameter when several canonical leaves form one user
   concept.

   Alternative considered: expose only 8-10 high-level tools. Rejected because
   it would hide file, evidence, media, transfer, and maintenance capabilities
   behind ambiguous mega-tools or make MCP weaker than CLI/REST.

3. Canonical leaves remain the internal authority.

   Rationale: Exomem already has extensive validation and governance in the
   canonical leaves. Product commands should compose those leaves, not reimplement
   path checks, type routing, index updates, or write guards.

4. MCP schema fidelity changes from old primitive baseline to product baseline.

   Rationale: this is an intentional public API redesign. The fixture should pin
   the new product tool set and require coverage from product tools to canonical
   capabilities, rather than requiring byte-identical primitive tools.

5. Heavy/model-backed behavior remains explicit.

   Rationale: embeddings, rerankers, CLIP, ASR, OCR, diarization, media frames,
   graph enrichment, and model-backed relation suggestion are deterministic
   measurement/extraction and are in bounds, but they must remain opt-in or
   mode-gated and degrade with clear guidance when unavailable.

## Risks / Trade-offs

- Breaking existing MCP clients -> This is accepted for the current product
  stage; reconnecting the connector and updating fixtures/docs is part of the
  change.
- Product tools become too broad -> Keep each tool anchored to one product
  object or workflow, and use typed modes only where they reduce conceptual
  duplication.
- Wrapper bugs bypass governance -> Product commands must call canonical leaves
  and tests must assert they do not duplicate write validation.
- Schema fixture churn -> Update fixtures intentionally and add exact-name/tool
  count assertions so future drift is visible.
- Reduced debuggability -> Keep internal leaves directly importable and covered
  by unit tests; CLI may expose an explicit admin/debug path only if needed.

## Migration Plan

1. Add product command definitions and coverage metadata over existing canonical
   leaves.
2. Generate MCP, REST, CLI, OpenAPI, and docs from product commands by default.
3. Update MCP schema fixtures and tests to expect the product tool set.
4. Update bootstrap and scaffold docs to teach product commands first.
5. Run focused MCP/REST/CLI parity tests, schema fidelity tests, scaffold leak
   tests, generated-doc checks, and the full suite.

Rollback: restore public-surface generation to canonical `COMMANDS` while leaving
the product command registry unused. No vault data migration is required.

## Open Questions

- Whether to keep a terminal-only `exomem internal <canonical>` debug path for
  maintainers. This should not be exposed through default MCP.
- Final product command names may be adjusted during implementation if tests or
  schemas show an overly broad tool.
