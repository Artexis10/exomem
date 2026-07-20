## Context

The acknowledgement-loss incident is already fixed on `main`: the idempotency store reserves a receipt before the leaf, persists terminal state before acknowledgement, and replays identical completed results. The remaining failures sit above that boundary. Product writers return heterogeneous raw dictionaries; `edit_memory` advertises every optional argument at once; bootstrap renders the global registry rather than the active surface; semantic audit findings are capped before action-oriented ordering; and reranking scores a fixed `3 * limit` prefix whenever it runs.

The change crosses MCP, REST, CLI, FastMCP discovery, replay persistence, audit presentation, and retrieval. The shared command leaves remain the source of behavior. Existing writer-lease, transactional write, semantic precommit, and optional-model soft-fail guarantees are constraints.

## Goals / Non-Goals

**Goals:**

- Make a successful governed mutation unmistakable in the first few response fields without losing diagnostics.
- Preserve one immutable terminal result across acknowledgement loss and identical replay.
- Keep one `edit_memory` tool while making invalid field combinations absent from its advertised primary schema.
- Make bootstrap incapable of recommending a tool missing from the invoking surface/profile.
- Make default audit output actionable beside a large grandfathered backlog, with explicit full enumeration.
- Let callers reduce reranker work deterministically without changing default ranking policy.

**Non-Goals:**

- Reopen or redesign the shipped writer lease/idempotency protocol.
- Add focused top-level edit tools.
- Infer idempotency from similar titles or content.
- Hide or delete audit truth, change precommit semantic enforcement, or auto-remediate findings.
- Promise a hard millisecond deadline around one synchronous CrossEncoder invocation.
- Add a reasoning model. The existing frozen reranker remains an optional measurement lane under the pure-substrate rule.

## Decisions

### Persist one canonical terminal before projecting response detail

`writer_lease` will convert a successfully committed product result into a versioned internal terminal record before `IdempotencyStore` stores `completed`. The record owns `ok`, `status`, `mutated`, primary path/paths, the original request/receipt identity, explicit public idempotency key when present, warning count, and the full leaf result. The default projection is compact; `response_detail="full"` adds the raw leaf result under `diagnostics`; `response_detail="legacy"` returns the raw leaf result for the compatibility window.

`response_detail` is presentation-only: adapters expose it, the invocation boundary removes it before canonical digesting and leaf execution, and changing it never reruns a mutation or causes key reuse. Replay returns the same stored terminal fields and original request identity. Attempt-relative replay disposition stays in structured logs, not the terminal payload, because an exact replay and a dynamically changing `replayed` field are mutually exclusive. Busy, pending, committed-failure, and committed-uncertain remain structured errors.

Old completed receipt rows containing a raw result remain replayable as their legacy shape until normal bounded retention removes them; the rollout will not fabricate missing terminal identity.

### Normalize edit operations before digesting

`edit_memory` gains a nested Pydantic discriminated union with `extra="forbid"` variants: `replace_body`, `replace_tags`, `replace_string`, `batch_replace`, `edit_section`, `patch_frontmatter`, and `fill_row`. This preserves all current edit behavior, including tags composed with supported body operations and the JSON-string compatibility coercion for batch items. Guard fields are exposed only on variants whose underlying leaf actually enforces them.

The runtime function temporarily retains legacy flat arguments, but the MCP/OpenAPI discovery projection advertises the discriminated primary shape. The common invocation boundary converts both forms into one canonical operation before idempotency digesting and calls the same existing `op_edit` leaf. Supplying both forms, unknown variant fields, incomplete variants, or multiple legacy modes fails before mutation with mode-specific guidance. The flat compatibility shim is marked for removal after one release.

This approach keeps one product command and one implementation leaf. It avoids seven top-level tools and prevents a semantic no-op from acquiring a different replay identity merely because an old client used the flat form.

### Bind bootstrap to an immutable active-surface descriptor

Each adapter constructs a descriptor from the exact registry tuple it registers: surface name, Tier-2 policy, and exported product command names. The descriptor is injected as invocation context, not as a caller-controlled tool argument. Bootstrap filters route catalogs, defaults, examples, advanced references, and `common_tools` against it and reports the active names. The packaged canonical MCP fingerprint remains clearly labelled as canonical; it is not presented as proof that a Tier-2-disabled or non-MCP surface exports the full canonical set.

Conformance tests walk every tool reference in each bootstrap profile and compare it with actual MCP discovery, REST/OpenAPI operations, CLI commands, and hosted REST commands.

### Keep audit truth raw and add an action-first public projection

The lower-level `AuditReport` continues to own all findings and category totals. For audit serialization, semantic posthoc findings are deterministically prioritized before their bounded default cap so current non-grandfathered blockers cannot be displaced by legacy debt. Grandfathered `RELATION_DISPOSITION_MISSING` findings project at info/backlog severity.

Default `detail="actionable"` returns current blockers first, malformed/unregistered semantic work next, normal findings after that, and a grouped grandfathered backlog with exact observed counts and bounded representative samples. `detail="full"` explicitly requests full enumeration and retains raw omission/truncation metadata. `review_memory(mode="audit")` and `maintain_memory(mode="audit")` forward the same detail controls; no read path acquires the mutation boundary.

### Bound reranker input by candidate count, not a fake timeout

`rerank_max_candidates` is optional. When present it must be at least the requested result limit and at most the existing hard candidate ceiling. The scorer receives `min(3 * limit, rerank_max_candidates)` leading candidates; any remaining fused candidates stay in stable fused order below reranked candidates before final trimming. The option is included in cache identity and timing/explanation metadata.

Default `None` preserves current behavior. Reranking remains explicit or accelerated-policy auto-selected, and ImportError/runtime failure still returns fused results. A hard latency budget is deferred until the inference backend offers a killable or cancellable boundary; returning from a thread timeout while inference continues would leak compute and make latency less predictable.

## Risks / Trade-offs

- **Compact default changes successful public payloads** → retain `full` and one-release `legacy` projections, version the terminal record, document the release boundary, and update all surface/schema fixtures intentionally.
- **FastMCP may validate against generated function metadata rather than only discovery JSON** → add black-box list-tools and call-tool tests proving new discriminated calls and undeclared legacy flat calls both reach normalization before promoting the connector contract.
- **Canonical edit normalization can accidentally change replay identity** → table-test every legacy/new equivalent pair and assert one digest/one leaf execution.
- **Audit grouping can conceal unknown totals after an upstream bound** → order before bounding, carry observed/omitted/complete fields, and never label a partial count exact.
- **A small rerank cap can reduce precision** → caller opt-in only, minimum cap equals output limit, and report requested/effective counts.
- **One PR spans independent concerns** → keep implementation in reviewable commits and run per-area focused tests before the combined suite; schema fixture and fingerprint are refreshed once at the end to avoid conflicting generated artifacts.

## Migration Plan

1. Ship the versioned terminal record plus compact/full/legacy projections; keep legacy receipt decoding.
2. Ship discriminated `edit_memory` discovery with the flat runtime shim and deprecation text. Verify a fresh ChatGPT session before promoting the new connector contract.
3. Ship surface-filtered bootstrap and conformance tests.
4. Ship action-first audit and rerank cap.
5. In the next compatibility release, measure use of `response_detail="legacy"` and flat edit calls before removing either shim through a separate OpenSpec change.

Rollback restores the previous presentation/schema layer. Stored versioned terminals remain decodable and canonical Markdown writes require no migration.

## Open Questions

- The exact future release that removes the two compatibility shims depends on observed client adoption; this change sets a one-release minimum, not a date.
