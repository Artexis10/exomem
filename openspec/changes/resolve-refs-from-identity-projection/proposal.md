## Why

A read by `exomem://memory/<uuid>` walks the whole Knowledge Base. Resolution reads and parses the frontmatter of every page on every call, so that the work a read performs does not depend on which references exist. On a vault of several thousand pages, that scan takes seconds and is almost all of the read.

The server already holds an in-process identity projection: the `exomem_id` of every canonical page, kept current by governed writes and by watcher events. The relation graph and the relation queue already read it. A reference read can read it too, and keep the disclosure property, because whether the projection is current is a fact about the vault, not about the reference.

The disclosure rule for this resolution lives today in code comments and in one planning sentence that claims constant work. This change states the rule once, in `stable-memory-references`, and makes the planning requirement refer to it.

## What Changes

- `memory_refs.paths_for_ids_read_only`, the one entry point for governed reference resolution, answers from the current identity projection when the server holds one. Otherwise it runs today's scan. Read by reference, batch reference visibility and plan-progress motivation all use this entry point.
- Vault-wide state alone chooses the source: the projection must be current before the lookup and still current after it. The requested references, their existence and the reader's permissions never choose it.
- The projection answers each reference with every page that holds it, duplicates included. The identity census keeps this owner list once per build, and the snapshot exposes it. Resolution never inverts the canonical reference map, because that map drops a duplicated identity. A visible page with a hidden twin would then read as not found, which discloses the twin.
- Both sources apply one page membership rule. No answer the scan gives today changes, except that a POSIX host now resolves `.MD` pages:
  - The scan accepts an upper-case `.MD` suffix on every platform. The projection, the page parser and the incremental sidecar refresh already accept it. Before, a POSIX host skipped such a page and a Windows host read it.
  - The identity census reads a page's frontmatter after it normalizes line endings, as the page parser and the scan do. Before, the census read raw bytes, so a page with CR-only line endings showed no `exomem_id` there.
  - The identity census never admits a page in the Knowledge Base `trash/` directory, while the scan reads it and a read by path hands out its reference. A vault whose Knowledge Base root holds that directory therefore always takes the scan, so those references resolve as they do today. The directory's presence is a vault-wide fact, so this keeps the source choice independent of the references asked for.
  - A missing or malformed `exomem_id` maps to no page through either source, as before.
- The `memory_refs.identity_census` span gains two integer fields: `snapshot` (1 when the projection answered, 0 when the scan ran) and `cpu_ms` (thread CPU time of the resolution).
- The read creates, fills, rebuilds or refreshes no index, cache or sidecar. A cold server still scans.

## Not Claimed

- The completion time of a read after resolution is not constant. Authorization, page reads and release decisions take time that depends on what the reference resolves to. This change makes the resolution work independent of which references exist. It does not make the whole read take a fixed time.
- An edit made outside the server reads from the projection's last observed state until the watcher reports the edit. Then the projection stops being current and resolution scans. Governed writes patch the projection as they publish their changes.
- When the identity census refuses a vault, for example a page behind a filesystem alias or a page with unreadable frontmatter, the server holds no projection and every read scans, as today.

## Follow-ups (Not In This Change)

- Decide whether a page in the Knowledge Base `trash/` directory should get or resolve a reference at all. Today the sidecar and the scan count it and the identity census does not. Once one rule holds everywhere, a vault with that directory no longer needs the scan.
- Pad governed non-owner direct reads, by path and by reference, into a registered fixed completion class (`projection_timing.fixed_public_completion`, as `release-gate` defines it for `find` and `ask_memory`). A direct read by path already reveals more through its timing than resolution does, so one change should cover both read forms.

## Impact

- Code: `src/exomem/memory_refs.py`, `src/exomem/semantic_contract.py`.
- Specs: `stable-memory-references` gains the resolution requirement. `planning` changes one sentence of "Bound evidence executes under authorization-precedes-resolution" to refer to it, and keeps every scenario.
- Docs: `docs/observability.md` describes the new span fields.
- Tests: `tests/test_memory_refs.py`, `tests/test_plan_motivation_disclosure.py`, and one shared fixture in `tests/conftest.py`.
