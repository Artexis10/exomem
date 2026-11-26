## 1. Registry

- [x] 1.1 Add `vocabulary/packs/core/source-formats.yaml` with two entries, `chatgpt-conversations` and `claude-conversations`, and `src/exomem/source_formats.py`, which holds its `RegistrySpec` (follow `sync_providers.py`). Refuse an overlay entry whose `parser` the code does not implement. Verify: registry tests load both entries; an overlay retires one; an unknown `parser` is refused.
- [x] 1.2 Add `item-created` (`source: item-time`) to `page-fields.yaml` and the closed `item-time` source to `page_fields.py`. Verify: the registry loads it, and `adopt` writes `item_created` only for a planned item that carries one.

## 2. Expansion and filing

- [ ] 2.1 Add the expand step at the start of `_file_step`. Recognise files by the registry and stream items with `ijson`. Write `<export stem> items/<item id>.md` and set its mtime to the item's last update. Keep the expansion record (`<state>/import-expansion/<import_id>.json`) with rows, cursor and per-export state. Yield at the call's budget. Verify with tests on a temporary vault: a 40-conversation invented ChatGPT export and a Claude export each file one `session` Source per conversation, with its title and `item_created`, plus the export file. A forced budget yield resumes and files each item once. Re-importing the same export creates no new Source.
- [ ] 2.2 Write the two parsers: ChatGPT (the `current_node` branch, shown roles from the registry) and Claude (`chat_messages` order, attachment `extracted_content` quoted). Treat every field except the `item_keys` as optional. Verify: fixtures with a missing title, a missing time, an edited-away branch, hidden system messages and an attachment.
- [ ] 2.3 Count derived rows in `_stage_total`. Report failed items and `IMPORT_EXPORT_PARTIAL` and `IMPORT_EXPANSION_CAP` in the filing view. Verify: a truncated export, a malformed item and an export over a lowered cap each show their counts and reasons, and none reads as `done` with all items filed.
- [ ] 2.4 Prove that untrusted text stays data: a conversation that carries instruction-shaped text and a Markdown fence files with its text intact, and no other page changes.
- [ ] 2.5 Add a `user-notes/` entry (see `user-notes/README.md`).

## 3. Corrections carried from the previous import change

- [ ] 3.1 `tests/test_vault_store_read_surface.py`: record the filing classifier's reads with `invocation_writes_outside_writer` patched to return False, instead of subtracting every argument it reads. Verify: the read-surface guard passes, and it fails when a read is removed from the classifier.
- [ ] 3.2 Route `write_intake._replay` through `write_intake.invoke_in_background`, and make its `principal`, `descriptor` and `command` arguments positional-only. Verify: the write-intake and governance principal-binding guard tests pass.
- [ ] 3.3 `cloud_import_client.resumed_choices` returns None when the checkpoint's `state["source"]` differs from the current source. Verify: reusing a checkpoint for another source reports `CHECKPOINT_MISMATCH`, not `CHECKPOINT_OPTIONS_CHANGED`.
- [ ] 3.4 Make the home-import rehearsal (`infra/cloud-rehearsal/src/cloud_rehearsal/home_import.py`) restart the cell while the filing view says `working`. Restart on the first `working` read, with a fixture large enough to outlast the restart. Verify: a dispatched rehearsal run on main reports each file filed once after the restart.

## 4. Delivery checks

- [ ] 4.1 Run the scoped suites for adoption, import filing, page fields, the vocabulary registry and the whole-tree guards in `tests/pr_guard_modules.txt`.
- [ ] 4.2 Before the release note goes out, file one real ChatGPT export and one real Claude export, each from an owner's own account, into a sandbox cell. Check that the conversation counts match the export and the created times match.
