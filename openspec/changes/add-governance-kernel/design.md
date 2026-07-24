# Design: add-governance-kernel

## Context

The kernel reuses proven Exomem patterns rather than inventing machinery.
Fingerprinted live reload: `access._load_config`/`policy_fingerprint`
(`access.py:59-135`) — content-hash based so a timestamp-preserving edit still
invalidates. Sidecars: `sidecar_store.apply_sidecar_pragmas` + `ensure_meta_table`
+ `SystemRandom` seeding (`:56-63`), path via `index_paths` convention
(`vault_root/<kb>/.governance.sqlite`). Selector inputs already exist as
frontmatter-derived fields on `ParsedPage` and as taxonomy sources
(`project_keys`, tags, `entity_types`, `semantic_language_registry`); the
selector compiler can reuse `structured_filters` idioms. Nothing here runs a
model — consistent with `openspec/config.yaml` "pure substrate."

## Goals / Non-Goals

Goals: a canonical, human-readable, user-owned policy source; a deterministic
compiler with an empty-policy fast path; query-time scope membership that serves
find *and* the non-find read surfaces; a pure, testable ceiling evaluator; zero
enforcement (inspection only) this change.

Non-Goals: egress filtering, notices, redaction, tokens, receipts, bridges, the
`govern_memory` tool (all later changes). No index-time membership table. No
policy engine dependency (Cedar/OPA/Casbin — see the plan's §8 rejection).

## Decisions

### D1 — Canonical policy = strict YAML in the vault, compiled to a sidecar
Source of truth is `_Governance/**.yaml` (syncable, diffable, travels with the
vault). The `.governance.sqlite` snapshot is a derived convenience for
cross-process inspection/doctor, rebuildable from the YAML and never the
enforcement authority. This keeps "everything is a user-owned file" true for
policy itself and lets backup/restore/adoption inherit governance for free.
Rejected engines (Cedar/OPA/Oso/pycasbin/SpiceDB) per the plan: none is a mature
in-process pure-Python fit that also expresses representation ceilings.

### D2 — Fingerprint + empty fast path
`policy.load(vault_root)` mirrors `access._load_config`: stat `_Governance/`;
missing → process-cached `EMPTY_POLICY` singleton, fingerprint `"missing"`;
present → per-file stat-signature tuple over the bounded YAML set, content hash
on signature change. Every downstream hook's first line is
`pol = policy.load(root); if pol.empty: return <no-op>`. One dir-stat per request
— the same cost class find already pays for `_access.yaml`.

### D3 — Conflicted-copy refusal
Because `_Governance/` sits in a synced vault, Obsidian can drop
`rules (conflicted copy).yaml`. The compiler refuses to compile when a
`(conflicted copy)` sibling is present, surfacing it for resolution rather than
nondeterministically merging or silently ignoring the user's edit. Fail-closed:
a refused compile keeps the last good snapshot and reports the conflict.

### D4 — Membership at query time, memoized — not an index-time table
Selectors read frontmatter the pipeline already holds. An index-time membership
table would become a fifth derived component in the `index_sync` fan-out with its
own drift class for `audit` to police, and would force a full-vault recompute on
every policy change. Instead, `membership.evaluate(page, policy)` runs against the
in-hand `ParsedPage`, memoized per `(fingerprint, path, mtime_ns)` with a bounded
LRU. Policy change = O(1) memo invalidation by fingerprint. Non-find surfaces
(get/overview/graph/read_media) already hold or cheaply parse the page.

### D5 — Pure lattice evaluator, order-free
`decide(item, audience, purpose, grants) -> Decision` computes
`min(org_cap, max(exceptions_and_grants, min(standing_rules)))`, default OPEN
(L6). All matching rules participate; min/max are commutative, so there is no
rule ordering, no priority integers, no specificity algebra — conflicts are
impossible by construction and `explain` can render the participating chain.
Purpose-absence is deterministic: a purpose-conditioned *allow* does not fire
when purpose is undeclared; an "outside purpose P" *restriction* does. Purpose is
model-declared, therefore advisory (opt-in per rule), never in a cache key. The
function is pure (no IO beyond the passed-in compiled policy + grants) → property
tests.

### D6 — Sidecar holds dynamic state, not membership
`.governance.sqlite` (this change: `compiled_policy` snapshot + meta only; later
changes add `session_grants`/`withhold_tokens`/`proposals`/`receipts_head`). It
is per-machine, never synced, rebuildable. Membership memo is in-process, not
persisted.

## Risks / Trade-offs

- **Directory fingerprint heavier than a single file**: bounded to the policy
  YAML set, per-file stat tuples, content hash only on signature change — same
  guarantee as `access.py:78`. Pinned by a timestamp-preserving-replace test.
- **Selector expressiveness creep**: schema v1 frozen small (paths glob, projects,
  tags, types, classes, explicit refs, plus `exclude`); new expressiveness needs
  a new change.
- **Membership memo staleness**: keyed on `mtime_ns` + fingerprint, so an
  out-of-band edit or a policy change invalidates the entry.

## Migration Plan

Absent `_Governance/` → `EMPTY_POLICY`, zero behavior change anywhere. Existing,
adopted, exported, restored vaults need no migration. `governance_version: 1`;
unknown future fields are a compile error (fail-closed), unknown *files* in
`_Governance/` are ignored with a warning (forward-compat).

## Open Questions

None blocking. The `class:` selector's detector set (secret/pii) is defined where
first used (`add-release-gate` ships the secret detector for the default-on
credential block).
