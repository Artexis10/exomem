## Why

A direct read refuses a missing page at once. It refuses a withheld page only after it reads and decides the page, and a read that asks for links or history first walks the vault and the edit log. The refusal text is already identical, but the completion time is not. A caller from whom anything can be withheld can therefore time a read by path, by memory reference or by unit reference, and learn that a withheld page exists.

Two costs outside the read widened the gap. The MCP surface logged each refusal with its full traceback, and a withheld refusal and a missing one raise exception chains of different depth, so their tracebacks took different times to render. The command-surface change that answers every coded refusal with the error envelope has since removed that cost, because FastMCP no longer sees the refusal as an exception. The reference gate that cleans every error text rebuilt a census of all vault files, and one pattern of all their names, for every refusal, so refusal work grew with the vault and ran for seconds on a large one.

`stable-memory-references` already says that reference resolution bounds its work but does not make the read that follows take a fixed time. This change gives the refusals of a restricted caller one fixed completion floor, states it in `get-payload-shape`, and makes the work inside the floor independent of vault size.

## What Changes

- `projection_timing` holds one repository constant, `RESTRICTED_REFUSAL_FLOOR_MS = 55`, and a pad-only helper. The helper waits until the floor has passed and never refuses. Its last 2 ms spin, so every wait ends on time. There is no environment or caller override.
- The command registry marks the direct reads with a `restricted_refusal_floor` attribute: `read_memory`, and its `get` and `fetch` routes. The dispatcher reads the attribute. It holds no list of command names.
- `writer_lease.invoke_command` starts a clock at dispatcher entry for a marked command. When the command raises, and only then, the dispatcher asks `egress.caller_restricted`. For a restricted caller it frees the finished frames of the refusal and raises the same exception after the floor.
- The MCP surface and the REST handler wait out the floor themselves, after their own handling of the refusal. They wait with an `await` and finish the last 3 ms on a worker thread, because the event loop wakes up to 2 ms late by an amount that would follow the refusal's work time. The MCP surface does this in an outermost middleware, so FastMCP's error handling and logging, the call trace and the authorization re-check all happen inside the floor. The dispatcher's own thread waits only when no surface does, as on the CLI.
- The owner-only call span `command.refusal_floor` records the refusal's work time, an `overrun` flag and a `deferred` flag, and carries no path, title or reference.
- A refusal that ran past the floor completes late with its original refusal. Nothing is refused because of time.
- Successes return at once, L1 to L4 notices included. The refusals of an owner without a connector ceiling are not padded.
- For a restricted caller, `op_get` asks the release gate for its decision right after the page read. `egress.page_withheld_entirely` runs the body of `annotate_page` and stops after the withheld decision. A page that is withheld entirely then refuses before history, links, the query log and the heat check. The refusal text is the existing missing-path text, and the disclosure receipt is the one that the gate itself records. The owner keeps the existing order.
- A direct read takes all its release decisions under the policy of its first policy load.
- The reference gate keeps one census of the vault's files, with its alias tables and its literal-alias index, per vault, in the new module `governance/artifact_census`. It lists again only the directories whose stamp changed, and it rebuilds the tables only when a file name was added, removed or renamed. Set lookups over the text's candidate spans replace the pattern of all aliases, with the same matches.
- `stable-memory-references`: the last sentence of "Disclosure-neutral reference resolution" now refers to the new floor requirement.
- `docs/observability.md` describes the new span and the second `read.release` span of a restricted read.

## Not Claimed

- A page larger than the governed capacity of 1 MiB per item can refuse after the floor.
- A refusal whose own work runs past the floor completes late, and the delay is visible. The `overrun` flag counts these refusals for the owner.
- The first refusal after a file name changed rebuilds the gate's census, and that work grows with the vault's file count. Such a refusal can complete after the floor. The rebuild does not depend on the page that the refusal concerns.
- The floor was calibrated on one developer machine under load, not on the release runner profile.
- In-process differences of a few microseconds remain in the work that follows the wait.

## Out Of Scope

- Other surfaces with the same pattern: `read_media`, `query_dataset`, `browse_memory` by path, `list_inbound_links`, `review_item_context` by reference, memory-context seeds, the write doors, and `/download`, which does not pass through the dispatcher.
- A path-only decision for pages larger than 1 MiB.
- The command-name set that `_fixed_projected_command_completion` and `projection_timing.request_class_for_command` both hold for `find` and `ask_memory`.

## Impact

- Code: `src/exomem/governance/projection_timing.py`, `src/exomem/governance/egress.py`, `src/exomem/governance/artifact_census.py`, `src/exomem/governance/policy.py`, `src/exomem/command_surface.py`, `src/exomem/commands.py`, `src/exomem/writer_lease.py`, `src/exomem/server.py`, `src/exomem/server_rest.py`.
- Owner-visible behaviour: a refused read of a withheld page by a restricted caller no longer writes a `get` row to the query log. A refused read of a missing page never did. A coded refusal now leaves one log line instead of a traceback. Every refused or failed call stops paying a census of the whole vault in the reference gate.
- Cost: an MCP or REST restricted refusal waits up to 55 ms, on the event loop except for its last 3 ms, which hold a worker thread. A CLI refusal waits on its thread. A restricted read that is not refused takes one more release decision. An owner read takes one restriction check, which in file mode returns before any policy work. The gate's census stays in memory, about 40 MiB for a vault of 15,000 files.
- Specs: `get-payload-shape` gains one requirement. `stable-memory-references` changes one sentence and keeps every scenario.
- Docs: `docs/observability.md`.
- Tests: `tests/test_restricted_refusal_floor.py`; `tests/test_governance_postfilter.py` covers a page added after the census; `tests/test_call_spans.py` pins the new span name to its documentation.
