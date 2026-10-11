# Design: Teach Cross-Domain Category Examples

## Context

`_build_portable_categories()` in `src/exomem/semantic_authoring.py` owns the taught
examples (`examples: {role, domain, rich}`); `render_concise()` projects them into the
contract block that must appear byte-identically in eleven carriers (scaffold SKILL.md,
nine workflow skills, `docs/semantic-language.md` between generated markers), while
`render_tool_guidance()` (one role example) flows into generated MCP tool descriptions
and `bootstrap_projection()` into both bootstrap profiles. Example changes are therefore
normative contract changes: the digest and version must move together, and every
projection surface must be regenerated in the same change.

## Goals / Non-Goals

- Goal: an agent reading any authoring touchpoint sees the category system apply to
  life, health, finance, legal, career — not only software.
- Goal: keep contract-block token growth small (~4 lines per carrier).
- Non-goal: expanding the sixteen-key core. Domain breadth is expressible through open
  vocabulary plus examples; the core-pinning tests stay authoritative.
- Non-goal: changing advisory/non-blocking semantics, ranking, or registry content.

## Decisions

1. **Three placement tiers.**
   - Tier 1 (projected block, cost × 11): swap `role` and `domain` examples to
     non-software domains; add `examples["breadth"]` — exactly four compact lines:
     one retained software line plus finance, legal/travel, and career lines; swap
     `rich` to a life-domain Decision with identical feature coverage.
   - Tier 2 (scaffold prose, cost × 1): a "One contract, every domain" subsection in
     the scaffold SKILL.md before the projected block, carrying a broader fenced
     example set (music, health, relationships, nutrition-finance crossover). Fences
     keep doc examples non-indexable per scaffold convention.
   - Tier 3 (auto-flowing): tool guidance single example becomes non-software
     automatically; bootstrap keeps dropping only `rich` in compact profile so
     `breadth` ships in both profiles (~70 tokens); tool-surface fixtures, plugin
     skills, capability docs regenerate.
2. **Contract version 3 → 4.** Examples are normative contract content; the normative
   identity digest changes either way, so the version literal moves with it. The three
   `v3 ` marker literals in tests move to `v4 `.
3. **Breadth lines must be executable teaching.** Each breadth example must parse to
   exactly one valid semantic unit; at least two must resolve `core` (role-first
   selection on non-software content) and at least one must resolve `unregistered`
   (open-vocabulary domain escape, e.g. `career`), proving the role-or-domain heuristic
   rather than merely describing it.
4. **Connector rollout follows the existing pending-sha protocol.** The tool-surface
   hash churns, so `deploy/chatgpt/personal-plugin-contract.json` records the new sha as
   pending with `refresh_required: true` rather than flipping the deployed sha.
5. **Generic archetypes only.** Examples are invented archetypes; the no-leak gate (no
   personal, product, or competitor tokens in shipped sources) runs early in the lane.

## Risks / Trade-offs

- Digest churn touches many pinned fixtures; mitigated by a deterministic re-projection
  sweep (replace the old block found verbatim, count-verified, in every carrier) and by
  running the parity gates in the focused suite.
- Overcorrecting to zero software examples would misteach a developer-heavy install
  base; mitigated by retaining one software line in `breadth` and the ops-flavored
  remediation examples.
