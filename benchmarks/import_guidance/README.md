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

This suite asks whether a session that settled something durable about an existing page updates that page before or with its recap, and whether the agent still writes nothing when nothing durable happened or the person or policy forbids a write. It compares two checkouts (arms) on the same turns in `scenarios/session-close/`.

The scripted harness serves recorded replies to one prompt, so this suite adds two modules instead of using it:

- `session_close.py` seeds a vault with the arm's own `init_vault` and the case's pages (`session_seed.py`, run in the arm's environment), starts the arm's real stdio server, and runs one `claude -p --input-format stream-json` process. It sends the case's user turns one after another, so the server's caches live across the session. A run ends with a snapshot diff of the knowledge base.
- `session_score.py` reads the answer key, runs the deterministic checks and the blind judge.

A run holds raw observations only. It never reads the key.

Layout:

- `scenarios/session-close/c0N-*.json`: `id`, `title`, `suite`, `seed`, `engagement` (the `EXOMEM_PROMINENCE` level), `turns` and the `answer_key` slot. `session_of` takes the seed and turns of another case (c06 is c01 at `light`).
- `scenarios/session-close/seeds/<seed>/`: the seeded pages, with paths relative to the knowledge base. They are invented data with fixed `exomem_id` values.

The answer key is `null` until its writer fills it. `session-score` refuses to score while any case in the runs has no key, and refuses a key that names a page outside the seed. The shape (fields only, no values) is in the `session_score.py` docstring.

Run it:

1. Run `python -m benchmarks.import_guidance run --suite session-close --product-root baseline=<checkout> --product-root candidate=<checkout> --model sonnet --repeats 2 --out <dir outside every repository>` from the repository root, with `PYTHONPATH` unset. Add `--dry-run` to seed each case on each arm and start no agent. Add `--scenario c01` to run one case.
2. Run `python -m benchmarks.import_guidance session-score <out>/runs.jsonl --baseline baseline --candidate candidate --out <score dir>` to get `scores.jsonl` and `report.md`.

How a run is made:

- The server runs with `PYTHONPATH=<arm>/src`, a state tree under the run folder, embeddings off and `EXOMEM_PROMINENCE` set to the case's engagement. The agent reads the arm's `SKILL.md` and `references/engagement.md` as its system prompt and has the server's tools only.
- The arm's own Stop and UserPromptSubmit hooks (from its `plugins/claude-code/hooks/hooks.json`) run with `EXOMEM_RETRIEVE_INJECT=mcp` and a hook home under the run folder.
- The first turn waits 15 seconds so the index warm (`RETRIEVAL_INDEX_WARMING`) ends. A row counts the warming replies that remain.
- The row holds the pages changed (as diffs), the pages created, each recap with the pages it links (`seeded`, `created`, `dangling`, `ambiguous`), where the last write of each changed page falls against the recap's write (`order`), the `compile` block of each `record_episode` reply, the hook counts and the cost.

Judge: it reads the person's turns, the key's `reference` and the page diffs, and answers J1 (the changes are correct) and J2 (the changes are appropriately scoped). It never sees the arm, the model, the recap or a tool reply.

Limits:

- The agent has no shell, so the Stop hook's episode check never fires, because it asks only after a landing. The suite measures the hookless close.
- The agent has `references/engagement.md` in its prompt, where a real agent would read it on demand.
- `order` uses the last write of a page. A page written twice shows its later write.
- Sonnet is the agent. The judge is Opus.
