# Import guidance probe

This probe shows whether a real headless agent follows the Cloud import tool text when a scripted server returns the states that text describes. Scenario files in `scenarios/` script the replies, and `status_shape.py` builds every `read_import` status with the product's own window code. The `observe_transfer`, `transfer_memory` and run-step replies have no in-repo source and are unverified, and the probe serves four tools, so it tests the text, not tool selection. Deterministic checks read the call log and a judge reads the agent's words; a run error, an unconnected server or an unreadable judge answer is `unknown`. Run `PYTHONPATH=benchmarks python -m import_guidance run --model <m> [--judge-model <m>] --repeats N --scenario <id|all> --out <dir outside every repository>` from the repository root, or add `--dry-run` to start no model. To add a case, copy a scenario file, script its replies, list its `checks` and `criteria` with the values they expect, and run `--dry-run`; each failure found later becomes a permanent case.

## Write-narration suite

This suite shows whether a real agent tells the person what a save did, or leaks the tool's diagnostics (error codes, request ids, hashes, retries, sync state). It compares two checkouts, called arms, on the same six user turns in `scenarios/write-narration/`. Run it with `--suite write-narration` and one `--product-root [LABEL=]PATH` for each arm.

1. Run `PYTHONPATH=benchmarks python -m import_guidance run --suite write-narration --product-root baseline=<checkout> --product-root candidate=<checkout> --model <m> --repeats N --out <dir outside every repository> --dry-run`. The dry run records each arm and starts no agent.
2. Remove `--dry-run` to run the agents. Run it again with another `--model` and the same `--out` to add a model.
3. Run `python -m import_guidance report <out>/runs.jsonl --baseline baseline --candidate candidate --out <report.md>` to get the leak rates, the difference and its 95% bootstrap interval.

How an arm's replies are made:

- `recording.py` starts `record_worker.py` with the arm's `src` first on `PYTHONPATH`. The worker makes the scenario's `record` calls through the arm's real MCP tools on a throwaway copy of the arm's fixture vault: bootstrap, validation, a refused commit and the committed save. `expect` fields make the recording fail when a scenario assumes a state it did not get.
- The scripted server serves those recorded replies (`$shape`), picks between them by the call's arguments (`$rules`), and passes the arm's `SERVER_INSTRUCTIONS` as the server instructions. The agent also reads the arm's `SKILL.md` as its system prompt and the arm's tool text.
- The recording is made once per arm and scenario, so the ids in it stay fixed for all runs of that arm. `answer_lacks_scripted_strings` looks for them, and for the field names in its `literals`, in the final answer.

Judge: the judge reads the person's turn, the scenario's `reference` (the true events, the same for both arms) and the agent's final answer. It never reads a tool reply, so it cannot tell the arms apart. Criteria R1 to R4 are in the scenario files.

Limits:

- The first commit in the two review scenarios is refused whatever arguments the agent sends. The real product would accept a commit that carries the hash.
- The agent has the tools a scenario serves, not the whole product surface, and the compact bootstrap reply stands in for the session profile.
- The judge model is the same family as the agents.
