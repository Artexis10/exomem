# Import guidance probe

This probe shows whether a real headless agent follows the Cloud import tool text when a scripted server returns the states that text describes. Scenario files in `scenarios/` script the replies, and `status_shape.py` builds every `read_import` status with the product's own window code. The `observe_transfer`, `transfer_memory` and run-step replies have no in-repo source and are unverified, and the probe serves four tools, so it tests the text, not tool selection. Deterministic checks read the call log and a judge reads the agent's words; a run error, an unconnected server or an unreadable judge answer is `unknown`. Run `PYTHONPATH=benchmarks python -m import_guidance run --model <m> [--judge-model <m>] --repeats N --scenario <id|all> --out <dir outside every repository>` from the repository root, or add `--dry-run` to start no model. To add a case, copy a scenario file, script its replies, list its `checks` and `criteria` with the values they expect, and run `--dry-run`; each failure found later becomes a permanent case.

## Write-narration suite

This suite shows whether a real agent tells the person what a save did, or leaks the tool's diagnostics (error codes, request ids, hashes, retries, sync state). It compares two checkouts, called arms, on the same user turns in `scenarios/write-narration/`. Run it with `--suite write-narration` and one `--product-root [LABEL=]PATH` for each arm.

1. Run `PYTHONPATH=benchmarks python -m import_guidance run --suite write-narration --product-root baseline=<checkout> --product-root candidate=<checkout> --model <m> --repeats N --out <dir outside every repository> --dry-run`. The dry run records each arm and starts no agent.
2. Remove `--dry-run` to run the agents. Run it again with another `--model` and the same `--out` to add a model.
3. Run `python -m import_guidance report <out>/runs.jsonl --baseline baseline --candidate candidate --out <report.md>` to get the leak rates, the difference and its 95% bootstrap interval.

How an arm's replies are made:

- `recording.py` starts `record_worker.py` with the arm's `src` first on `PYTHONPATH`. The worker makes the scenario's `record` calls through the arm's real MCP tools on a throwaway copy of the arm's fixture vault: bootstrap, validation, a refused commit and the committed save. `expect` fields make the recording fail when a scenario assumes a state it did not get.
- The scripted server serves those recorded replies (`$shape`), picks between them by the call's arguments (`$rules`), and passes the arm's `SERVER_INSTRUCTIONS` as the server instructions. The agent also reads the arm's `SKILL.md` as its system prompt and the arm's tool text.
- The recording is made once per arm and scenario, so the ids in it stay fixed for all runs of that arm. `answer_lacks_scripted_strings` looks for them, and for the field names in its `literals`, in the final answer.

Judge: the judge reads the person's turn, the scenario's `reference` (the true events, the same for both arms) and the agent's final answer. It never reads a tool reply, so it cannot tell the arms apart. Criteria R1 to R4 are in the scenario files.

Store-identity cases 07 to 09 ask whether the agent tells the person which store took a write:

- 07 runs the arm's code as Exomem Cloud (`"mode": "cloud"` in `record`) while the person asks for their personal Exomem. The judge's R5 passes when the agent says nothing was saved because the personal Exomem is not connected, or says plainly that the fact went to Exomem Cloud. Whether the agent wrote is read from the call log (R6) and passes or fails nothing.
- 08 is an ordinary save on the Cloud-mode arm and 09 a save to the personal Exomem on the personal-mode arm. R7 needs a committed save (the `write_committed` check) and no refusal or request to choose a store.
- These three serve the session-profile bootstrap that the skill tells an agent to request, because the compact reply carries no store identity. The scripted server does not check the digest the agent passes.
- `scenario.outcome_facts` adds one sentence to the judge's reference, chosen by whether the call log holds a committed save, so the judge knows what happened without seeing a tool reply.
- Run `python -m import_guidance store-report <out>/runs.jsonl --baseline baseline --candidate candidate --out <report.md>` for the pass counts, the write rate and the paired intervals.

Limits:

- The first commit in the two review scenarios is refused whatever arguments the agent sends. The real product would accept a commit that carries the hash.
- The agent has the tools a scenario serves, not the whole product surface, and the compact bootstrap reply stands in for the session profile.
- The judge model is the same family as the agents.

## Session-close suite

This suite tests whether a capture session saves durable facts in their existing canonical pages and preserves unrelated knowledge. It also tests restraint, recap production and citation adherence. Every enabled recall probe then asks a fresh actor what the canonical pages say. Capture success never selects which recall probes run.

`session_close.py` drives real actors against each arm's real stdio server. `session_seed.py` initializes an isolated vault with that arm's scaffold and the same invented seed pages. `session_score.py` reads independent keys only after generation. It exports blind views for a native Codex evaluator and imports structured judgments. Generation starts no judge.

Layout:

- `scenarios/session-close/cNN-*.json` contains only inputs: `id`, `title`, `suite`, `seed`, `engagement`, `turns` and optional `recall_turn`.
- `session_of` inherits the seed and capture turns. Each case enables its own recall question explicitly.
- `scenarios/session-close/seeds/<seed>/` contains invented canonical pages with fixed memory IDs.
- `scenarios/session-close/answer-keys/<case-id>.json` contains the independent scorer-only object. A missing, null or malformed key refuses scoring.
- Each key has `reference`, `must_change`, `must_not_change`, `recap` and `new_pages`. The scorer rejects page paths outside the seed. `new_pages: none` still permits the recap Source.

`--client-mode plugin` is the default. It appends the arm's skill and installs its Stop and UserPromptSubmit hooks. Omitting `--hook-turn` keeps those hooks; that flag adds a separate synthetic close ask. `--client-mode tool-only` keeps the same strict real MCP server, stages no plugin hooks, and appends no skill. It requests `disableAllHooks: true` in the supplied settings file. Combining tool-only with `--hook-turn` is rejected before launch.

The [official hook setting](https://code.claude.com/docs/en/settings-reference#disableallhooks) does not override managed hooks or force-enabled managed plugin hooks. Settings record the requested configuration. A complete connected tool-only trace must report zero observed hook starts and completed invocations. Observed hooks or unavailable observations leave no-hook acceptance unknown; capture and fresh counts remain separate. Raw evidence stays available, and the harness does not retry automatically. Real-model evidence must establish the observed client behavior.

Capture and fresh rows retain `client_mode`, the actual argv, supplied settings, hook starts and completed invocation counts. Work directories include the mode. Native identity, scored rows, denominators and paired intervals keep modes separate; historical rows without a mode mean plugin. Blind judge views expose neither mode nor actor identity.

The key writer supplies the references and recall questions independently. Generation's `Case` object contains no key. The actor receives neither reference text nor scorer files. Cases c01, c02, c03, c07, c08, c09, c10 and c11 have fixed input recall probes. Case c11 combines a compiled-note update and an entity update in one session.

For external cases, pass the same `--case-dir` to `run --suite session-close` and each `session-score` invocation. Cases keep the existing format and resolve seed identifiers against the shipped seeds. Scoring reads `<case-dir>/answer-keys/<id>.json`; actor staging receives neither the case directory nor its keys. Omit the option to use shipped cases.

1. Select both checkouts with `--product-root`, the actor with `--model`, and the number of repeats with `--repeats`.
2. Run the existing `run` command with `--suite session-close` and an output directory outside every repository to capture raw runs.
3. Add `--dry-run` to seed and validate without starting actors, or `--hook-turn` to append the arm's own close prompt.
4. Run `session-score` on `runs.jsonl` with both arm labels and `--out` to prepare the native judge files.
5. Give only `judge-views.jsonl` to the native Codex evaluator; keep `private/judge-map.json` outside its packet.
6. Run `session-score` with the same `--out` and `--judgments` to import the returned judgments and write the report.

The default preparation, also selected by `--prepare-judge`, starts no model. Each shuffled item contains an opaque `id`, its blind `prompt` and `answer_schema`. The evaluator returns JSONL objects with that `id` and `structured_output` matching the supplied schema. The scorer reuses the existing rubric and boolean-outcome parser. The separate private map binds each ID to its run, stage and exact prompt, including the reference and rubric. Preparation omits capture items whose outcomes are already deterministic because no material page changed. Changed evidence, missing results, malformed results and duplicate IDs leave semantic judgments unknown. The reader never starts a replacement judge. Deterministic failures and transport-unknown probes remain in the report, even when they need no semantic judgment.

`--automatic-judge` explicitly selects the legacy Claude CLI path; `--judge-model` applies only there. The native path never discovers or starts Claude.

Only run actors or judging under an agreed evaluation protocol and budget. The no-model checks establish harness behavior; they do not establish a model improvement.

Each capture actor uses strict MCP configuration, no shell tools and no session persistence. Both client modes keep the empty isolated working directory and the project-only setting-source floor. The server imports the arm's source and keeps its vault, state, logs and caches in the run directory. The hooks use that actor's isolated hook home. The initial capture actor retains its context across the scripted turns.

After capture exits, the harness retains its before/after snapshots, page diff, replies, session identity and paired tool requests and replies. It also retains raw stream, connectivity, errors, usage, cost, hook counts and compile diagnostics. Capture's `observed` field always comes from these capture snapshots.

If `recall_turn` exists, the harness launches a new actor and stdio server against the same vault and external state. It never reseeds. The fresh actor gets one question, a fresh working directory and hook home, and no capture transcript, resume token or summary. Its raw stream, identity, question, answer, paired requests and replies, connectivity, errors, usage and cost live under `fresh`. Fresh before/after snapshots expose any probe writes separately. A failed probe leaves capture evidence intact and produces an unknown recall result.

The deterministic checks retain required page changes, forbidden changes, recap adherence, citation adherence and extra-page restraint. Citation adherence applies only when a recap exists and a compiled page changed. It does not excuse missing entity work.

The capture judge reads the turns, independent reference, canonical page diffs and final required pages. J1 checks factual correctness. J2 checks justified scope and preservation of unrelated knowledge, permitting semantic-unit representation required by the product. J3 checks completeness against every material reference fact and constraint. Missing required page work fails J3 even when the shown edits and citations are correct. The blind view excludes arm, model and compile diagnostics.

Recall is scored separately. The scorer projects actual served text and its own path from the product's response schemas. It preserves lifecycle and truncation fields. The union of the independent key’s `must_change` and `must_not_change` paths defines the seeded canonical pages. The blind reference judge decides which retrieved content supports the answer. A recap cannot substitute for canonical content. R2 judges whether the retrieved canonical content, excerpts or units contain the relevant reference facts and support the answer. The judge must not assume unseen content.

Supported content:

- `read_memory`: page `body/path`; exact units with `status: found`, `unit.content/parent.path` and optional `parent_context.markdown`.
- `search_memory`: direct hit lists or `hits` envelopes; page `excerpt/path` and semantic-unit `content` or `excerpt` with `parent_path`.
- Deep search packs: `claims[path].lede/sections` and `semantic_units` entries with their own `parent.path` and unit excerpts.
- `recall_context` or `activate_context`: `units[].text/provenance.path`, `current_state[].statement/path` and `map.items[].state.statement/state.path`.

Anchors, enclosing map paths, titles and pointers do not supply canonical prose. Recognized empty or locator-only results fail. Failed transport and unrecognized structures remain unknown. The raw full tool replies stay in the run. The blind view omits arm, model, compile diagnostics and citation counts.

The report gives separate known, unknown and inapplicable denominators for canonical correctness, restraint, recap production by required/optional/forbidden policy, applicable citation adherence and every input-enabled recall probe. Canonical correctness covers cases that owe an update; restraint covers cases that owe no update. It averages paired repeats and models within each case before it bootstraps cases. A 95% interval that includes zero means no change. For recap production, a reported success means a recap exists; production on forbidden cases is unwanted.

Limits:

- The first actor waits for initial index warming using the existing driver behavior. The no-model stdio integration follows the product's retry hints instead of adding a sleep.
- The no-model integration records a recap, commits a governed canonical edit, closes the first client/server, then finds and reads that edit through a new client/server.
- Some legacy seed notes lack semantic units. Current governed edits must add a semantic unit; the integration does so without changing those seed fixtures.
- Entity updates carry no compiled-note citation claim. Cases c02, c09, c10 and c11 exercise compiled-note citation adherence.
- `order` records the page's final write time, so it cannot reconstruct earlier writes to that page.
- The harness stages the engagement reference in the system prompt, where an ordinary actor may read it on demand.
- The shell-free actor cannot trigger the landing-based Stop check. `--hook-turn` measures an explicit close prompt instead.
- Real-model capture, recall and independent judging have not run for this increment. Their behavior and comparative effects remain unverified.
