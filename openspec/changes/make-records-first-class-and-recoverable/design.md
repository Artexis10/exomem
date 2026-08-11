## Context

The first-class Records substrate and its public `record_memory` command already support human-owned Markdown logs, Markdown items, query-only datasets, stable identity, guarded append/update, audit inspection, and the recently added `describe`/`validate` authoring workflow. The incident that motivated this change exposed three gaps above that substrate:

1. Agent discovery is passive. Runtime tool metadata is tautological, `record` is absent from beginner/front-door action catalogs, and compact bootstrap places Records after a large semantic-authoring projection.
2. Manifest acceptance is inconsistent. Create preflight freezes saved views without fully resolving them, while inspect performs stricter validation. Existing manifests have no Records-native audited revision or rebaseline path.
3. Release evidence is shallow. The installed-wheel Records loop starts from a fixture written directly to disk, and connector promotion records tool visibility rather than a deployed end-to-end lifecycle.

Records remain a pure substrate. Deterministic parsing, validation, routing metadata, audit transitions, and acceptance assertions belong in Exomem; semantic inference about whether conversational content is an observed event remains an agent responsibility taught through the product contract. No server-side reasoning model is introduced.

The reported long-held mutation boundary is addressed separately by `rebuild-graph-without-blocking-writes`. This change requires that availability result before final live acceptance but does not duplicate the graph implementation.

## Goals / Non-Goals

**Goals:**

- Make agents infer the Records route whenever conversational context contains durable observed state, even without explicit “save”, “log”, or “Records” wording.
- Give agents a salient, compact, action-specific public contract while retaining one multiplexed Records tool.
- Make validation, creation, inspection, revision, and saved-view execution share one acceptance path.
- Provide guarded audited manifest revision and explicit recovery from valid out-of-band edits without erasing historical discontinuity.
- Prove authoring, mutation, recovery, restart, concurrency, and agent selection through real MCP transports and a disposable deployed vault.
- Prevent connector promotion from treating schema visibility as feature acceptance.

**Non-Goals:**

- Add automatic server-side reasoning, medical interpretation, or a transcript scraper.
- Silently create a new long-lived collection when no compatible collection exists.
- Replace human-owned manifests with an object database or second canonical authoring representation.
- Migrate storage strategies, make datasets writable, or repair structurally invalid item data through rebaseline.
- Add force-unlock for a live mutation owner or duplicate graph-rebuild availability work.

## Decisions

### Make Records an inferred agent route, not a keyword feature

Add `record` to both beginner and product-front-door vocabularies. The MCP-visible tool description, `action` description, bootstrap workflow, common-tool guidance, installed scaffold, and relevant generated capability text will all state the same semantic boundary: Records hold observed events and state; Planning holds future intent; Sources preserve received raw material; Evidence preserves proof; Notes hold conclusions.

Capability filtering remains honest: every active profile that advertises Records must export `record_memory`, including the disposable hosted-cell acceptance surface. A profile that intentionally excludes the command reports Records unavailable instead of teaching a dead route.

An agent may proactively append when an observed event is durable, attributable, sufficiently shaped for exactly one existing collection, and allowed by the active Exomem engagement policy. It reports the write afterward. Multiple plausible collections or missing identity/date/provenance produces one focused question. No compatible collection produces a concise collection proposal; creation remains an explicit audited mutation rather than silent schema invention.

The deterministic server does not classify arbitrary conversation text. Selection behavior is enforced through static contract tests plus live Codex and Claude Code acceptance prompts. This preserves the pure-substrate boundary while testing the product where inference actually occurs.

Alternative considered: add a server-side `classify_record_intent` model. Rejected because it duplicates agent reasoning, adds a hidden model dependency, and cannot guarantee client tool selection.

### Keep one tool and make its finite actions discriminated

`record_memory` remains the only Records command. Its finite selector expands to nine actions: read-only `describe`, `validate`, `inspect`, and `query`; mutating `create`, `append`, `update`, `revise`, and `rebaseline`.

Each action keeps an explicit allowlist and required-field set. Public parameter descriptions state which actions accept each field. `validate` has two unambiguous forms:

- create preflight: `manifest_path + manifest_text`;
- revision preflight: `collection + manifest_text`.

Supplying both selectors or neither is refused. Revision validation returns current manifest/container guards and a normalized proposed contract but writes nothing.

Alternative considered: split nine actions into separate MCP tools. Rejected because Records already has one canonical product front door, storage strategies are intentionally hidden behind it, and connector schema churn would increase.

### Validate the complete manifest contract eagerly

Manifest parsing/validation will normalize and resolve every saved view against the declared schema, including defaulting absent filters to an empty list. The same helper is used by `validate`, guarded `create`, normal load, inspect, revision, and saved-view query. A manifest accepted by preflight therefore cannot fail immediate inspect solely because another path validates more strictly.

Diagnostics are deduplicated by stable code/location so one invalid view produces one actionable finding.

Alternative considered: patch only inspect's tuple default. Rejected because it would fix the reproduced symptom while retaining the split acceptance contract.

### Add guarded audited `revise`

`revise` accepts a collection selector, complete proposed manifest text, exact current manifest and container hashes, and a rationale. Under the existing writer boundary it authorizes and re-reads the current manifest, validates the proposed contract and all current canonical items, rechecks the guards, and publishes the manifest plus one Records audit transition atomically.

Collection identity, semantic profile, canonical source, and storage strategy are immutable through `revise`; representation migration remains separate. Schema changes are allowed only when every current item validates without coercion. The transition records before/after manifest and container hashes without copying record values.

To repair a manifest whose optional contract detail is invalid, the governed selector resolves and authorizes the current manifest path before its bytes are opened or parsed. Missing and withheld collections produce the same projected result. The operation then authorizes the declared source, every current canonical item, and every post-revision referenced path before validation or publication. A mixed-release collection refuses without exposing hidden paths, values, hashes, counts, or gap diagnostics. Revision requires a continuous current audit chain and may tolerate only the bounded optional manifest defect being replaced; direct-edit hash gaps use `rebaseline` instead. Ambiguous identity, audit forks, invalid canonical items, or unauthorized artifacts still refuse. All lifecycle errors and receipts pass the ordinary L6 projection before return.

The existing Records audit event and receipt version 1 remain closed and unchanged for create/append/update. `revise` and `rebaseline` use lifecycle event/receipt version 2. Both v2 shapes are closed and independently validated. Their common event fields are the v1 correlation/path/hash fields plus `continuity`, `acknowledged_gap_codes`, `gap_fingerprint`, `checkpoint_snapshot_hash`, and `minimum_reader_version`. Their closed receipt fields are `_record_receipt`, `receipt_version`, `operation`, `collection_id`, null `item_key`/item hashes, before/after manifest and container hashes, `affected_paths`, domain-separated `payload_hash`, `outcome`, `audit_correlation`, the four discontinuity/reader fields, and no arbitrary nested payload.

For `revise`, continuity is true, codes are empty, discontinuity fingerprints are null, and the manifest is the only affected canonical path. For `rebaseline`, continuity is false, codes are non-empty sorted exact strings, and both fingerprints are required. A stored v2 receipt always has `outcome: committed`. Exact idempotent request replay returns that byte-identical stored receipt and never appends another event; replay is represented only by the enclosing mutation terminal (`status: replayed`, `mutated: false`), not by rewriting the receipt.

Alternative considered: edit `_collection.md` through the generic editor and rebuild audit afterward. Rejected because that reproduces the current unaudited gap and separates one logical mutation into unsafe steps.

### Add explicit `rebaseline` without rewriting human data

`rebaseline` handles a collection that is currently structurally valid but whose audit hashes differ because a human or generic editor changed canonical files. It accepts the exact current manifest/container hashes, the exact inspect-reported gap codes being acknowledged, and a rationale. It writes no item content. It appends a content-free checkpoint transition and updates the manifest audit head atomically.

The checkpoint does not make provenance continuous again. Audit status becomes the stable value `acknowledged_gap`, never `ok`, while the current canonical snapshot is reported structurally valid and trusted only from that checkpoint forward. Inspection, query history, and agent history permanently expose `provenance_continuity: false` and a bounded warning.

The `rebaseline` event uses Records lifecycle event version 2 and records `operation: rebaseline`, the prior head, `continuity: false`, sorted exact acknowledged gap codes, a `gap_fingerprint` over canonical JSON containing the prior head, gap codes, and guarded before-manifest/container hashes, and a `checkpoint_snapshot_hash` over canonical JSON containing the sorted authorized paths and hashes of the pre-checkpoint manifest/items. It also carries before/after manifest and container hashes and the sanitized rationale, but no item values. The transition ID is generated independently; neither fingerprint input contains event bytes, transition ID, after-manifest hash, receipt, or terminal, so no recursive hash exists. Later valid mutations extend the new head without deleting or relabelling the acknowledgement. Rebaseline refuses schema violations, duplicate identities, ambiguous selectors, malformed/forked history, mismatched acknowledgement, unauthorized artifacts, or drift after preflight. It never invents missing item history.

Canonical JSON sorts object keys, uses no whitespace, uses `ensure_ascii=false`, and is encoded as UTF-8. Lifecycle request hashes use prefix `exomem-record-lifecycle-request:v2\0`; gap fingerprints use `exomem-record-gap:v2\0`; snapshot fingerprints use `exomem-record-checkpoint:v2\0`. Fixed vectors in the delta spec pin all three algorithms.

Alternative considered: automatic reconcile repair. Rejected because generic reconcile owns derived indexes, not canonical Records history, and silently closing a gap would make false provenance.

### Use two release gates and a disposable acceptance vault

Every Records-affecting pull request runs a deterministic installed-wheel test in a temporary vault. Unlike the current fixture-first loop, the client must call `describe`, validate and create a manifest containing a saved view without filters, inspect it, append/query/update, revise it, create a direct-edit gap, rebaseline it, restart, and prove parity. A concurrent full graph rebuild test proves an unrelated Records mutation is not blocked once the coordinated graph change lands.

After deployment, a dedicated disposable HTTP/OAuth vault repeats the lifecycle. Codex and Claude Code each receive prompts that contain observed state but never say “save”, “log”, “record”, or “Records”; a compatible existing collection must be selected and mutated proactively. A no-collection prompt must produce a collection proposal rather than a wrong-layer write or silent schema creation.

The live runner emits content-free unsigned facts. The existing hosted promotion verifier accepts them only inside its operator-signed, 24-hour evidence envelope. The signed contract binds the deployment SHA, release and package identities, canonical MCP surface digest, run nonce, disposable-vault purpose and reset epoch, principal and audience HMACs, exact client/model/system-contract versions, fixed prompt-case hashes, action coverage, per-mutation request/receipt IDs and committed terminal, restart result, and independently re-read before/after state hashes. Extra fields, stale evidence, reset reuse, identity mismatch, unverifiable readback, or an incomplete case/action set fail closed. Exact replay of byte-identical signed evidence against its unchanged candidate is an idempotent no-op returning the same promotion result; it never creates a second acceptance. `src/exomem/hosted_plugins.py`, `plugins/hosted/promotion/*.json`, `deploy/chatgpt/personal-plugin-contract.json`, and their guardrail tests are the enforcing artifacts. CI does not pretend it can prove a not-yet-deployed endpoint: local installed-wheel proof gates the code PR; signed live evidence gates the later promotion PR.

Natural-language selection remains a blocking product acceptance result, not a claim about deterministic server classification. Pinning the exact client, model, and system-contract versions plus prompt-case hashes makes the result reproducible enough to identify what was tested; the deterministic lifecycle remains independently proven by transport tests.

For graph availability, this change consumes the exact `graph-rebuild-availability` checkpoint/terminal contract. A write publishes its canonical batch and versioned graph-sync checkpoint under the boundary, releases the boundary, and may then await the per-vault single-flight rebuild before returning. A second writer can enter and publish the next checkpoint while both responses wait on the same rebuild result. Crash recovery, checkpoint acknowledgement, committed derived-failure terminals, and second-batch admission are owned by `rebuild-graph-without-blocking-writes`, not redefined here.

Alternative considered: retain a free-form verification string. Rejected because PR #438 demonstrated that prose can claim a verified surface while omitting the broken lifecycle.

## Risks / Trade-offs

- [Proactive capture surprises users] → Follow the existing engagement policy, require an unambiguous existing collection and sufficiently shaped observed event, report every inferred mutation, and propose rather than silently create schemas.
- [Revision accidentally becomes representation migration] → Freeze collection identity, profile, source, and strategy; validate all existing items; keep migration out of scope.
- [Rebaseline hides history] → Use persistent `acknowledged_gap` rather than `ok`, preserve the prior head and acknowledged gaps as a permanent discontinuity returned by inspection/query/history, and refuse structural corruption.
- [A large compact bootstrap remains easy to truncate] → Put front-door actions and Records before semantic-authoring detail, enforce both total size and maximum byte position, and keep full schema under `describe` only.
- [Live agent acceptance is nondeterministic] → Use fixed prompt hashes, exact client/model/system-contract versions, exact outcome assertions, a disposable vault reset per run, independently re-read state, and trusted signed evidence; deterministic transport tests remain the code gate.
- [Connector promotion cannot run before deployment] → Separate code-merge and post-deploy promotion gates explicitly rather than fabricating pre-deploy evidence.
- [Action expansion worsens optional-field overload] → Keep action allowlists/required fields executable, expose action-specific descriptions, and add schema-fidelity tests for every accepted/refused combination.
- [Hosted capability filters drift from the canonical surface] → Assert bootstrap/tool-list parity per active profile and run the disposable acceptance through the hosted HTTP/OAuth profile.

## Migration Plan

1. Land the Records product/lifecycle change with regenerated surfaces and pending connector digest; do not promote it yet.
2. Land `rebuild-graph-without-blocking-writes` and verify the unrelated-mutation concurrency contract.
3. Deploy the combined release to the public service without changing existing collection bytes.
4. Reset or provision the dedicated disposable acceptance vault and run deterministic HTTP/OAuth plus Codex/Claude acceptance.
5. Open a new promotion PR carrying structured current evidence and supersede PR #438.
6. Existing collections require no migration. Collections with valid direct-edit audit gaps may opt into `rebaseline`; invalid optional manifest detail uses guarded `revise`.

The release establishes Records reader contract version 2 before enabling either lifecycle selector. The first v2 transition atomically upgrades the manifest's existing `record_audit.version` from 1 to 2, which is the durable per-collection reader marker. Every later append/update preserves that marker even though its already-closed event remains version 1, so a valid history may be `v1 -> v2 -> v1`; later lifecycle events remain v2. The new reader dispatches each event by its own version, scans the whole reachable chain, and never loses an earlier acknowledged discontinuity. The immediately preceding reader fails closed on the v2 manifest and never rewrites it. Hosted package/deployment locks record `minimum_records_reader_version: 2`, and readiness/rollback tooling refuses a runtime below that floor. Supported rollback is therefore a compatibility build retaining the v2 parser/status semantics while disabling `revise` and `rebaseline`; it is never the preceding binary. Upgrade, first-write, append/update, restart, downgrade-refusal, and rollback-build fixtures pin that behavior.

## Open Questions

None. The user approved inferred proactive routing, proposal-before-first-schema, explicit audited revision/rebaseline, a disposable live cell, and separate coordinated graph availability work.
