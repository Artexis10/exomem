## Context

Exomem already has a rich Claude Skill scaffold, but MCP clients that do not load
that skill only see tool schemas. The command registry is the correct place to add
a portable contract because it generates MCP, REST, CLI, and OpenAPI surfaces from
one leaf function. Existing `find` timing diagnostics and compute mode resolution
already expose the measurements needed to explain performance, but agents need a
single place to learn how to interpret them.

## Goals / Non-Goals

**Goals:**

- Give generic MCP clients a read-only `bootstrap` call that returns Exomem's
  operating contract in structured form.
- Keep the contract deterministic, generic, and public-safe.
- Preserve default response shapes for `find` and `/upload` while adding useful
  metadata when those paths already return structured JSON.
- Keep all public surfaces generated from the existing command registry.

**Non-Goals:**

- No server-side LLM, agent reasoning, confidence scoring, or automatic policy
  enforcement.
- No change to retrieval ranking, reranking heuristics, upload authorization, or
  media extraction behavior.
- No requirement that every MCP client automatically calls `bootstrap`; docs and
  tool discoverability make the intended call explicit.

## Decisions

- **Registry command, not hand-registered MCP only.** `bootstrap` is a normal
  read-only command with MCP/REST/CLI surfaces so generic clients and scripts get
  the same contract. Alternative considered: hand-register only in MCP. Rejected
  because it would violate the command-surface single-source rule.
- **Static-plus-runtime payload.** Most contract content is static guidance; runtime
  fields come from cheap local state such as package version and `mode.resolved()`.
  This keeps the operation deterministic and within the pure-substrate constraint.
- **Profiles are verbosity controls.** `compact` is the default for first-session
  use, `full` adds longer examples, and `diagnostics` adds performance-oriented
  interpretation. Invalid profile values fail fast.
- **Upload metadata is additive.** Existing `path` and `sidecar_path` stay in the
  response; new fields add hash/size/media identity so agents can report outcomes
  without guessing.
- **Timing profile metadata remains opt-in.** `find` diagnostics stay behind
  `include_timings=true`; the bootstrap contract teaches agents when to request
  them instead of changing normal lookup cost or response shape.

## Risks / Trade-offs

- [Risk] Some clients may not call `bootstrap`.
  -> Mitigation: name and describe the tool clearly and document it in generic
  client setup instructions.
- [Risk] The bootstrap prose can drift from the shipped Claude Skill.
  -> Mitigation: keep it compact, test for key invariants, and avoid duplicating
  every detail from `_Schema/SKILL.md`.
- [Risk] Upload hashing could increase memory usage.
  -> Mitigation: compute hash and size during streaming copy, not by reading the
  whole file after upload.
- [Risk] MCP schema snapshot churn is intentional.
  -> Mitigation: update the fixture only after tests prove the new tool is the
  expected registry-generated addition.
