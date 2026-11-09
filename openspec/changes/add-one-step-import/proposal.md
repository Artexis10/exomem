# Add one-step import

## Why

An agent that files a staged folder today drives `adoption_studio` through `start`, `select`, `plan`, `apply` and `finish`, and repeats them for each page of a large folder. It threads run ids, plan ids and three kinds of continuation between calls. A weaker agent loses the thread, stops early or files a folder twice. The `exomem import` and `exomem cloud import` commands stage a folder, then leave the agent to find the rest.

The intended experience is one repeatable call. The agent names the staged folder, repeats the call it gets back, and reads one sentence about where the import stands.

## What changes

- **A `file` action on `adoption_studio`, public as `import_memory` kind `file`.** It takes the staged folder `_Imports/<id>`, an optional `collection` and an optional `continuation`. It runs the existing lifecycle steps for the newest unfinished page, as far as one call affords, across every page of the folder. It adds no new way to write: each step is the function that the step path already calls.
- **Compact reply.** `run_id`, `filing` (`working`, `stopped` or `done`), `continuation`, `progress`, `summary`, `stop`, `person` and `filed`. The agent repeats the call until `filing` is not `working`.
- **Crash-safe recovery.** A `file` call that holds an apply invocation reconciles under its original identity, as an interrupted `apply` does. Both apply-only recovery gates in `writer_lease.py` accept `file`.
- **CLI.** `exomem cloud import` files each window as it lands, and at the end. A `--no-file` flag leaves the staged windows for the agent, and `--collection` names the collection. A cell without `file` falls back to the step path.
- **Guidance.** The import reference, the operations reference, the `exomem-ingest` skill and the next-action lines of `exomem import` name `import_memory` `file`.

## Capabilities

### Modified Capabilities

- `adoption-studio`: one call files a staged folder.
- `retry-safe-mutations`: an interrupted `file` call that holds an apply invocation reconciles under its original identity.
- `cloud-vault-import`: the CLI files each landed window.

## Impact

- **Code:** `adoption_run.py` (the `file` section), `commands.py`, `operation_entries.py`, `governance/egress.py`, `writer_lease.py` (two gates), `mutation_terminal.py` (compact keys), `local_import.py`, `cloud_import_windows.py`, `cloud_import_client.py`, the skill scaffold references, the Cloud rehearsal large-import scenario and the generated tool-schema, skill and plugin artifacts.
- **Tool surface:** `import_memory` gains one variant, 9% larger. The schema budget rises 650 B in total.
- **Out of scope:** the step path keeps its behavior, and `scale-agent-led-imports` stays as it is.
