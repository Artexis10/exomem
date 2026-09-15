# Design: add-context-activation

## Context

Recall answers "what stored material resembles this text?" and the typed graph answers
"what is connected to an anchor?". Neither answers the product question a fresh agent
has on an ordinary turn: which durable user/project/domain context should be in working
memory right now. Audit at `main` 19762189 (2026-09-16): the read path is
`op_ask_memory → op_find → release gate → find() → annotate_hits → referents → project_hits`;
the referents stage (`commands.py:2550-2574`) is the precedent for a read-only,
categorical, abstaining stage placed after the release gate; `deep=true` is
`find(pack=true)`; derived stores live under `state_paths.vault_state_dir()` with
`embedding_index.py` as the generation-token/copy-on-write pattern; Records and
Planning items are excluded from recall (`recall_policy.py:181-231`) and are queried
through `record_governance.query_collection` and `planning.query`; claims routing exists
(`collection_claims.route`, `MIN_CLAIM_COVERAGE=2`); `epistemic_graph.graph_context` is
an importable typed-neighbourhood API with five builtin traversal profiles; the graph
lane discards corroboration for pages already in the primary set
(`find_candidates.py:633,680,730`); no cross-kind current-state resolver exists;
`activation` already names two unrelated mechanisms (`usage.py:156`, `attention.py:522`)
and `activation.py` is corpus-adoption measurement, so new modules are named
`working_set*` and `context_roles`. The constitution (`openspec/config.yaml:13-33`)
forbids any server-side reasoning model, so disambiguation belongs to the active agent.

## Goals / Non-Goals

Goals: accept the raw turn; resolve anchors with abstention; select roles
deterministically; retrieve through existing primitives only; return a bounded,
auditable, provenance-bearing packet; leave `ask_memory` byte-identical; stay
index-backed so the operation never inherits the whole-vault eligibility walks that
make live hybrid recall cost seconds today.

Non-goals: host hook injection, continuity tokens, `anchor=` override, hot profile,
usage-prior tuning, consolidation/dreaming, any change to find ordering, any new
canonical page type.

## Decisions

- **D1 — Standalone Tier-1 tool, not a parameter.** `activate_context` is its own
  operation following the `add-vault-overview` shape (leaf → `op_activate_context` →
  MCP/CLI/REST). A raw turn is not a query and the packet is not a hit list; the
  surface-hash and hosted-tree regeneration cost is paid once, and the ChatGPT
  connector adopts the tool at the next release. The tool stays on the surface under the
  kill switch and abstains, so the surface digest is environment-independent.
- **D2 — Index mirrors `embedding_index.py`.** `.working-set.sqlite` with a meta-version
  row, a write-generation token read back inside the write transaction, a copy-on-write
  in-process cache, scoped wipe on schema mismatch. Tables: `anchors`, `anchor_aliases`,
  `anchor_terms` (FTS5), `anchor_vectors`, `anchor_links`, `anchor_categories`,
  `anchor_roles`, `meta`. Sources are governed structure only; signatures are structural
  (title, lede, headline sections, unit categories, manifest field names) — never
  generated. Signature embeddings reuse the configured embedding backend and are
  optional (absent under `EXOMEM_DISABLE_EMBEDDINGS`, in which case `vector_band` is
  simply never produced).
- **D3 — Evidence kinds are categorical and the rule is the referents rule.**
  `exact_alias` (NFKC-casefold title/alias match), `lexical_overlap` (FTS rank band
  over `anchor_terms`), `vector_band` (strong/weak cosine bands over `anchor_vectors`,
  thresholds pinned in `ranking_config`, never emitted), `category_match`,
  `claims_match` (delegates to `collection_claims.route`), `retrieval` (a fused hit from
  a small-limit `find()` that is or links to the anchor), `graph_corroboration` (typed
  edge between candidates from `anchor_links`, counted even when both are in the primary
  set — the compiler does not inherit the find lane's discard), `usage_prior` (tie-break
  only, from `usage.usage_multiplier`). Resolution: `exact_alias` or ≥2 independent
  non-usage kinds → `resolved`; one strong kind with competitors → `partial`; ≥2
  resolved anchors with disjoint `anchor_links` neighbourhoods → `ambiguous`; else
  `unresolved` → abstain. The ambiguity is returned with both anchors; the active agent
  chooses (the `anchor=` override lands with the host change).
- **D4 — Roles are a versioned registry, not code.** `context-roles.yaml` (scaffold +
  plugin copy, byte-identical, no-leak gated) declares role id, lane, category set,
  anchor-kind defaults and cue patterns; `context_roles.py` loads it, applies vault
  overrides (add/narrow only), computes `roles_hash`, and selects ≤6 roles as defaults ∪
  cue matches in registry priority order. Missing or broken override → shipped registry
  plus a warning in `generation`, mirroring `traversal_profiles`.
- **D5 — Lanes reuse primitives; nothing new touches the vault.** Units via the
  existing unit search with category pushdown restricted to the anchor neighbourhood
  paths; Records via `record_governance.query_collection` on collections whose claims
  route to the anchor; Planning via `planning.query(lifecycle="active")` filtered by
  anchor tags/refs; entity facets from the entity registry snapshot; graph via
  `graph_context(traversal_profile=…)` at depth ≤2 from `resolved` anchors (depth 1 from
  `partial`); evidence as pointers only. Per-role caps and a global `budget_chars`
  (default 4,000, hard 8,000) enforce abstinence; units precede pages; overflow becomes
  pointers.
- **D6 — Current-state resolver is small and source-marked.** For a resource or
  collection anchor: latest Records item(s) by `observed_on`/`updated` in a claiming
  collection → status fields on the profile page → latest active note in the
  neighbourhood. The packet names the source. This closes the "resource shipped abroad
  but recommended" class without a lifecycle model.
- **D7 — Egress: same release object as `project_hits`.** `guard_working_set` sits
  beside `guard_referents` (`governance/egress.py:2007`), deep-copies the packet, drops
  every field naming a withheld path (including wikilink syntax) and drops evidence that
  depended on a withheld neighbour. `purpose` flows exactly as for recall.
- **D8 — Freshness.** Packet cache key = recall freshness key + index generation +
  roles hash. Managed runtimes never build the index inline on a request (single-flight
  background build, `index_warming` abstention once), mirroring the entity-registry
  warm; unmanaged runtimes build inline within the request budget.
- **D9 — Instrumentation and gates.** Spans `working_set.index/resolve/roles/lanes.<role>/budget`
  under the existing collector; `sum(stages) <= total_ms` asserted by a real-call test;
  `tests/test_latency_gate.py` gains `CEIL_WORKING_SET_MS` and a 2k→8k ratio bound.
- **D10 — Kill switch.** `EXOMEM_DISABLE_WORKING_SET` → no index is created or opened,
  the tool returns `abstained: true, abstention.reason = "disabled"`; `ask_memory` is
  unaffected either way.
- **D11 — Derived artifacts regenerate in the same PR.** Schema fixture, both surface
  digests, hosted plugin/candidate trees, `docs/capabilities.md`, README, SKILL recall
  loop line (balanced/maximal: "for a substantive turn with no prior context, call
  `activate_context` with the user's turn").

## Alternatives rejected

- `ask_memory(activate=true)`: cheapest surface change, but conflates a turn with a
  query and a packet with hits; rejected by the owner in favour of a clean tool.
- Server-side model fallback for ambiguity: unconstitutional (reasoning role); the
  agent is the decider.
- Graph diffusion from raw recall seeds: the graph compounds a correct anchor but cannot
  choose the intended sense of a vague phrase; expansion runs only after resolution.
- Putting anchors in canonical Markdown: they are derivable from governed structure;
  making them authored state would create a second truth to maintain.
- A per-request ordinary recall over the whole vault as the only evidence source: it
  inherits the multi-second eligibility walks; the compiler's own lanes are index-backed
  and the `retrieval` kind uses a small-limit `find()` only.

## Risks / Trade-offs

- Surface churn: both digests move; mitigated by regenerating once and scheduling the
  ChatGPT refresh after release.
- Anchor coverage depends on vault structure (aliases, hubs, claims); the benchmark's
  negative twins and the `missing[]` field make gaps visible instead of silently
  widening recall.
- Latency: the tool calls a small-limit `find()`; while `accelerate-governed-recall` is
  unlanded that call can still cost seconds on the live cell. The compiler's own stages
  are bounded and measured separately so the benchmark attributes the cost.
- Vector bands need thresholds; they are pinned in `ranking_config` and covered by the
  twin fixtures, and `vector_band` alone never resolves.

## Migration Plan

Additive. First start after upgrade builds the index in the background (managed) or on
first call (unmanaged); no vault migration; the kill switch returns the pre-change
behaviour for the tool while leaving recall untouched. Rollback = unset nothing:
disabling the switch or removing the release removes the sidecar's use; the sidecar
file is disposable.

## Open Questions

- Whether `Products/` and `Systems/` should be declared as resource anchor folders by
  default or via a scaffold setting (default on for the personal profile, decided by
  the benchmark's C5 case).
- Whether the `working_set` accept/reject review family belongs to this change or to
  the host change where agent feedback naturally arrives (deferred to the host change).
