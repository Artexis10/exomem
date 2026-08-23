## Context

The north-star acceptance test for the lifecycle-routing work is a replay: take an episode of ordinary work, remove every utterance that names the store or the act of storing, run it through a real agent on the shipped contract, and compare the durable state it leaves against the state an expert session left. Slice 1 gave that comparison its three ingredients — a generic fixture vault, a projector that exposes structured collections, and a deterministic expectation — and left the family registration to this amendment. Three existing pieces shape the design: f26 (a journey that discovers the real envelope and projects only what it observed), track C (`claude -p` invocation builder, stream-json transcript parser, harness-fault semantics), and the sequence-2 governance (registered-but-withheld families, red fixtures for every refusal path).

## Decisions

### D1 — An epistemic family, not a membench track

f27 is a §1 family of kind `operational` (a journey), evaluated by deterministic assertions over a neutral snapshot, withheld until the sequence-3 receipt is acknowledged. It is not a membench track-D journey (those call no model by contract) and not an AT-1 dimension (those are judged). The agent is real and its output is not reproducible; the *evaluation* is — the persisted snapshot and transcripts replay through the evidence module with digest equality like any other family's evidence. The trajectory uses the existing vocabulary: `configure` (the arm), `agent_turn` (one per user utterance, `ref` = the corpus turn id), `snapshot`. f27 is not an unprompted family: the user turns are the stimulus, so `UNPROMPTED_FAMILIES` is unchanged.

### D2 — The expert end-state is authored, never produced by an agent

The corpus (`benchmarks/epistemic/corpora/lifecycle_replay.py`) is a pure function of its arguments: a seeded vault (the slice-1 Planning and Records manifests, parent chain, join on `title`, no items), an ordered transcript of user turns, and per-turn annotations of the consequences an expert lands, in three tiers — `intent` (a plan item is filed from stated intent), `outcome` (a record is appended from an observed event), `transition` (an open item changes status because of an outcome). The expected end-state is the fold of the annotations. Turns that land nothing are part of the corpus on purpose: a tentative claim ("I think the third one might be fine, not sure yet"), an elapsed-time remark ("it's been a week since I touched the fifth"), and a deferral ("I'll do the last two next time") each annotate `none`, because the slice-1 contract says a tentative claim is never an event and elapsed time is never an outcome. The corpus vocabulary is generic — a batch-production workstream of deliverables with events `produced | approved | delivered | published | rejected | redo-needed` — and the scaffold no-leak rule applies to it. Deliverables are referenced by their seeded titles; the comparator matches titles after NFKC normalisation, case folding and whitespace collapse and nothing looser. A miss is a miss.

Records items are keyed `[occurred_on, title, event_type]` by the slice-1 manifest. The agent chooses `occurred_on`; the expectation therefore matches a record on `(title, event_type)` and requires `occurred_on` to be present and a valid date, comparing its value only when the utterance states one. The corpus avoids two expected events with the same `(title, event_type)` so the date can never be the discriminator.

### D3 — The driver runs the real agent in the thinnest honest isolation

`benchmarks/epistemic/journeys/f27_replay.py` discovers the `claude` executable and records `claude --version` (refusing with `EnvelopeNotDiscovered` when absent — a library fallback would turn an agent test into a library test). The invocation floor for every turn:

- the process environment with every `CLAUDECODE` / `CLAUDE_CODE_*` / `CLAUDE_PID` variable removed — inside a Claude Code session those variables make the child report "Not logged in";
- `--setting-sources project` with a benchmark-owned project directory as cwd — verified to exclude the user's settings, hooks, MCP servers and `~/.claude/CLAUDE.md` while keeping the subscription login (`--bare` is not usable: it restricts auth to an API key, and this programme is subscription-only);
- `--strict-mcp-config --mcp-config <cfg>` written by track C's `write_mcp_config` — an exomem stdio server on the deterministic lexical profile with vault, config, leases and logs under the benchmark workdir;
- `--output-format stream-json --verbose --include-hook-events`, `--allowedTools` restricted to the exomem server, `--max-turns` bounded per utterance, `--model` pinned and recorded;
- `--session-id <uuid>` on the first turn and `--resume <uuid>` on every later one, so the episode is one conversation.

Two arms, each on a fresh copy of the seeded vault. **Hookless**: no plugin, built-in tools disabled, the documented custom-instructions block for hookless clients appended as system prompt (`docs/prominence.md`, the `maximal` block, cited by line), prominence `maximal` set through the arm's config file before the first turn. **Hooked**: `--plugin-dir plugins/claude-code` (the shipped skill and hooks exactly as a plugin user receives them), the `Skill` built-in enabled, hook state under the workdir's `EXOMEM_HOOK_HOME`, prominence `balanced`. Each arm's prominence is the product's own default for that surface; the harness configures nothing the product would not.

The runner is injectable (track C's `Runner` protocol). Tests drive it with recorded transcripts; the live path shells out. A `--dry-run` prints the complete argv per turn without executing — a script that cannot run is not evidence, and the argv is checked against the installed CLI's declared options.

### D4 — Harness faults are reported, never scored

A non-zero exit, an error-subtype or `is_error` result, a "Not logged in" result, or a malformed stream-json line marks the arm a harness fault (track C semantics). No snapshot is produced for that arm; the phase's assertions evaluate `blocked` with the fault reason. A harness fault is structurally unscorable — invalid-run semantics, never a product failure.

### D5 — The store-bearing-utterance gate is a loader rule

`STORE_BEARING_RE` in the corpus module pins the vocabulary: the store (`exomem`, `kb`, `knowledge base`, `planning`, `plan item`, `record`, `records`) and the act of storing (`save`, `store`, `track`, `remember`, `capture`, `log it/this/that`, `note it/this/that/down`, `write it/this/that down`, `file it/this/that`). `assert_no_store_bearing_utterance` runs at corpus construction and at scenario load, naming the turn and the match. The retrieve-nudge's `_KB_BEARING_RE` is the sibling, cited, and deliberately not reused: it matches `earlier`, `previous`, `history` and `decision`, which are exactly the ordinary language the family measures. A red fixture carrying "save this one" refuses at load.

### D6 — Two assertions, one pair, no aggregate

Both assertions gate on the `collections` section: a snapshot whose section is empty, or lacks a `planning` or a `records` collection, evaluates `blocked` — an unprojected section is an observation error, never a pass.

`lifecycle_consequence_landed_unprompted` takes the expected end-state from the scenario's expectation parameters (the corpus id; the assertion reads the fold, never the transcript). For each tier it counts landed over expected; it passes only when every tier is complete, and its detail carries the fractions and the missing keys.

`no_structured_write_beyond_expectation` computes the extras: plan items not in the expected set or with a status the fold did not assign, records not in the expected set, any collection beyond the two seeded, and any page outside the declared allowlist (`Knowledge Base/log.md`, `Knowledge Base/index.md`). It passes only when the extras set is empty and lists the extras otherwise.

Neither composes `signal_absence_checked_across_all_surfaces` — they are state assertions, not signal-absence assertions — and `COMPOSES_ABSENCE_META` is unchanged. `REQUIRES_ITEM_PAIR` is unchanged. Mechanism-removal tests exist for each branch: removing the tier loop, the blocked gate, the extras computation or the normalisation each turns a test red.

### D7 — Paired readouts and a pinned manifest

The run report (`report.json`) carries, per arm: coverage per tier beside the extras count from the same run; the count of capture-nudge firings (hook events in the stream); the count of `record_memory` / `plan_memory` write tool uses; token and duration accounting; and the harness-fault reason if any. The manifest pins CLI version, model, exomem version, prominence per arm, the corpus digest and the fixture digest. No number aggregates across tiers or arms; a coverage figure is never published without its extras dual. The house rule from sequence 2 applies unchanged.

### D8 — Expected partial, withheld, and the next slice's target

On today's runtime with the slice-1 contract the family is expected partial: the outcome and transition tiers are the ones slice 1 taught, the intent tier depends on the agent filing plan items from stated intent without being asked, and the hookless arm has only the tool surface and a pasted block to teach it. Whatever the development run shows is recorded in the tasks as the finding. The family is withheld from comparative runs, scores and claims until the founder acknowledges sequence 3; a development run is evidence about the harness and the current runtime, not a claim. The next contract slice is built against this family, and "became more automatic" means its tiers flip green while the extras stay at zero.

### D9 — What this amendment does not add

No budget constants (the tier counts are fixed by the corpus). No catastrophic assertions (a missed consequence is a trust failure). No new operation kinds. No change to the projector, the runtime or the tool surface. No witness join against the server write log — the projected vault is the state witness; the transcript's tool-use counts are reported beside it, not reconciled against it.

## Risks

- **Model drift.** The family's result varies with the model and the CLI release; both are pinned in the manifest and a run is comparable only to runs with the same pins.
- **Nudge interaction in print mode.** The Stop hook may block and extend a turn; `--max-turns` bounds it, and the nudge count is reported so a green tier reached only through repeated nudging is visible.
- **Title strictness.** The corpus references deliverables by seeded title; an agent that paraphrases a title misses. This is deliberate and documented; loosening it is a new amendment.
- **Subscription-only nesting.** Running the journey from inside a Claude Code session requires the session variables stripped; the driver does it, and the dry-run prints the exact environment delta.
