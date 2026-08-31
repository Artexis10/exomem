## Why

Exomem currently ships one code-owned behavioural rule for software work: Planning owns durable intent while OpenSpec and the repository own implementation truth. That is a useful personal pairing encoded as a product assumption. Exomem Planning must instead work fully standalone or coordinate with any user-declared external tool, while Records closes the loop from intended work to observed reality.

This exposes a broader product opportunity: make each vault's agent behaviour explicitly personalisable through user-authored, inspectable contracts. The source must be strict structured data for deterministic validation and resolution, with a faithful plain-English rendering for people rather than executable prompt prose.

## What Changes

- Add a reusable, code-owned contract-family substrate and the first family, `workflow`, whose user-authored instances live as ordinary human-readable Markdown under `_Schema/contracts/workflow/`.
- Define a versioned workflow-contract schema covering scope selectors, standalone or companion Planning mode, open-vocabulary companion tools and owned execution artifacts, proactive intent/outcome capture posture, and explicit Planning transition posture.
- Resolve contracts deterministically from an explicit contract selection or bounded project/domain/activity context, with documented precedence, ambiguity refusal, an absence-safe standalone default, and no server-side language model.
- Keep hard product invariants outside user contracts: Planning remains intended future state, Records remains observed reality, governance still gates disclosure, and no contract can authorize a hidden capability or mutate an external system.
- Extend the existing schema/configuration product surface to inspect, validate, resolve, preview, and save reviewed workflow contracts with stale-write guards. A resolver returns both a canonical machine decision and a deterministic English explanation with provenance and a contract fingerprint.
- Replace the hard-coded OpenSpec rule in bootstrap, scaffold, Planning, and Records guidance with generic contract-aware behaviour. OpenSpec remains a supported example companion, not a privileged product dependency.
- Teach agents to inspect existing Planning when durable intent is stated, update a matching plan rather than duplicate it, follow the resolved companion boundary, record observed outcomes, and propose or perform only explicit Planning transitions.
- Keep companion integration links-first in this delivery. Contracts declare authority and references; they do not introduce bidirectional synchronization, polling, webhooks, or tool-specific adapters.

## Capabilities

### New Capabilities

- `workflow-contracts`: User-authored contract storage, schema, deterministic resolution, provenance, human rendering, invariant boundaries, and extension model.

### Modified Capabilities

- `planning`: Planning becomes standalone by default and contract-aware when coordinating with declared companion tools; durable-intent routing and non-duplication become explicit requirements.
- `records`: Observed outcomes participate in a contract-aware Planning feedback loop without inferring completion or external truth.
- `agent-bootstrap-contract`: Bootstrap serves and teaches applicable workflow contracts instead of a hard-coded OpenSpec boundary.
- `portable-agent-contract`: Runtime JSON, scaffold/skill guidance, and human contract renderings remain aligned across generic MCP, plugin, skill, CLI, and REST clients.
- `product-command-surface`: The existing schema/configuration command gains workflow-contract inspect, validate, resolve, preview, and guarded-save operations across the shared registry surface.

## Impact

The change affects the per-vault `_Schema` scaffold and portability contract, contract parsing/validation/resolution modules, `schema_memory`, bootstrap projection, Planning and Records guidance, thin execution-pointer validation, knowledge-pack wording, generated MCP schema fixtures, and focused command/contract tests. Existing Planning/Records files need no representation rewrite. The behavioural default does require an explicit semantic migration: every known pre-feature vault receives a durable review marker before scaffold refresh and refuses silent standalone fallback until the user selects standalone for the session or saves a reviewed standalone/companion contract.

No external integration SDK, background synchronization service, generative server model, or new product command is introduced. Existing governance and egress checks remain authoritative over every projected contract and referenced artifact.
