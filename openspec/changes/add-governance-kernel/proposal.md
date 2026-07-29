# Proposal: add-governance-kernel

## Why

Exomem today offers exactly two dispositions for a source: fully indexable or
`excluded` (invisible). There is nothing between "the model can see everything"
and "the model can see nothing" — no way for the owner to say *this project's
architecture may inform my thinking but its client names must never leave the
vault*. The product goal is **maximum connection under user control, with minimal
unintended disclosure**: one broadly connected vault where *internal
discoverability* and *external releasability* are separate, deterministic,
user-controlled planes.

This change lands the **kernel** — the policy representation, compiler, and pure
decision evaluator — with **no enforcement yet**. It is inspection-only: it can
load `_Governance/` policy, resolve which pages a scope covers, and answer "what
is the effective disclosure ceiling for this item, audience, and purpose?" —
but nothing in the retrieval or read path consults it until `add-release-gate`.
Landing the kernel alone keeps the risky change (touching egress) small and lets
the evaluator be proven against fixtures first.

The design obeys the repo constitution (`openspec/config.yaml`): pure substrate —
the server measures, the brain reasons. The evaluator is a pure Python function
over compiled tables; no model, no network, no confidence floats. Absent
`_Governance/`, the compiler yields an `OPEN` policy and every downstream caller
short-circuits — a governed feature that is default-off and soft-failing.

## What Changes

- New in-vault policy home `Knowledge Base/_Governance/` (strict YAML, JSON-Schema
  validated): `scopes/*.yaml` (membership selectors + excludes),
  `rules/*.yaml` (audience + purpose + disclosure ceiling L0–L6 + options),
  `grants/*.yaml` (standing exceptions). One document per file; ULID ids;
  `governance_version: 1`.
- A live-reload loader with a content fingerprint, mirroring `access.py`'s
  `policy_fingerprint` (`:78`): missing dir → cached `EMPTY_POLICY` singleton;
  present → per-file stat-signature → content hash on change. Refuses to compile
  when an Obsidian-style `(conflicted copy)` sibling is present (fail-closed).
- A compiled snapshot in a per-machine sidecar `.governance.sqlite`
  (`sidecar_store` pragmas/meta), keyed by fingerprint — for cross-process
  inspection and doctor, never as the enforcement authority.
- Query-time **membership** evaluation of selectors against the already-parsed
  `ParsedPage`, memoized per `(fingerprint, path, mtime)` — not an index-time
  table (no fifth derived component in the `index_sync` fan-out).
- A **pure decision evaluator**: `ceiling = min(org_cap, max(grants,
  min(standing_rules)))`, default OPEN (L6). Order-free lattice — conflicts are
  impossible by construction.
- `_Governance` added to `find_corpus.EXCLUDED_DIR_NAMES` so policy files never
  index as content. A generic `_scaffold/_Governance/README.md` (leak-guard-safe).

## Capabilities

### New Capabilities

- `governance-kernel`: deterministic, local-first policy representation
  (`_Governance/` YAML), a fingerprinted compiler with an empty-policy fast path
  and conflicted-copy refusal, query-time memoized scope membership, and a pure
  disclosure-ceiling evaluator over an order-free lattice.

### Modified Capabilities

(none — the kernel's internal read leaves are registry-level plumbing;
`command-surface` requirements gain new operations automatically, per the
relation-acceptance-queue precedent. No user-facing tool ships in this change.)

## Impact

- Code: new package `src/exomem/governance/` — `policy.py` (load + fingerprint +
  EMPTY_POLICY + conflicted-copy refusal), `compile.py` (snapshot into
  `.governance.sqlite`), `membership.py` (selector evaluation + memo),
  `decisions.py` (pure lattice + `Decision` dataclass), `store.py` (sidecar
  tables skeleton), `__init__.py` (facade). Edits: `src/exomem/index_paths.py`
  (governance sidecar path), `src/exomem/find_corpus.py` (one
  `EXCLUDED_DIR_NAMES` entry), `src/exomem/_scaffold/_Governance/README.md`.
- Tests: `tests/test_governance_policy.py`, `tests/test_governance_store.py`,
  `tests/test_governance_membership.py`, `tests/test_governance_decisions.py`,
  `tests/test_governance_overhead.py` (empty-policy short-circuit); scaffold
  no-leak (`tests/test_scaffold_no_leak.py`) must stay green.
- No new runtime dependencies (stdlib + existing `pyyaml`); all governance tests
  run in the lean lane (`EXOMEM_DISABLE_EMBEDDINGS=1`) — the evaluator needs no
  models.
- Explicitly NOT in scope: any egress enforcement, notices, redaction, the
  `govern_memory` tool, receipts, bridges. Those are separate changes.
