## Context

The serving host's call ledger (`~/.local/share/exomem/service/logs/ledger.jsonl` and `personal-service/logs/ledger.jsonl`) is the connector's server-side truth: the Cloudflare tunnel on this host fronts `exomem.substratesystems.io`, and ChatGPT rows arrive as `openai-mcp/1.0.0`. Read on 2026-09-10, it shows:

- Six `preserve_artifacts` calls ended `MUTATION_COMMITTED_ACKNOWLEDGEMENT_UNCERTAIN` (12 to 39 s each). Every one carries post-commit spans `index.upsert_after_write` (1 to 15 s) and, from 2026-09-09, `graph.refresh_paths` and `derived.canonical_commit`. The canonical bytes committed; the derived acknowledgement did not.
- Two `MUTATION_OUTCOME_UNKNOWN` rows follow an acknowledgement-uncertain row by about ten seconds with identical `request_bytes`: the client retried the same identity and got the fail-closed terminal.
- `preserve_artifacts` durations run 14 to 95 s all week; two 2026-09-08 calls the conversation reported as failed logged `ok` after 30.4 s and 21.9 s.
- The 2026-09-09 19:22Z and 21:45Z rows have argument shapes `scope` 36 bytes and `category` 35 bytes, which equal `canonical_json({"v": "food/caviarhouse-group-order"})` and `canonical_json({"v": "roe-tasting-and-butter-mayo"})`; the server wrote `Evidence/foodcaviarhouse-group-order/...`.
- Every `preserve_artifacts` row has `target_paths: []`, because the ledger derives targets from path-shaped arguments only.

Code facts behind them (tree at origin/main 86054c0a):

- `writer_lease.py`: `_acknowledge_derived_batches_timed` (from L512) wraps the post-commit derived acknowledgement; any exception there raises `_PostCommitOutcomeUncertain` (L664, "fast acknowledgement failed; returning committed-uncertain"). In the execution path (L2317 to L2336) `after_operation_guard(result)` runs before `_persist_completed_from_canonical`, so a derived failure prevents the terminal from ever being persisted. On the same-identity retry (L2207 to L2222) `resume_canonically_committed` returns `_OUTCOME_UNKNOWN_TERMINAL` when the canonical row has no retained terminal and no graph commit receipt (L4060 to L4069), which is then persisted as the stable fail-closed answer.
- `client_artifacts.py:preserve_artifacts` (L1327) stages every handle, commits each under `manager.mutation_guard(... operation="preserve_artifacts_commit")`, calls `mark_active_mutation_committed()`, then reconciles media under a second guard; per-file outcomes are `stored` or `failed`. `_existing_receipt` (L795) is consulted only by the `adoption` lane (L954, L1002, L1077, L1125).
- `preserve.py:_sanitize_segment` (L695) applies `re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", s)`; `client_artifacts._validate_destination` (L313) accepts any value the sanitizer leaves non-empty; `_destination(vault_root, "Evidence", _sanitize_segment(scope), _sanitize_segment(category))` (L1072) joins the mangled value.
- `server_hosted.py:_HOSTED_MUTATION_ERROR_SHAPES` (L89 to L95) lists `MUTATION_ACKNOWLEDGEMENT_PENDING` and `MUTATION_COMMITTED_ACKNOWLEDGEMENT_UNCERTAIN` but not `MUTATION_OUTCOME_UNKNOWN`, so `_hosted_mutation_error_details` (L429) returns `{}` for it.
- `call_ledger.py:_target_paths` (L160) reads path-shaped arguments; outcomes never contribute.

## Goals / Non-Goals

Goals: a committed artifact batch always has a persisted terminal that a same-identity retry can replay; a duplicate under a new identity is reported rather than stored twice; a malformed destination is refused before any byte moves; the ChatGPT client receives structured fields for every mutation error code it can see; the ledger names what an artifact write landed.

Non-goals: request latency and deferral of media or index work (next change); the connector's stale action schema (operator step); attachment-handle bridging; promotion or hygiene sensors.

## Decisions

### D1 Terminal persistence precedes derived acknowledgement

`writer_lease` persists the completed terminal from the canonical result before running the derived acknowledgement. The acknowledgement then runs in a bounded step whose failure or timeout is projected into the already-persisted terminal as `derived_sync: "pending"` plus `derived_sync_components` and a warning naming the derived component (never a path, never an exception class), and `_PostCommitOutcomeUncertain` is raised only when persisting the terminal itself fails. This keeps the contract's existing sentence ("committed-uncertain when exact terminal persistence cannot be proven") true and removes the case the ledger shows: commit proven, acknowledgement lost, terminal absent.

Invariant this must keep: a `completed` row never carries unfinished operation-completion work. The existing `after_operation_guard` bundles two different things: the derived acknowledgement (index and graph fan-out proof, vocabulary delivery) and, for commands whose terminal *is* the derived state, the unbounded graph join that finalizes a `_graph_rebuild_handoff`. If that finalization ran after persistence, a crash in the window would leave a `completed` row holding an unfinalized handoff and the replay would report the rebuild as cleared while the graph stayed quarantined. So the hook is split at the composition site by command class. `reconcile`, `maintain_memory(mode="reconcile")` and any result carrying a graph-rebuild handoff keep the whole guard before terminal persistence exactly as before; a crash there leaves `canonically_committed`, which `resume_graph_sync` finalizes on replay. Every other command runs the guard after persistence as the derived acknowledgement, in its existing internal order (acknowledge, bounded graph outcome, vocabulary delivery). `IdempotencyStore.run` therefore keeps `after_operation_guard` in its pre-persistence position and gains a second, post-persistence hook for the acknowledgement. One shared predicate decides which of the two runs a command's guard. The handoff clause is a property of the result rather than of the command, so the predicate cannot be read at bind time: both hooks are bound, each consults the predicate when it is reached, and the pre-persistence hook records that it ran so a guard that already finalized (and thereby stripped) the handoff that selected it is not run a second time. The two crash-window tests in `tests/test_graph_rebuild_availability.py` are the executable form of this invariant and stay unchanged.

The compact response projection carries the acknowledgement outcome through the envelope fields that already survive it (`derived_sync`, `derived_sync_components`), not through free-text warnings, which compact deliberately drops for artifact receipts.

Alternative rejected: making the derived acknowledgement infallible. Index and graph fan-out are the slowest components on the host and legitimately fail under contention; the terminal must not depend on them. Also rejected: moving the whole guard after persistence and rewriting the two crash-window tests. That trades a real replay defect for the one this change repairs.

Control accounting: what it prevents is an ambiguous terminal after a real commit; what it costs when wrong is a success terminal that says `derived_sync: pending` for work that in fact completed, which the next reconcile corrects; the payer is the reader of one stale field, not the writer.

Known residue: the delivery reserve bounds waiting, not the whole acknowledgement. It survives only as a deadline folded into the component wait, deliberately, so that the cheap steps the acknowledgement also performs (proving the commit, publishing pending visibility, signalling components) always run rather than being skipped by a budget already spent on a slow leaf. Publishing pending visibility takes the consistency guard, which can block for an unbounded time, so a call can still exceed the reserve. Bounding that guard is a different change.

### D2 Same-identity replay resolves the persisted batch terminal

With D1 the canonical row always retains its terminal, so the existing `canonical_resume` path returns it and `_OUTCOME_UNKNOWN_TERMINAL` is reached only for rows written before this change. No new receipt store is introduced. A fault-injection test interrupts delivery after persistence and asserts the retry replays the batch result with one canonical write per file.

### D3 Per-file terminal states

`preserve_artifacts` and `preserve_evidence` return `request_id`, `receipt_id`, and per file `state` in `{stored, already_stored, failed}` together with `path`, `ref`, `hash`, `hash_algorithm`, `size`, `media_id`, `content_type`, `warnings`. `outcome` stays for compatibility one release and mirrors `state` (`stored`/`failed`; `already_stored` reports `outcome: "stored"` with `duplicate_of` naming the existing path). The batch summary counts all three states. Presentation stays outside the mutation payload digest, as the contract requires.

### D4 Content-hash idempotency on the plain path

After staging, before commit, the writer looks up the destination directory's sidecars for a matching `hash` (sha256 of the staged bytes). A match yields `already_stored` with the existing `path` and `ref`; nothing is written and the audit chain does not advance. The lookup is bounded to the destination directory (scope and category), so an identical artifact under a different category is still stored: the same bytes in two evidence families are two facts. The `adoption` lane keeps its receipt-based replay unchanged.

Alternative rejected: a global content-address index. It would make cross-family duplicates disappear silently and adds a store; the destination-scoped lookup answers the observed failure (retry into the same family).

Second known residue: a resolved-duplicate terminal inherits the graph-sync fields of the commit boundary it passed through, so an all-duplicate call can report `graph_sync: "pending"` with remediation prose about "the changed pages" while having changed none. The fields describe the vault's queued graph work truthfully; only their attribution to this call is wrong, and for a mixed batch — one that stored at least one file — they are correct outright. Suppressing them would mean threading a new "this call changed no canonical path" signal from the leaf through the composition site into the graph wait, whose `registered_checkpoint` is vault-wide and carries no per-call attribution to lean on. That is a restructure of the graph path, not a fix to this one, and it is left.

Known residue: the lookup runs after staging and before the per-file mutation boundary, so two concurrent preserves of identical bytes into one destination can both miss it and both store — which is exactly what happens today without the lookup, so the dedup narrows the window rather than opening one. Closing it would mean either re-reading the destination inside every per-file boundary, which restores the O(files x sidecars) scan the index exists to remove, or holding one boundary across the whole batch, which the narrow-boundary design exists to prevent. A destination-scoped lock is a different change.

### D5 Destination segments are validated, never normalised

`_validate_destination` refuses a `scope` or `category` that contains a path separator, a reserved character, leading or trailing whitespace, or that sanitises to a different string, with `INVALID_PRESERVE` whose `details` carry `field` (`scope` or `category`), `reason`, and the accepted form ("one path segment; nest with category, not with '/'"). `_sanitize_segment` remains for filenames and legacy callers but no longer runs on destination segments in the preservation commands. The refusal happens before staging, so zero bytes are fetched for a malformed destination.

Control accounting: prevents silent path forks; a wrong refusal costs the caller one corrected call with the field named; no human is in the loop.

### D6 Hosted shape and ledger targets

`_HOSTED_MUTATION_ERROR_SHAPES` gains `"MUTATION_OUTCOME_UNKNOWN": ("uncertain", None)`. The command wrappers for `preserve_artifacts`, `preserve_evidence` and `capture_source` hand the ledger the committed relative vault paths as structural targets; the ledger's "never values" rule is unchanged because a vault-relative path is already what `target_paths` carries for note writes.

## Risks / Trade-offs

- D1 changes ordering inside the writer lease, the most safety-critical module. Mitigation: red-first fault-injection tests for both orders (acknowledgement failure and persistence failure), the existing `test_writer_lease*` and `test_mutation_terminal*` suites, and the derived-batch acknowledgement tests; the reviewer lane attacks the crash window between persistence and acknowledgement.
- D3 changes the tool response schema: regenerate the tool-surface contract, MCP schema fixtures, plugin tree, hosted renders, the v5 candidate and `docs/capabilities.md` in the same change (the last delivery lost two rounds to these).
- D4 reads sidecars in the destination directory on every commit; bounded by directory size and cheaper than the staging download it follows.
- Local test runs need `XDG_STATE_HOME` pointed at scratch and the CI shard layout; the default 60 s per-test timeout with `timeout_method = thread` kills a serial run on the first slow test.

## Migration

No data migration. Canonical rows persisted before this change keep their behaviour; new commits carry retained terminals. The split `foodcaviarhouse-group-order` Evidence tree is operator repair after deploy, through governed tooling, recorded for closure only.

## Open Questions

- Whether `already_stored` should also fire when the hash matches under a different category within the same scope. Start no (D4); revisit with usage evidence.
- Whether the derived acknowledgement should be bounded by a fixed budget (for example 5 s) or by the remaining request budget from the write-path resilience contract (60 s single-origin). Start with the remaining request budget minus a fixed reserve for terminal delivery; the latency change will revisit.
