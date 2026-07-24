## 1. Schema, hashing, and chain (pure logic first)

- [ ] 1.1 Add failing unit tests for the row schema: required fields, `schema_version` constant,
      `outcome` enum `ok|refused|error`, and the shape of `args` (`{name: {len, sha256}}`).
- [ ] 1.2 Add failing tests for canonical JSON (sorted keys, compact separators, UTF-8) and for
      `row_hash = sha256(canonical(row without row_hash))`.
- [ ] 1.3 Add failing tests for the chain: `prev_hash` equals the prior `row_hash`, the genesis
      anchor (64 zeros), a strictly increasing `sequence`, and detection of a dropped/edited row
      as a gap or broken link.
- [ ] 1.4 Implement `src/exomem/call_ledger.py` row construction, canonical hashing, and the
      per-row chain to make 1.1–1.3 pass.

## 2. Redaction discipline

- [ ] 2.1 Add failing tests proving argument values, note body content, raw tokens, and raw
      subjects never enter a row; only names, per-arg `len`+`sha256`, and structural
      `target_paths` do.
- [ ] 2.2 Implement argument reduction to shape+hash and wire `caller_principal_hash` from
      `mcp_retry_scope()` (`src/exomem/command_surface.py:259-284`), which is currently
      discarded.
- [ ] 2.3 Prove the ledger is redaction-safe independent of `EXOMEM_HOSTED_CELL`, i.e. it does
      not depend on `privacy_log.install_hosted_log_redaction`
      (`src/exomem/privacy_log.py:38-66`).

## 3. Seam A — middleware identity, timing, and single-row emission

- [ ] 3.1 Add failing tests that `CallTraceMiddleware.on_call_tool`
      (`src/exomem/server.py:83-145`) emits exactly one ledger row per call at its
      return/except point, carrying `request_id`, `tool`, `duration_ms`, and arg shape.
- [ ] 3.2 Emit the row reusing the request scope already bound by `mcp_request_context`
      (`src/exomem/server.py:90`); record `outcome="error"` with the exception type in the
      `except` branch (`:138-145`). Leave the existing `tool_start`/`tool_success` prose log
      lines unchanged.

## 4. Seam B — wrapper outcome and OpError.code

- [ ] 4.1 Add failing tests that the `bind_vault` outcome branch
      (`src/exomem/command_surface.py:234-242`) records `outcome="refused"` plus the
      `OpError.code` onto a request-scoped context, and leaves `outcome="ok"` on normal return.
- [ ] 4.2 Implement the request-scoped outcome ContextVar and have the middleware (Seam A) read
      it back so a returned refusal envelope is recorded as `refused`, not `success`.
- [ ] 4.3 Add tests covering `WRITER_LEASE_REQUIRED`, `SEMANTIC_IDENTITY_DUPLICATE`, and
      `MUTATION_WARMING` refusal codes end to end.

## 5. Append discipline and concurrency

- [ ] 5.1 Add failing tests that each row is a single newline-terminated `os.write` to an
      `O_APPEND` descriptor, that a row exceeding the atomic-write bound truncates its
      variable-length fields with `truncated=true` rather than splitting, and that a child
      process inheriting the descriptor cannot interleave a partial row.
- [ ] 5.2 Implement the in-process appender (sequence + chain + append) under a cheap in-process
      lock, mode `0600`, no cross-process lock file.
- [ ] 5.3 Add a verification test asserting all MCP tool calls are dispatched in the main server
      process (the single-writer assumption); reserve a `writer_id` field for a per-writer-stream
      fallback if that assumption is ever broken.

## 6. Rotation and archive

- [ ] 6.1 Add failing tests for size-triggered rotation that keeps the newest N rows and moves
      the older tail byte-exact into a content-addressed `ledger-<hash>.jsonl` archive, modeled
      on `log.md` (`src/exomem/vault.py:4451-4508`).
- [ ] 6.2 Add failing tests that `sequence` does not reset across rotation and that the archived
      segment's last `row_hash` equals the live file's first retained `prev_hash`, with a
      per-archive header recording `[first_sequence, last_sequence]` and boundary hashes.
- [ ] 6.3 Implement rotation in the same in-process appender under the same lock (no
      rename-on-rotate handler), writing archives under `<state_dir>/calls/_archive/`.

## 7. Placement, configuration, and soft-fail

- [ ] 7.1 Resolve the ledger dir host-local (default `<state_dir>/calls/`, `state_dir` mirroring
      `~/.cache/exomem`, `src/exomem/writer_lease.py:275`), overridable via
      `EXOMEM_CALL_LEDGER_DIR`; prove it resolves outside the vault tree and is writable when the
      vault is read-only.
- [ ] 7.2 Add the `EXOMEM_DISABLE_CALL_LEDGER` kill switch (mirroring `EXOMEM_DISABLE_QUERY_LOG`)
      and make every ledger operation soft-fail, swallowing exceptions like
      `query_log._append` (`src/exomem/query_log.py:65-71`).

## 8. Tests, latency, and leak gate

- [ ] 8.1 Add the CI leak test: drive calls with a unique sentinel in arguments and assert the
      sentinel and any raw credential appear nowhere in the ledger file or its archives; assert
      the restrictive file mode.
- [ ] 8.2 Add a per-row latency micro-check confirming append cost stays well under 1 ms and does
      not fsync on the hot path; confirm no regression against `tests/test_latency_gate.py`
      (`CEIL_TOTAL_MS=5000`) and `scripts/semantic_write_latency.py` (`VALIDATE_MEDIAN_MS=500`).
- [ ] 8.3 Add a JSON Schema for a ledger row and validate a sample of emitted rows against it in
      CI, mirroring the `fable-delegate` `audit.jsonl` schema-in-CI discipline.
- [ ] 8.4 Run `ruff check`, the scaffold leak check, and strict OpenSpec validation; run the lean
      suite with embeddings/media disabled and record totals.

## 9. Documentation

- [ ] 9.1 Document the ledger: location, row schema, chain/verification, rotation/retention,
      redaction guarantee, the kill switch, and how it differs from `queries.jsonl`/`reads.jsonl`/
      `writes.jsonl` and from the `.idempotency.sqlite` replay cache.
