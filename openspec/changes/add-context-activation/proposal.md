# Proposal: add-context-activation

## Why

A fresh agent with access to a rich vault still has to decide what to search for and
formulate the right recall query before Exomem can help it; on a vague ordinary turn
("I'm burning through AI usage again", "I'm planning to cook this") raw hybrid recall
surfaces the relevant page among transcripts and evidence images and never assembles
the cross-cutting facts a decision needs (resources, preferences, constraints, current
state, active plans), while Records and Planning items are invisible to recall by
design. Reproduced on the personal cell on 2026-09-16. The referents stage already
proved the shape of the fix for one anchor kind (people); this change generalises it
into a deterministic, read-only context compiler that accepts the raw turn and returns
a bounded, provenance-bearing working-memory packet, so "formulate the right query" is
no longer a required reasoning step for the primary agent.

## What Changes

- A new read-only Tier-1 operation `activate_context(turn, max_chars, purpose,
  include_timings)` exposed identically on MCP, CLI (`exomem activate`) and REST
  (`/api/activate_context`) over one leaf function, returning a working-memory packet
  rather than ranked hits.
- A new derived, disposable **activation index** sidecar (`.working-set.sqlite`) under
  the machine-local state root, built incrementally from governed structure only
  (Entities and their aliases, Hubs, Products, Systems, Planning items, Records
  collection manifests and their `claims`, project keys), mirroring the embedding
  sidecar's generation-token and copy-on-write conventions. It is never authoritative
  and never enters ordinary recall lanes.
- Deterministic **anchor resolution** with categorical evidence kinds (`exact_alias`,
  `lexical_overlap`, `vector_band`, `category_match`, `claims_match`, `retrieval`,
  `graph_corroboration`, `usage_prior`) and the referents statuses `resolved`,
  `partial`, `ambiguous`, `unresolved`; unresolved turns abstain with an empty packet.
- A versioned **context-role registry** (`context-roles.yaml`, shipped in the skill
  scaffold and the Claude Code plugin) with anchor-kind defaults and a deterministic
  turn-cue table; roles are retrieval lenses, extensible only through review.
- Bounded per-role retrieval lanes over existing primitives (semantic units with
  category pushdown, Records collection queries, Planning queries, entity/profile
  facets, `epistemic_graph.graph_context` under a traversal profile, evidence pointers,
  the ordinary fused hits as one evidence source), a small current-state resolver for
  mutable resources, evidence-aware dedup with lifecycle and supersession marks, a
  character budget with a hard ceiling, and an egress guard that never names withheld
  pages.
- Instrumentation (`working_set.*` timing spans, a CI latency ceiling and a 2k→8k
  scaling bound), kill switch `EXOMEM_DISABLE_WORKING_SET`, regenerated tool-surface
  fixtures and hosted plugin trees, README/capabilities/SKILL updates.
- Non-goals for this change: hook injection and the hookless carrier line
  (`activate-context-on-host-turns`), continuity tokens and the `anchor=` override
  (same follow-up), the hot profile (`add-hot-profile`), any usage-prior tuning beyond
  tie-breaking, any background consolidation, and any server-side model beyond the
  scorers recall already runs.

## Capabilities

### New Capabilities
- `context-activation`: the read-only activation operation, the derived activation
  index, anchor evidence and resolution with abstention, bounded role lanes, the
  working-memory packet and its budget, governance/egress, freshness, instrumentation,
  the kill switch and surface parity.
- `context-roles`: the versioned context-role registry, deterministic role selection
  from anchor kinds and turn cues, and its review-gated evolution rules.

### Modified Capabilities
<!-- none: ask_memory, find ordering, bootstrap and the hooks are unchanged by this change -->

## Impact

- `src/exomem/commands.py` (new `op_activate_context` + registry row), `__main__.py`
  (subcommand), `server_rest.py` (route), `governance/egress.py` (`guard_working_set`).
- New modules `working_set_index.py`, `working_set_resolve.py`, `working_set.py`,
  `working_set_state.py`, `working_set_runtime.py`, `context_roles.py`; new scaffold
  and plugin file `context-roles.yaml`.
- One new sidecar under `state_paths.vault_state_dir()`; no vault writes.
- Both tool-surface digests move; `tests/fixtures/mcp_tool_schemas.json`, the hosted
  plugin/candidate trees, `docs/capabilities.md` and the README tool table are
  regenerated; the ChatGPT connector adopts the tool after the next release and a live
  conversation; marketplace listing material is deferred.
- Acceptance is measured by the companion change `add-context-activation-benchmark`
  (deterministic thresholds on a seeded corpus; agent arms on the personal cell).
