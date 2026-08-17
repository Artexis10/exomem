## 1. Schema, hashing, and chain

- [x] 1.1 Unit tests for the row schema: required fields, `schema_version` constant, `outcome`
      enum `ok|refused|error`, and the shape of `args` (`{name: {len, sha256}}`).
- [x] 1.2 Tests for canonical JSON (sorted keys, compact separators, UTF-8) and for
      `row_hash = sha256(canonical(row without row_hash))`.
- [x] 1.3 Tests for the chain: `prev_hash` equals the prior `row_hash`, the genesis anchor
      (64 zeros), a strictly increasing `sequence`, and detection of a dropped or edited row as a
      gap or a broken link.
- [x] 1.4 Implement `src/exomem/call_ledger.py` row construction, canonical hashing, and the
      per-row chain.
- [x] 1.5 A process restart resumes the chain from the live file's last row rather than
      restarting `sequence` — otherwise every restart would look exactly like the tampering the
      sequence exists to detect.

## 2. Redaction discipline

- [x] 2.1 Tests proving argument values, note body content, raw tokens, and raw subjects never
      enter a row; only names, per-arg `len`+`sha256`, and structural `target_paths` do.
- [x] 2.2 Implement argument reduction to shape+hash and wire `caller_principal_hash` from
      `mcp_retry_scope()`, which is currently discarded.
- [x] 2.3 Prove the guarantee holds independent of `EXOMEM_HOSTED_CELL` — it must not depend on
      `privacy_log.install_hosted_log_redaction`, which is simply off for local installs.
- [x] 2.4 Nested structures hash whole, so a value buried inside an argument object (an
      `edit_memory` `operation.new_body`) is covered by the same construction as a top-level one.

## 3. Caller identity

- [x] 3.1 Tests that `mcp_caller_identity()` reads `clientInfo` name/version, the transport, and
      the session id off the live MCP context, and that absence of a context yields nulls rather
      than a raise.
- [x] 3.2 Implement `mcp_caller_identity()` in `command_surface.py` and carry its fields on every
      row **in the clear** — they identify software, not a person, and hashing them would destroy
      the only field that answers "which client?" while protecting nothing.
- [x] 3.3 Test that two clients against one vault are distinguishable from the ledger alone.

## 4. Emission — one row per call, at every exit

- [x] 4.1 Tests that `CallTraceMiddleware.on_call_tool` emits exactly one row per call carrying
      `request_id`, `tool`, `duration_ms`, and arg shape, for reads as well as writes.
- [x] 4.2 Emit the row inside the scope already bound by `mcp_request_context`; record
      `outcome="error"` with the exception class in the `except` branch. Leave the existing
      `tool_start`/`tool_success` prose lines unchanged.
- [x] 4.3 Read the wrapper's per-call-token breadcrumb (`pop_tool_failure`) at that same point so
      a returned refusal envelope is recorded as `refused` with its `OpError.code`, not as `ok`.
      **Superseded in part:** the breadcrumb this task proposed already landed on main
      (`_record_tool_failure` / `pop_tool_failure`) — ContextVars do not propagate back out of
      FastMCP's anyio threadpool, so a ContextVar could not have served. What was missing was the
      durable sink, not the signal.
- [x] 4.4 Cover the exits that reject **before** the leaf, which would otherwise leave no row at
      all: the binary-blob content guard, and `edit_memory` operation normalization. The latter
      required moving the normalization inside the request scope.
- [x] 4.5 Test that an unstructured exception keeps its class name rather than being laundered
      into a plausible-looking refusal code.

## 5. Append discipline

- [x] 5.1 Each row is one bounded, newline-terminated buffer written with a single `os.write` to
      an `O_APPEND` descriptor, so a row stays intact even if a child process inherited the
      descriptor.
- [x] 5.2 Bound the variable-length fields (per-field chars, argument count, target-path count)
      and set `truncated` when a bound bites, so one pathological argument cannot produce an
      unbounded row.
- [x] 5.3 Sequence, chain, and append run under one cheap in-process lock; mode `0600`; no
      cross-process lock file. **Single-writer assumption:** all MCP tool calls are dispatched in
      one server process, and `resolve_log_dir()` is host-local, so two writers cannot share a
      ledger. Documented in the module rather than carrying a `writer_id` field for a case the
      deployment shape excludes.

## 6. Rotation and archive

- [x] 6.1 Size-triggered rotation keeps the newest N rows and moves the older tail byte-exact
      into a content-addressed `ledger-<hash>.jsonl` archive, modeled on `log.md`.
- [x] 6.2 `sequence` does not reset across rotation, and the archived segment's last `row_hash`
      is still the live file's first retained `prev_hash`, so a verifier walks archive-then-live
      continuously.
- [x] 6.3 Implement rotation in the same appender under the same lock, writing archives under
      `<log_dir>/ledger-archive/`.

## 7. Placement, configuration, and soft-fail

- [x] 7.1 Resolve the ledger dir host-local, **outside the vault and outside the writer-lease
      boundary**, overridable via `EXOMEM_CALL_LEDGER_DIR`. **Changed from the proposal:** it
      defaults to `resolve_log_dir()` — beside `mutations.jsonl` — rather than to a `calls/`
      directory under the writer-lease `state_dir`. A second location would have been a second
      thing to explain, and every other journal already routes through that one function.
- [x] 7.2 Add the `EXOMEM_DISABLE_CALL_LEDGER` kill switch (mirroring `EXOMEM_DISABLE_QUERY_LOG`)
      and make every ledger operation soft-fail, swallowing exceptions like `query_log._append`.
- [x] 7.3 Test that a ledger failure — full disk, permission error — leaves the call's own result
      untouched.

## 8. Operator surface

- [x] 8.1 Add `ledger` as a `exomem logs --file` alias, sourced from one list so the CLI's
      `choices` cannot drift from what `resolve_log_file` accepts.
- [x] 8.2 Join the ledger in `exomem trace <request_id>`. It is the only source covering *every*
      tool call, so it is what makes a trace complete for a tool that is neither a read, a query,
      nor a mutation.
- [x] 8.3 Make reads archive-aware, ordering segments by the sequence the rows carry rather than
      by filename — archive names are content-addressed and say nothing about age.
- [x] 8.4 Add `exomem logs verify`, walking archive-then-live as one chain and **anchored** to
      the genesis row. Without the anchor a dropped *oldest* segment verifies clean: nothing
      precedes the first surviving row to contradict it, so the chain merely appears to start
      later than it did.

## 9. Tests and gates

- [x] 9.1 The CI leak test: drive calls with a unique sentinel in arguments and assert the
      sentinel and a credential-shaped value appear nowhere in the ledger file or its archives;
      assert the restrictive file mode where the platform has one.
- [x] 9.2 Assert the append does not fsync on the hot path, and measure the per-row cost:
      median 0.35 ms, p99 0.72 ms (Windows, 4 KB argument). Rows lost to a hard crash stay
      detectable as a later `sequence` gap.
- [x] 9.3 Run `ruff check`, the scaffold leak check, and strict OpenSpec validation; run the full
      suite with embeddings disabled and record totals against the base commit.

## 10. Documentation

- [x] 10.1 Document the ledger: location, row schema, chain and verification, rotation and
      retention, the redaction guarantee, the kill switch, and how it differs from
      `queries.jsonl`/`reads.jsonl`/`writes.jsonl` and from the `.idempotency.sqlite` replay
      cache.
