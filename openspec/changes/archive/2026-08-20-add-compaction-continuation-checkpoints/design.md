## Context

Exomem currently installs two standalone, stdlib-only reliability hooks into Claude Code and Codex CLI: `UserPromptSubmit` reminds the agent to retrieve relevant memory, and `Stop` reminds it to capture durable conclusions. Neither preserves independently verifiable working-state evidence at compaction or ordinary session-exit time. Both clients expose `PreCompact` with `manual|auto` matchers and `SessionStart` with `compact|resume` sources, supply session, working-directory, and transcript provenance to command hooks, and accept session-start `additionalContext`. Claude Code additionally exposes `SessionEnd`; current Codex CLI 0.144.3 does not, despite unrelated internal session-end strings. Their configuration files, field naming, command forms, home overrides, and optional lifecycle capabilities differ.

Transcript representation is explicitly unstable across clients and versions. Installed Codex 0.144.3 was observed storing its compaction item encrypted, while other versions and Claude Code may differ; this design depends on neither plaintext nor encrypted summary content. The reasoning model already receives its client's own compacted context. Exomem records only structural evidence that lets the model verify where work was happening and what artifacts to reopen.

The failure model includes an unavailable connector, missing OAuth, no vault path, malformed/missing transcript, non-git cwd, multiple matching hooks launched concurrently, simultaneous compactions, process interruption, symlink attacks in local state, malformed existing hook configuration, and arbitrary secrets inside conversation/tool content. Compaction must remain unblocked through every script-handled failure.

## Goals / Non-Goals

**Goals:**

- Make a best-effort local checkpoint attempt before manual/automatic compaction on both clients and at ordinary session exit where the client documents that hook, while preserving the last valid checkpoint on failure.
- Restore bounded structural evidence when the same client session compacts or resumes: repository identity, dirty paths, task/ledger pointers and line numbers, and transcript binding hashes.
- Avoid copying conversation, system, developer, or tool content into checkpoint state or reinjected context.
- Keep MCP, REST, Exomem CLI, and model calls outside the compaction critical path.
- Make concurrent delivery idempotent and stale-writer safe.
- Keep one checkpoint/storage/renderer implementation behind a versioned normalized event contract, with thin Claude Code and Codex CLI adapters.
- Install, migrate, verify, disable, and diagnose both clients' lifecycle hooks without damaging unrelated hook configuration.

**Non-Goals:**

- Reimplement client compaction, infer an objective/next action, or inspect/decrypt the compaction summary.
- Persist any conversation excerpt, tool payload, system/developer prompt, environment value, git diff, or credential content.
- Automatically claim that an Exomem note was captured or maintain a durable-capture queue/acknowledgement protocol.
- Guarantee a new checkpoint after OS kill, client-enforced hook timeout, storage failure, or loss of both local client state and repository artifacts.
- Claim a universal industry hook standard or advertise an untested client. Additional clients require an explicit envelope/config adapter and contract tests.
- Manufacture cross-client parity by mapping a semantically different hot-path event such as per-turn `Stop` onto a missing lifecycle event.

## Decisions

### 1. Store structural evidence only

The version-1 checkpoint contains:

- event metadata: schema/checkpoint/client/session/optional-turn IDs, normalized trigger/source, model when supplied, observation time, and a hashed/basename workspace identity rather than a canonical absolute cwd;
- transcript binding: path relative to the resolved client home when possible (otherwise only a path hash), observed size/mtime-ns, and offset/length/SHA-256 for a bounded tail slice;
- workspace state: git root/worktree relative identity, branch/detached state, HEAD, and bounded dirty paths;
- continuation artifacts: repository-relative paths, size/mtime/hash, completed/incomplete checkbox counts, and bounded incomplete checkbox line numbers for an exact allowlist of plan/task files;
- degradation and truncation flags.

It contains no free-form transcript or artifact text. Nested tool history quoted inside a user message, assistant echoes, plaintext compact summaries, and secret values carried in those excluded bodies are therefore not copied by construction rather than probabilistic redaction. Structural identifiers such as branch names and repository-relative paths are intentionally present and may themselves be sensitive; they are bounded, kept in mode-restricted local state, injected only into the same bound client session, and never written to metadata logs. A future opt-in excerpt mode would require a separate privacy contract and is out of scope.

The transcript tail is read only to calculate a digest and is never stored or logged. The hook reads no JSONL record semantically, so malformed UTF-8, oversized lines, and format changes cannot inject content. Total checkpoint JSON is capped at 64 KiB; paths are capped at 512 UTF-8 bytes, dirty paths at 128, artifact records at 16, and incomplete line numbers at 64 per artifact. Every truncation is explicit.

### 2. Normalize lifecycle envelopes before touching state

The deployed script contains one core entry point and a small adapter registry. An adapter maps a raw command-hook payload into a versioned internal event containing only `client`, `event`, `session_id`, optional `turn_id`, `trigger` or `source`, `cwd`, `transcript_path`, and optional model. It accepts only pinned documented event/source combinations and explicit snake_case/camelCase aliases; unknown clients, events, or ambiguous payloads soft-fail before state access. Every other raw field is ignored, especially content-bearing fields such as Claude Code `PreCompact.custom_instructions`.

The core returns either silence or a client-neutral continuation string. The adapter emits the client-specific JSON envelope. Claude Code and Codex currently share the `hookSpecificOutput.hookEventName/additionalContext` shape, but that coincidence is tested at the adapter boundary rather than baked into storage or rendering. The same script is deployed per client with an explicit `--client` argument, so payload inference cannot cross client state.

Supporting another client means adding one adapter that proves raw-input normalization, lifecycle matcher/config generation, and output-envelope behavior. It does not copy or condition the checkpoint profiler, lock/storage layer, artifact policy, validator, or renderer.

### 3. Resolve client home and state paths once

Runtime and installer use one resolver per explicit client. `EXOMEM_HOOK_HOME` overrides both for isolated/test installs. Otherwise Codex uses `CODEX_HOME`, then `Path.home() / ".codex"`; Claude uses `CLAUDE_CONFIG_DIR`, then `Path.home() / ".claude"`. State defaults to:

`<resolved-home>/.cache/exomem-continuation/<client>/<safe-prefix>-<client-session-sha256-prefix>/`

The client namespace and hash suffix prevent collisions between clients or session IDs with the same sanitized prefix. State roots and files are created as 0700/0600 where supported. Filesystem access goes through a platform-safe handle adapter: POSIX uses directory-relative descriptors plus `O_NOFOLLOW`; Windows opens each component without following reparse points and verifies the resulting handle before use. A preliminary `lstat` alone is not considered sufficient. Existing symlinks, junctions/reparse points, or non-regular state files are rejected, including adversarial swaps exercised by platform race tests.

### 4. Serialize writers with an auto-released cross-process lock

Each session has one mode-restricted regular lock file opened through the safe-handle adapter. A platform lock adapter acquires a non-blocking OS advisory lock on its handle (`flock`-class semantics on POSIX and byte-range locking on Windows), retrying with short jitter for at most 500 ms. The operating system releases ownership when the handle closes or the process dies, so acquisition has no owner-metadata publication gap and no stale-lock reclamation protocol. Lock-file creation is an atomic, symlink-safe regular-file operation; inability to create/open/acquire it is a soft failure that preserves prior state. Kill-at-every-stage multiprocess tests prove that a dead owner cannot permanently wedge the session.

Under the lock, the writer:

1. canonicalizes all non-volatile checkpoint evidence—normalized event identity, transcript binding, git/workspace state, artifact profiles, and degradation/truncation flags—without observation time or checkpoint ID, and hashes it as the structural-payload digest;
2. computes a checkpoint ID including schema version, client/session/optional-turn/event/trigger, transcript size/mtime/tail digest, and the structural-payload digest;
3. loads and validates `current.json`;
4. treats the same checkpoint ID as idempotent without rotating or duplicating history, while permitting a newer delivery to atomically refresh current observation/order/freshness fields under that same ID;
5. compares the lexicographic event-order tuple `(transcript_mtime_ns, transcript_size, observed_at_ns, checkpoint_id)` recorded at hook entry (with fixed sentinels for unavailable transcript fields) and refuses a stale writer that would replace a newer checkpoint;
6. writes with a unique same-directory temporary name, restrictive mode, flush/fsync where supported, and `os.replace`;
7. retains at most `current.json` and `previous.json`, cleaning abandoned temporary files while holding the lock.

Rotation across two filenames is not claimed to be transactional. If interruption occurs between rotation and current replacement, the loader may recover a valid `previous.json`. A later same-ID delivery promotes or refreshes that generation back to a single current generation without duplicate history, while a different older delivery treats the validated previous generation as its ordering floor. No partial file is accepted as valid. True concurrent same-ID and different-ID processes are part of acceptance testing.

### 5. Discover continuation artifacts through a closed repository policy

The hook never recursively scans arbitrary directories or follows symlinks. From a validated git root it considers only regular files matching:

- `.superpowers/sdd/progress.md`
- `.task/TASK.md`
- `.task/RESULT.md`
- `openspec/changes/*/tasks.md`

Candidates are bounded before reading. Dirty matching OpenSpec task files come first; remaining files with incomplete checkboxes are ordered by mtime-ns descending then relative path, capped at eight OpenSpec files and sixteen artifacts total. Each read is capped at 256 KiB. The checkpoint stores hashes, counts, and checkbox line numbers—not headings or task text. Paths must resolve beneath the git root and must not be symlinks.

### 6. Bind and validate before reinjection

`SessionStart(source="compact"|"resume")` selects `current.json`, or a valid `previous.json` fallback when current is missing/corrupt. A candidate must match the exact client and session ID, checkpoint schema, state-root binding, and—when the current transcript exists—its path binding plus the saved historical tail slice. Validation is append-safe: the current file may be larger and newer after compaction, but it must still contain the saved bytes at the saved offset/length and cannot be shorter than the observed pre-compaction size. Truncation and changes overlapping the saved slice are detected. The bounded design does not claim detection of an in-place rewrite outside that slice. The candidate must be no older than the 30-day retention/freshness window. Previous fallback is explicitly labeled `rollback` in injected context.

The hook renders at most 4096 UTF-8 bytes of `additionalContext` (the JSON envelope may be larger). The digest includes only structural fields, repository-relative paths, task line numbers, truncation/degradation flags, and checkpoint ID. It tells the agent to reconcile those pointers with the client's compacted context, reopen the cited artifacts, continue from evidence, and use normal Exomem governance only if a durable stepping-stone should be captured. This is advisory routing, not an idempotent capture queue and not proof of a successful write.

Missing, stale, foreign-session, over-bound, symlinked, or otherwise invalid checkpoints yield no injected context. Repeated process resumes may reinject the same checkpoint because recovery is the point; no capture-completion state is inferred.

### 7. Make installation adapter-driven and fail closed

The installer adds the shared checkpoint script for Claude Code and/or Codex CLI, passing an explicit client argument. Both target adapters register `PreCompact` (`manual|auto`) and `SessionStart` (`compact|resume`); only the Claude adapter registers unfiltered `SessionEnd`. The supported-event matrix is version-pinned, and Codex `SessionEnd` remains disabled until a named official Codex version documents and emits it. `--client all` runs the two client installers independently and reports per-client results; one client's invalid config does not authorize replacing or truncating it, and successful writes to the other client remain explicitly reported rather than rolled back implicitly. A partial all-client result exits non-zero. Because the existing CLI has only singular `--hook-dir`/`--settings` overrides, combining either with `--client all` is rejected rather than pointing both adapters at one caller-supplied config path; per-client environment homes remain supported.

Claude uses its effective config home (`CLAUDE_CONFIG_DIR` or `~/.claude`) and `settings.json` command-hook structure. Codex uses its effective config home (`CODEX_HOME` or `~/.codex`), `hooks.json`, and Windows command override. Existing capture/retrieve installation behavior for each client remains intact. A closed allowlist of exact current/legacy script basenames identifies candidate checkpoint entries; current entries must also carry the exact client argument, while explicitly named legacy entries may omit it. Broad substring matching is forbidden. Re-running removes only matching entries in each adapter's target events, inserts exactly one current entry per supported target event, and preserves unrelated groups/commands in order—including unrelated `SessionEnd` entries in Codex config.

Hook-config migration reads JSON fail-closed: malformed or non-object configuration raises without writing. The installer retains the original bytes, identity/stat, and digest; immediately before replacement it revalidates source identity and digest, then retries from the new source a bounded number of times or fails without replacement when concurrent drift persists. This detects observed edits by users, clients, or sync tools during read/modify/write; it does not claim transactional exclusion of a non-cooperating writer after the final validation. A real change preserves file mode, creates a uniquely named timestamp-plus-random backup, and uses a same-directory atomic replacement. A normalized no-op creates no backup and performs no rewrite. Defaults honor the client resolver and explicit CLI overrides. `install-hook --check` validates client-specific event matchers, hashes, command variants, legacy entries, and runtime state; absence before the first supported write event is a warning.

Rollback is documented as manual removal of the adapter's exact hook groups (three for Claude, two for Codex) plus the deployed checkpoint script; this change does not add an uninstall CLI.

### 8. Observe failures without blocking compaction

The script catches all in-process exceptions, exits zero, and writes no stdout for `PreCompact` or supported `SessionEnd`. `SessionStart` emits context only after validation. Metadata-only logs record event, checkpoint ID, status, duration, and error class; never transcript bytes, artifact content, environment values, or absolute paths.

Hook timeout is five seconds while implementation targets sub-second execution and gives each git subprocess a short timeout. Client kill/timeout cannot be caught and therefore is not covered by the soft-fail guarantee; the prior valid checkpoint remains the recovery floor.

Retention defaults to 30 days from checkpoint observation time. Pruning walks the state root through a persisted, bounded directory cursor with deadline checks so large or hostile roots cannot consume the root-lock budget before candidate work and repeated runs still reach every entry. It validates an expired non-current session while excluding its writers, then atomically renames the entire session directory to a unique tombstone beneath the same client state root before releasing that exclusion. Canonical-path writers may then create fresh state without touching the tombstone. The pruner deletes only a bounded, fully enumerated validated tombstone, skips symlinks/reparse points and the current session, and never follows paths outside the checkpoint root. Recognized expired pending manifests and abandoned pending temporary files are cleaned under the same bounded root scan; temporary contents never authorize deletion of a session directory. On platforms that cannot rename a directory while its session lock handle is open, every creator/writer and pruner participates in a short root-coordination lock with fixed order `root -> session`: writers hold root only through safe session-directory/lock-handle acquisition, while a pruner holds root until the expired directory is renamed. This prevents canonical-path recreation from racing the close-and-rename fallback without serializing the full checkpoint operation. Multiprocess prune-versus-writer tests gate both paths.

## Risks / Trade-offs

- **Structural evidence is less descriptive than copied conversation text** → Each client already provides semantic compacted context; the checkpoint adds independent file/state pointers without duplicating free-form content.
- **Structural names can themselves be sensitive** → They are required for useful recovery, bounded, locally permissioned, same-session-bound, and excluded from metadata logs; the design promises content exclusion, not that user-chosen filenames are semantically secret-free.
- **Transcript representation changes** → Only file provenance is hashed; no record parsing exists.
- **Lock owner dies** → The OS releases the advisory lock with the process handle; kill-stage tests prove the next writer can acquire it, while failure to lock remains non-destructive.
- **Two-file rotation is not transactional** → Loader validates current and falls back to a labeled previous generation.
- **Resume finds old state** → Exact client/session/path/historical-slice binding and 30-day freshness gate prevent foreign/expired injection; bounded binding deliberately does not detect unrelated in-place transcript rewrites outside the saved slice.
- **Client hook contracts drift** → Raw envelopes/config are isolated behind contract-tested adapters; unsupported shapes fail before state access, and Codex live E2E plus both adapter suites gate release.
- **Hook-config migration damages user config** → Malformed input fails closed; valid writes are backed up, mode-preserving, narrow, and atomic.
- **Automatic governed capture is not guaranteed** → Deliberate: semantic capture remains agent-owned and existing `Stop` behavior is the safety net.

## Migration Plan

1. Ship the script and installer support without changing already-running sessions.
2. Users re-run `exomem install-hook --client claude`, `--client codex`, or `--client all`; unrelated hooks remain untouched and changed valid configuration is backed up per client.
3. Each installed client reloads/restarts as its hook runtime requires.
4. The first supported write event creates runtime state; pre-first-run absence is healthy with a warning.
5. Rollback manually removes the adapter's exact groups and script. Checkpoint state remains inert and may be manually deleted.

## Open Questions

None. The design intentionally chooses private structural recovery evidence plus advisory governed capture, one shared core, and explicit client adapters rather than pretending the surrounding hook configuration is universal.
