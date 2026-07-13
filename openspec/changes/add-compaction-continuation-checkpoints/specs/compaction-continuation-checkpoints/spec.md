## ADDED Requirements

### Requirement: Lifecycle checkpointing is local-first and non-destructive

For Claude Code and Codex CLI installations, Exomem SHALL register a `PreCompact` command hook matching `manual|auto`. For Claude Code, Exomem SHALL additionally register its documented unfiltered `SessionEnd` hook. Codex CLI 0.144.3 SHALL NOT receive a `SessionEnd` registration because that version does not expose the event; a future Codex adapter MAY enable it only after pinning a named official version and real emitted-event proof. Each supported write event SHALL make a best-effort local checkpoint attempt without calling a model, MCP, REST, or Exomem CLI operation. Every in-process failure SHALL exit zero without intentionally blocking compaction/exit and SHALL preserve the last valid checkpoint; client kill or timeout is outside that guarantee. A semantically different event such as per-turn `Stop` SHALL NOT substitute for missing `SessionEnd` support.

#### Scenario: Connector is unavailable during automatic compaction

- **WHEN** a supported client emits automatic `PreCompact` while the connector, OAuth, network, and vault are unavailable
- **THEN** the hook attempts a local checkpoint using available event/repository provenance
- **AND** makes no network, model, MCP, REST, or Exomem subprocess call

#### Scenario: Checkpoint attempt fails in process

- **WHEN** storage, git inspection, or serialization raises inside the hook
- **THEN** the exception is contained, the process exits zero, and the prior valid checkpoint is not replaced by partial output

#### Scenario: Claude session exits without prior compaction

- **WHEN** Claude Code emits `SessionEnd` for a session whose structural state changed since its last checkpoint
- **THEN** the same local checkpoint core records the final structural state without stdout or context injection
- **AND** a later exact-session resume can use that checkpoint

#### Scenario: Codex version has no SessionEnd event

- **WHEN** installing for pinned Codex CLI 0.144.3
- **THEN** config contains no Exomem `SessionEnd` checkpoint handler
- **AND** the installer preserves any unrelated user-provided `SessionEnd` group without treating it as Exomem parity

### Requirement: Checkpoints contain structural evidence and no conversation content

The versioned checkpoint SHALL contain bounded event, transcript-binding, git/worktree, dirty-path, continuation-artifact, checkbox line-number, hash, and degradation metadata. Transcript binding SHALL include observed size/mtime plus bounded historical-slice offset/length/digest, never slice bytes. It SHALL NOT persist conversation messages/excerpts, system/developer prompts, tool inputs/results, compacted summary content, artifact text/headings, environment values, git diffs, or credential contents. Structural names such as repository-relative paths and branch names are permitted recovery metadata, SHALL be bounded and same-session-bound, and SHALL NOT appear in metadata logs. The total checkpoint SHALL be at most 64 KiB and every bounded collection/path SHALL expose truncation.

#### Scenario: Transcript contains nested and echoed secrets

- **WHEN** user or assistant text embeds tool history, system prompts, bearer tokens, API keys, private keys, or reinjected hook context
- **THEN** none of that text is parsed into or copied to the checkpoint
- **AND** only the transcript's bounded-tail digest, size, mtime, and safe relative/path-hash binding may appear

#### Scenario: Transcript is malformed or changes format

- **WHEN** the transcript contains malformed UTF-8, oversized JSONL lines, unknown records, plaintext summary records, or encrypted compaction records
- **THEN** record content is never parsed semantically
- **AND** the hook still produces available structural state or a valid degraded checkpoint

#### Scenario: Claude manual compact instructions contain secrets

- **WHEN** a Claude Code `PreCompact` payload carries arbitrary secret or tool text in `custom_instructions`
- **THEN** the adapter ignores that non-allowlisted field
- **AND** none of its value appears in checkpoint state, logs, structural digest input, or reinjected output

### Requirement: Lifecycle adapters normalize clients before state access

The implementation SHALL expose one versioned internal event/output contract and one shared checkpoint, storage, validation, artifact, and rendering core. Claude Code and Codex CLI adapters SHALL map pinned documented raw command-hook fields and output envelopes to that contract, pass an explicit client identity, reject unknown/ambiguous event shapes before state access, ignore every non-allowlisted content-bearing field, and never fork core recovery logic. A new client SHALL NOT be advertised as supported until its envelope, lifecycle configuration, and output adapter have contract tests.

#### Scenario: Equivalent client events normalize identically

- **WHEN** documented Claude Code and Codex CLI payloads describe the same lifecycle event and structural workspace state
- **THEN** their adapters produce the same normalized core fields except explicit client identity and client-provided optional fields
- **AND** both use the same profiler, storage, validator, and renderer code paths

#### Scenario: Adapter attempts to substitute core behavior

- **WHEN** architecture tests spy on both adapters across write and reinjection events
- **THEN** each invokes the same core entrypoints
- **AND** no client-specific profiler, storage, validator, artifact scanner, or renderer implementation is reachable

#### Scenario: Unknown or ambiguous payload is received

- **WHEN** a configured adapter receives an unsupported event, source, trigger, missing session identity, or conflicting field aliases
- **THEN** it exits zero without reading or writing checkpoint state

### Requirement: State paths are client-aware, collision-resistant, and symlink-safe

Runtime and installation SHALL resolve an explicit client's home consistently. `EXOMEM_HOOK_HOME` SHALL override either client for isolated/test installs. Otherwise Codex SHALL use `CODEX_HOME`, then `~/.codex`; Claude SHALL use `CLAUDE_CONFIG_DIR`, then `~/.claude`. Per-session state paths SHALL include the client namespace plus a sanitized prefix and cryptographic client/session-ID hash suffix, be created with restrictive permissions where supported, and reject symlink/reparse/non-regular traversal through platform-safe handle-based operations rather than a check-then-open sequence.

#### Scenario: Distinct unsafe session IDs normalize to the same prefix

- **WHEN** two session IDs have the same sanitized/truncated prefix
- **THEN** their hash suffixes produce distinct checkpoint directories

#### Scenario: Two clients expose the same session ID

- **WHEN** Claude Code and Codex CLI each emit the same textual session ID
- **THEN** explicit client namespaces and client/session hashes keep their checkpoint state distinct

#### Scenario: State path is replaced by a symlink

- **WHEN** a checkpoint directory, lock, current, previous, or temporary path is a symlink
- **THEN** the hook refuses that path, follows no target, exits zero, and leaves the target untouched

### Requirement: Concurrent writes are auto-released, idempotent, and stale-writer safe

Each client session SHALL use a bounded OS advisory lock on a mode-restricted regular file opened through the platform-safe handle adapter. The lock SHALL release automatically on handle close or process death and SHALL have no owner-metadata publication or stale-reclamation phase. Under the lock, the writer SHALL hash a canonical structural payload containing all non-volatile normalized event, transcript, git/workspace, artifact, and degradation/truncation evidence while excluding observation time and checkpoint ID. Checkpoint identity SHALL include schema, client, session/optional-turn/event/trigger, transcript size/mtime/tail digest, and that structural-payload digest. Same-ID delivery SHALL not rotate or duplicate history; when its observation is newer, it MAY atomically refresh the current generation's `observed_at_ns`, event-order tuple, and freshness while preserving the same checkpoint ID. Changed structural state SHALL produce a different ID even with an unchanged or missing transcript. A writer older than the validated current event-order tuple `(transcript_mtime_ns, transcript_size, observed_at_ns, checkpoint_id)` SHALL not overwrite current. Temporary files SHALL be unique, mode-restricted, flushed, and atomically replaced. At most current and previous valid generations SHALL remain.

#### Scenario: Two processes deliver the same event concurrently

- **WHEN** two hook processes observe the same logical event
- **THEN** exactly one checkpoint ID becomes current and no duplicate history generation is created

#### Scenario: Lock owner dies during acquisition or write

- **WHEN** a writer is killed at any tested stage after lock-file creation, acquisition, rotation, or temporary write
- **THEN** the OS releases lock ownership with the process handle
- **AND** a later writer can acquire the session without manual stale-lock recovery

#### Scenario: Structure changes without transcript change

- **WHEN** HEAD, dirty paths, artifact hashes, or checkbox lines change while transcript provenance is identical or unavailable
- **THEN** the structural-payload digest and checkpoint ID change
- **AND** the new checkpoint is not suppressed as duplicate delivery

#### Scenario: Older and newer events interleave

- **WHEN** a writer for an older observation obtains the lock after a newer checkpoint committed
- **THEN** it detects the newer order and does not replace or rotate it

#### Scenario: Process stops between rotation and replacement

- **WHEN** interruption leaves no valid current after a prior current was rotated
- **THEN** no partial temporary file is accepted
- **AND** the loader can recover the validated previous generation as an explicitly labeled rollback

### Requirement: Artifact discovery is closed, bounded, and content-free

The hook SHALL inspect only regular, non-symlink files beneath the validated git root matching `.superpowers/sdd/progress.md`, `.task/TASK.md`, `.task/RESULT.md`, or `openspec/changes/*/tasks.md`. It SHALL bound candidates and read bytes before inspection, prefer dirty OpenSpec task files, use a deterministic remaining order, and store only repository-relative path, provenance hashes/times/sizes, checkbox counts, and incomplete checkbox line numbers.

#### Scenario: Repository contains symlinked or excessive task artifacts

- **WHEN** allowed-looking candidates include symlinks, paths escaping the git root, oversized files, or more candidates/checkboxes than permitted
- **THEN** unsafe candidates are skipped, bounds are enforced before reading, and truncation/degradation is recorded

#### Scenario: Active OpenSpec task file is dirty

- **WHEN** a matching OpenSpec `tasks.md` is present in bounded git dirty paths
- **THEN** it is considered before non-dirty incomplete task files
- **AND** the checkpoint stores its relative path/hash and checkbox line numbers without task text

### Requirement: Reinjection validates binding, freshness, and fallback

For Claude Code and Codex CLI installations, Exomem SHALL register `SessionStart` matching `compact|resume`. It SHALL inject only a checkpoint whose schema, exact client and session ID, state-root binding, append-safe transcript binding when available, and 30-day freshness are valid. Append-safe validation SHALL require the same path binding, current size at least the observed size, and the saved historical slice digest at its saved offset; later appended compaction records are allowed. It detects truncation and changes overlapping that slice but SHALL NOT claim detection of in-place rewrites elsewhere in the file. It SHALL select valid current first and MAY use valid previous only when current is absent/corrupt, labeling that digest `rollback`. Otherwise it SHALL inject nothing.

#### Scenario: Current checkpoint is valid

- **WHEN** compact/resume starts with a fresh current checkpoint bound to the same session and transcript
- **THEN** current is selected for rendering

#### Scenario: Compaction appends to the transcript

- **WHEN** the same bound transcript is larger/newer after compaction but still contains the saved historical slice at its original offset
- **THEN** binding remains valid and the checkpoint may be injected

#### Scenario: Transcript is truncated or saved slice changes

- **WHEN** the current transcript is shorter than the observed size or bytes overlapping its saved historical slice differ
- **THEN** the checkpoint is rejected without injection

#### Scenario: Current is corrupt and previous is valid

- **WHEN** current fails validation but previous is fresh and bound to the same session/transcript
- **THEN** previous is rendered with an explicit rollback warning

#### Scenario: Checkpoint is stale or foreign

- **WHEN** candidate state is expired, belongs to another session, or conflicts with the current transcript binding
- **THEN** the hook exits zero without injecting checkpoint context

### Requirement: Injected continuation context is bounded and advisory

The hook SHALL render at most 4096 UTF-8 bytes in `additionalContext`, excluding the JSON envelope. It SHALL include only checkpoint ID/status, structural repository/transcript evidence, repository-relative artifact pointers, checkbox line numbers/counts, and degradation/truncation flags. It SHALL instruct the agent to reconcile those pointers with the client's compacted context and use normal Exomem governance only for a genuine durable stepping-stone. It SHALL NOT infer an objective/next action, copy content, claim capture completion, or describe itself as an idempotent capture queue.

#### Scenario: Resumed agent receives continuation evidence

- **WHEN** a valid checkpoint has more structural records than fit the context bound
- **THEN** the UTF-8-byte bound is enforced with deterministic truncation
- **AND** the agent receives artifact paths/line numbers and instructions to inspect evidence rather than fabricated semantic conclusions

#### Scenario: No durable conclusion exists

- **WHEN** the resumed work is transient execution state only
- **THEN** the advisory permits continuing without an Exomem write
- **AND** no transcript or checkpoint content is automatically written as compiled memory

### Requirement: Multi-client installation and migration are narrow and fail closed

`exomem install-hook --client claude|codex|all` SHALL install the shared checkpoint script with an explicit client argument and one current handler in each adapter-supported event of the target user config file using that client's configuration schema and command forms; it SHALL NOT claim uniqueness across project, plugin, managed, or other composed hook sources. The pinned capability matrix SHALL be Claude `PreCompact`/`SessionEnd`/`SessionStart` and Codex 0.144.3 `PreCompact`/`SessionStart`. Claude SHALL use `settings.json` under `CLAUDE_CONFIG_DIR`/`~/.claude`; Codex SHALL use `hooks.json` under `CODEX_HOME`/`~/.codex` plus its Windows command override. Merge SHALL use a closed exact-basename allowlist, require the explicit client argument for current entries, permit its absence only for explicitly named legacy entries, preserve unrelated groups/order—including unsupported/unrelated `SessionEnd` groups in Codex config—and existing capture/retrieve behavior, honor client-home and explicit overrides, reject malformed/non-object JSON without writing, and update valid config through mode-preserving backup plus same-directory atomic replacement. Immediately before replacement it SHALL compare source identity/stat/digest and retry from fresh bytes or fail unchanged on observed concurrent drift. A normalized no-op SHALL create neither backup nor rewrite; real changes SHALL use unique backup names. Re-running SHALL be idempotent. `--client all` SHALL report results per client, exit non-zero on any partial failure, SHALL NOT overwrite an invalid config for either client, and SHALL reject singular `--hook-dir`/`--settings` overrides rather than sharing one explicit path across clients.

#### Scenario: Existing config contains unrelated lifecycle hooks

- **WHEN** valid Codex hook config contains unrelated `PreCompact`, `SessionEnd`, `SessionStart`, `Stop`, and `UserPromptSubmit` entries
- **THEN** installation preserves those entries in order, adds exactly one current checkpoint handler to supported `PreCompact` and `SessionStart` in that target file, and adds none to `SessionEnd`

#### Scenario: Claude config contains unrelated lifecycle hooks

- **WHEN** valid Claude settings contain unrelated `PreCompact`, `SessionEnd`, `SessionStart`, `Stop`, and `UserPromptSubmit` entries
- **THEN** installation preserves those entries in order and adds exactly one Claude checkpoint handler to each required event
- **AND** the installed command passes explicit client identity to the shared script

#### Scenario: Concurrent config mutation is observed

- **WHEN** the target config identity, stat, or digest changes after the installer reads it and before replacement
- **THEN** the installer retries from the new bytes or fails without replacement after its bounded retry limit
- **AND** it never knowingly replaces the observed newer version with a stale merge

#### Scenario: Reinstall is already normalized

- **WHEN** deployed scripts and normalized target config are already current
- **THEN** installation performs no config rewrite and creates no backup

#### Scenario: Existing config is malformed

- **WHEN** hook config is malformed JSON or not an object
- **THEN** installation fails with a clear error and writes neither replacement nor truncated config

#### Scenario: Custom Codex home is configured

- **WHEN** `CODEX_HOME` points to a custom directory and no explicit CLI path override is supplied
- **THEN** deployed scripts, hook config, runtime checkpoint state, logs, and health checks resolve under that same home

#### Scenario: Custom Claude home is configured

- **WHEN** `CLAUDE_CONFIG_DIR` points to a custom directory and no explicit CLI path override is supplied
- **THEN** deployed scripts, settings, runtime checkpoint state, logs, and health checks resolve under that same home

#### Scenario: One config is invalid during all-client install

- **WHEN** `--client all` encounters one valid client config and one malformed client config
- **THEN** it reports each result explicitly, safely installs the valid client, leaves the malformed config byte-for-byte unchanged, and exits non-zero

#### Scenario: All-client install receives a singular path override

- **WHEN** `--client all` is combined with `--hook-dir` or `--settings`
- **THEN** argument validation rejects the invocation before changing either client

### Requirement: Acceptance proves both adapters and one live client

Release acceptance SHALL pin official envelope fixtures to named Claude Code/Codex versions and documentation sources, run the same normalized event, privacy, concurrency, storage, validation, renderer, installer, and shared-core architecture contract suites for both adapters, and run installed-script integration fixtures for both. It SHALL additionally exercise Claude's documented `SessionEnd` adapter contract. It SHALL record a real Codex CLI session in which the client itself emits supported `PreCompact`, `SessionStart(compact)`, and `SessionStart(resume)` events after installation; piping fabricated JSON directly to the script does not satisfy this live lane. A live Claude Code exercise MAY be recorded when available but SHALL NOT be required when its pinned documented envelopes and installed configuration pass the adapter contract suite.

#### Scenario: Codex live lane passes and Claude contracts pass

- **WHEN** the shared core suite, both client adapter/config suites, and Codex CLI end-to-end exercise pass
- **THEN** the feature satisfies cross-client release acceptance without requiring a paid or interactive Claude session

#### Scenario: A client adapter contract fails

- **WHEN** either client no longer normalizes a documented event or emits a valid reinjection envelope
- **THEN** release acceptance fails even if the other client's live end-to-end exercise passes

### Requirement: Diagnostics, retention, and rollback preserve state safety

`install-hook --check` SHALL validate exact client-supported registrations/matchers, deployed hashes, legacy entries, and runtime permissions/age/status; no runtime state before the first write event SHALL be a warning. Metadata-only logs SHALL exclude content, environment values, and absolute paths. Retention SHALL default to 30 days. An expired non-current session SHALL be atomically renamed while writers are excluded to a unique tombstone under the same state root; deletion SHALL target only the validated tombstone, allowing canonical-path writers to create fresh state safely. A platform fallback that must close the session handle before rename SHALL require all writers/pruners to follow fixed `root -> session` coordination, with writers releasing root after safe session-handle acquisition and pruners holding it through tombstone rename. Disablement SHALL bypass `PreCompact`, Claude `SessionEnd`, and `SessionStart` without deleting state. Rollback SHALL be documented as manual removal because no uninstall CLI is added.

#### Scenario: Health check runs before first checkpoint

- **WHEN** scripts/config are current and no checkpoint exists
- **THEN** health succeeds with a first-run warning

#### Scenario: Checkpointing is disabled

- **WHEN** the disable variable is truthy
- **THEN** PreCompact and any client-supported SessionEnd write nothing, SessionStart injects nothing, and existing state is preserved

#### Scenario: Old state is pruned

- **WHEN** a later hook run sees an expired non-current session directory and acquires its lock
- **THEN** it atomically renames only that verified session directory to a unique in-root tombstone while excluding writers
- **AND** later deletion touches only the tombstone while active/fresh/symlinked paths and newly recreated canonical state remain intact
