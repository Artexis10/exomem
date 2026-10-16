## Why

A direct read refuses a missing page at once. It refuses a withheld page only after it reads and decides the page, and a read that asks for links or history first walks the vault and the edit log. The refusal text is already identical, but the completion time is not. A caller from whom anything can be withheld can therefore time a read by path, by memory reference or by unit reference, and learn that a withheld page exists.

`stable-memory-references` already says that reference resolution bounds its work but does not make the read that follows take a fixed time. This change gives the refusals of a restricted caller one fixed completion floor and states it in `get-payload-shape`.

## What Changes

- `projection_timing` holds one repository constant, `RESTRICTED_REFUSAL_FLOOR_MS = 50`, and a pad-only helper. The helper waits until the floor has passed and never refuses. There is no environment or caller override.
- The command registry marks the direct reads with a `restricted_refusal_floor` attribute: `read_memory`, and its `get` and `fetch` routes. The dispatcher reads the attribute. It holds no list of command names.
- `writer_lease.invoke_command` starts a clock at dispatcher entry for a marked command. When the command raises, and only then, the dispatcher asks `egress.caller_restricted`. For a restricted caller it frees the finished frames of the refusal, waits until the floor has passed, and raises the same exception. The owner-only call span `command.refusal_floor` records the wait and an `overrun` flag, and carries no path, title or reference.
- A refusal that ran past the floor completes late with its original refusal. Nothing is refused because of time.
- Successes return at once, L1 to L4 notices included. The refusals of an owner without a connector ceiling are not padded.
- For a restricted caller, `op_get` asks the release gate for its decision right after the page read. `egress.page_withheld_entirely` runs the body of `annotate_page` and stops after the withheld decision, before any rendering. A page that is withheld entirely then refuses before history, links, the query log and the heat signal. The refusal text is the existing missing-path text, and the disclosure receipt is the one that the gate itself records. The owner keeps the existing order.
- `stable-memory-references`: the last sentence of "Disclosure-neutral reference resolution" now refers to the new floor requirement.
- `docs/observability.md` describes the new span and the second `read.release` span of a restricted read.

## Not Claimed

- A page larger than the governed capacity of 1 MiB per item can refuse after the floor.
- A refusal whose own work runs past the floor completes late, and the delay is visible. The `overrun` flag counts these refusals for the owner.
- The 50 ms value is provisional. Its target is the smallest multiple of 5 ms that is at least twice the 99.9th percentile of restricted-refusal work on a withheld 1 MiB page, on the release runner profile. This change does not do that calibration.
- In-process differences of about ten microseconds remain in the work that follows the wait.

## Out Of Scope

- Other surfaces with the same pattern: `read_media`, `query_dataset`, `browse_memory` by path, `list_inbound_links`, `review_item_context` by reference, memory-context seeds, the write doors, and `/download`, which does not pass through the dispatcher.
- A path-only decision for pages larger than 1 MiB.
- The command-name set that `_fixed_projected_command_completion` and `projection_timing.request_class_for_command` both hold for `find` and `ask_memory`.

## Impact

- Code: `src/exomem/governance/projection_timing.py`, `src/exomem/governance/egress.py`, `src/exomem/command_surface.py`, `src/exomem/commands.py`, `src/exomem/writer_lease.py`.
- Owner-visible behaviour: a refused read of a withheld page by a restricted caller no longer writes a `get` row to the query log and no longer counts as a read for the heat signal. A refused read of a missing page never did either.
- Cost: a restricted refusal waits up to 50 ms on one worker thread. A restricted read that is not refused takes one more release decision and one restriction check. An owner read takes one restriction check, which returns before any policy work.
- Specs: `get-payload-shape` gains one requirement. `stable-memory-references` changes one sentence and keeps every scenario.
- Docs: `docs/observability.md`.
- Tests: `tests/test_restricted_refusal_floor.py`; `tests/test_call_spans.py` pins the new span name to its documentation.
