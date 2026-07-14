## 1. Structural Checkpoint Contract Tests

- [x] 1.1 Add failing pure-logic tests for the normalized event contract; version/source-pinned Claude/Codex snake/camel-case envelope fixtures; explicit-client rejection; ignored non-allowlisted fields; per-client home resolution; schema/bounds; collision-resistant client/session paths; deterministic IDs including transcript and canonical structural-payload digests; event ordering; truncation; and degraded non-git/no-transcript state.
- [x] 1.2 Add failing privacy-by-construction tests proving nested tool/system/secret text in user or assistant records, secret-bearing Claude `PreCompact.custom_instructions`, plaintext/encrypted compaction items, malformed UTF-8, and oversized JSONL lines never enter checkpoint, structural digest input, logs, or reinjected output.
- [x] 1.3 Add failing artifact-policy tests for the exact allowlist, repository containment, symlink rejection, bounded pre-read selection, dirty-OpenSpec priority, deterministic ordering, hashes/counts/line numbers, and zero stored task text.
- [x] 1.4 Add failing architecture/spied tests proving both client adapters invoke the same write/select/render core entrypoints and cannot substitute client-specific profiler, storage, validator, artifact scanner, or renderer paths.

## 2. Concurrent Local Lifecycle Checkpoint Implementation

- [x] 2.1 Implement the standalone stdlib-only structural profiler, transcript tail binding, git/worktree metadata, artifact checkbox profiler, bounds, and degradation flags.
- [x] 2.2 Add failing true-multiprocess tests for OS advisory-lock timeout/auto-release and kill at every creation/acquisition/write stage; same-ID delivery without history rotation plus newer-delivery freshness refresh; unchanged/missing transcript with changed HEAD/dirty/artifact state; interleaved older/newer events including absent-transcript sentinels; unique temporary files; rotation interruption; current/previous validation; mode safety; live POSIX symlink/path-swap attacks plus structural Windows handle guards and a Windows-only live reparse test; and prune-versus-writer tombstone races on both held-session-lock and fixed `root -> session` fallback paths.
- [x] 2.3 Implement platform-safe handle operations, bounded auto-released OS advisory locking, structural-payload identity, stale-writer rejection, idempotent current/previous storage with same-ID freshness refresh but no history duplication, unique atomic writes, restrictive creation modes, tombstone retention, metadata-only logging, disablement, and all-exception soft-fail behavior.
- [x] 2.4 Add parametrized Claude/Codex subprocess-level manual/auto `PreCompact` contract tests plus Claude-only `SessionEnd` contract tests for connector/vault absence, non-git cwd, client-specific custom home, spaces/backslashes, total-size/latency bounds, zero stdout, and zero exit status. Prove pinned Codex 0.144.3 rejects/does not install `SessionEnd`.

## 3. Validated Continuation Reinjection

- [x] 3.1 Add failing selection tests for exact client/session/state/path binding, append-safe historical-slice validation, appended compaction records, truncated/saved-slice-change rejection, explicit non-detection of unrelated in-place rewrites, 30-day freshness, valid-current priority, labeled previous rollback, and stale/foreign/corrupt silence.
- [x] 3.2 Add failing renderer tests for the 4096-UTF-8-byte `additionalContext` bound, checkpoint/status, structural pointers and checkbox lines, degradation/truncation, advisory capture language, and absence of semantic inference/content.
- [x] 3.3 Implement shared `SessionStart(compact|resume)` validation, fallback selection, bounded continuation rendering, Claude/Codex output adapters, and soft-fail behavior.
- [x] 3.4 Add parametrized Claude/Codex subprocess-level compact/resume contract tests for valid current, rollback previous, missing/invalid/oversized state, disabled mode, repeated resume, connector-unavailable recovery, and exact client/session isolation.

## 4. Installer, Migration, And Diagnostics

- [x] 4.1 Add failing installer tests for `EXOMEM_HOOK_HOME`, `CODEX_HOME`, and `CLAUDE_CONFIG_DIR` resolution; pinned capability-matrix wiring (Claude `PreCompact`/`SessionEnd`/`SessionStart`, Codex 0.144.3 `PreCompact`/`SessionStart` only); explicit client arguments; Codex `commandWindows`; Claude command form; preservation of unrelated Codex `SessionEnd`; `--client all` result isolation/non-zero partial failure/singular-path-override rejection; idempotency; exact legacy basename migration; unrelated-order preservation; and unchanged capture/retrieve hooks.
- [x] 4.2 Add failing configuration-safety tests for malformed/non-object fail-closed behavior, source identity/stat/digest drift retry/failure, mode-preserving unique backup, normalized no-op with no backup/rewrite, same-directory atomic replacement, interrupted write preservation, and platform symlink/reparse refusal.
- [x] 4.3 Extend the installer/merge adapter registry to deploy the shared checkpoint script for three Claude events and two Codex events without duplicating capture/retrieve hooks, and surface per-client actionable errors without overwriting invalid config.
- [x] 4.4 Extend `install-hook --check` and human/JSON reports with per-client exact registration/matcher/hash, custom-home, first-run warning, runtime permission/age/status, legacy-entry, and metadata-log checks.

## 5. Documentation And Verification

- [x] 5.1 Update CLI help, README, and AI-assistant docs with the shared-core/client-adapter model, supported-client matrix, structural-only content, lifecycle, privacy, local state, connector-failure recovery, retention/freshness, disablement, client-specific reload/trust, exact migration, and manual rollback.
- [x] 5.2 Run focused hook/checkpoint/installer tests, scaffold leak checks, Ruff, compile checks, diff checks, and strict OpenSpec validation.
- [x] 5.3 Install into isolated temporary Claude and Codex homes and prove both installed-script adapter/config integration suites, including Claude `SessionEnd` and Codex absence/preservation behavior. Then record a real Codex CLI session—client-generated events, not piped fixtures—covering manual `PreCompact`, `SessionStart(compact)`, and `SessionStart(resume)`, with a valid newest checkpoint and bounded structural reinjection requiring no MCP credential. Record a live Claude `SessionEnd` run when available, but do not make it a release blocker.
- [x] 5.4 Run the complete lean suite with embeddings/media disabled and record totals plus any baseline-only failures.
- [x] 5.5 Have an independent reviewer verify every scenario, especially automated shared-core/no-fork proof, both pinned client adapters, privacy by construction, structural-payload identity, lock auto-release, concurrency/stale-writer ordering, symlink/reparse safety, tombstone pruning, concurrent config drift, fallback labeling, and no automatic transcript-to-memory write.
