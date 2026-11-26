# Design: import app exports

## Context

Home, the CLI and MCP uploads all stage files under `_Imports/<id>/`. They all reach Adoption Studio's `file` action, which the cell's resident filer drives to the end (`import_filing.py`). `file` loops `_file_step` (`adoption_run.py`): start, select, plan, apply and finish. Identity throughout is the staged path. `outcomes` is keyed by `original_path`, the filing view counts manifest members, and `unfiled_members` lists the members with no filed outcome.

`plan_item` reads a text original up to 4 MiB inline and files anything larger as an artifact Source with no text. `captured` is the filing day. The only per-member time is `file_modified`, which comes from the member's mtime.

## Goals and non-goals

Goals:
- One Source per conversation in a ChatGPT or Claude export, searchable, with the conversation's own created time.
- One registry where a later format (Notion, Takeout, ENEX, WhatsApp, Mem) adds an entry and a parser.
- Honest counts: read, filed, failed and partial.

Non-goals:
- Live sync with any vendor. The 2026-09 ruling holds: import files, do not sync SaaS.
- Linking a re-exported conversation to its earlier Source.
- Attachments inside conversations. The export's other files (images, documents) are already staged files and file as usual.

## Decision 1: expand at the start of `file`, into the stage

The cell writes derived files into the stage before `select`, and filing treats them as ordinary staged files.

The alternatives:
- **At each entry point** (`import_upload.stage`, `local_import`, `cloud_import_job`). This is three hooks for one rule. The Home upload goes through the Cloud tar path, so a hook at one entry point would miss the others.
- **Inside `plan_item`, one path to many items.** `outcomes`, the manifest total, `unfiled_members`, `consume`, the rename step and dedup all assume one path per item. Each would need a virtual path scheme.

Expansion at `file` reuses the whole filing path. Its cost is a small record of what was derived, which the stage total and the planned items read.

**Shape.**
1. The expand step walks the stage's files once and asks the registry which ones are recognised.
2. For each recognised export it streams items with `ijson`, which is already a dependency.
3. It writes `<export dir>/<export stem> items/<item id>.md` for each item.
4. It sets the derived file's mtime to the item's last update, so `file_modified` stays true.
5. It appends one row per item to the stage's expansion record: `<state>/import-expansion/<import_id>.json`. The row holds the derived path, the item id, its created time, its Source kind, the export's path and the format key.
6. The record also holds a cursor (the export path and the count of items done) and a per-export state: `working`, `done` or `partial` with its reason.

**Resume.** Each slice stops at the call's budget. The next `file` call restarts the stream and skips the items that the cursor counts. A crash between writing a file and recording its row rewrites the same file with the same bytes. The derived name is the vendor's id, so a repeat is idempotent.

**Budget.** The existing 512 MiB expansion cap and 65,536-entry cap apply to derived files too, counted in the record. An export that would pass a cap stops at the cap with state `partial` and reason `IMPORT_EXPANSION_CAP`. The items written so far still file.

## Decision 2: a `source-formats` registry, closed vendor facts only

Each entry has these attributes:
- `file_glob`: a file name pattern over the export's member name, for example `conversations*.json`. It is a closed fact of the vendor format, like `conflict-copy-glob` in `sync-providers`.
- `item_keys`: the JSON keys that every item of that vendor format carries.
- `parser`: a closed value that code implements (`chatgpt-conversations` or `claude-conversations`).
- `item_kind`: the source kind each item files as (`session`).
- `roles`: the vendor's author roles that the transcript shows, each with its display label.
- `untitled` and `attachment`: the words a transcript uses for a conversation with no title and for the heading of a quoted attachment, so a format brings its own language.

Since February 2026 a ChatGPT export is several files, `conversations-000.json`, `conversations-001.json` and so on, beside `export_manifest.json` and `user.json`; a Claude export is one `conversations.json`. The pattern `conversations*.json` therefore also matches the Claude file name, and the first item's keys tell the vendors apart: ChatGPT items carry `mapping` and `current_node`, and Claude items carry `chat_messages` and `uuid`.

This is sound under registry rule C6, because each value is a vendor-defined format fact, not a guess at meaning. Code branches only on the closed `parser` value. A vault may disable an entry or change `roles`, `untitled` and `attachment` through its overlay; the recognition facts of a shipped entry stay fixed. A new format needs a parser in code and an entry in the pack.

## Decision 3: the item's own time is a provenance field

`page-fields.yaml` gains `item-created` (`field: item_created`, `role: provenance`, `source: item-time`). `item-time` is a new closed `source` value: the time an item states for itself, read from the expansion record. `captured` stays the filing time, so Source names and destinations do not change with old dates. A Source that did not come from an export has no `item_created`.

## Transcript format

Each derived file is UTF-8 Markdown:
- The first line is `# <conversation title>`, so `title_from_text` names the Source. An untitled conversation gets `Untitled conversation <created date>`.
- Each shown message gets a `**<role label>** · <time>` line, followed by its text.
- **ChatGPT:** the shown branch is the path from `current_node` back to the root, reversed. Branches that the person edited away are left out. Hidden and `system`/`tool` messages are not shown unless the registry lists their role.
- **Claude:** messages follow `chat_messages` order. An attachment's `extracted_content` is shown under the message as a quoted block named by the attachment's file name.

## Untrusted content

An export is untrusted content that could steer an agent toward an outbound action (the "lethal trifecta" pattern; the CaMeL design separates such data from control). Its text goes into a Source as data. No step reads it as an instruction, follows its links or calls a model on it. A conversation that quotes a prompt stays quoted text. The transcript writer escapes nothing it does not need to: `adopt` fences the derived file's bytes as it fences any text original.

## Scale

The load target is 10,000 tenants (Substrate `openspec/project.md`). Expansion runs per cell inside that cell's own filing and shares nothing across tenants. Memory stays bounded: `ijson` streams one item at a time, and no step loads the whole export. Time per `file` call stays within the existing request budget, because expansion yields at the budget like plan and apply do.

A heavy export of 5,000 conversations at the measured 441.8 files a minute (rehearsal run 45869658005) files in about 11 minutes. The filer runs it with nobody present.

## Accounting

- `_stage_total` counts manifest members plus derived rows.
- The export file itself is a member and files once, as an artifact Source whatever its size, so no conversation is indexed twice. Each shard of a split export files once the same way, and its transcripts go in a folder named for it (`conversations-001 items`).
- An item that cannot be read gets no file, and the record keeps it as `failed` with its reason. The filing view adds the failed items to its `failed` count and to `left_out.by_reason`.
- An export whose stream ends early or is malformed gets state `partial`, with reason `IMPORT_EXPORT_PARTIAL`. The view shows it under `left_out` by the export's path.

## Format evidence

No vendor documents these JSON schemas. The parsers follow the shapes that the vendors' exports carried in 2026. Fixtures are invented and anonymised. A parser treats every field as optional, except the item keys that the registry declares. A missing field degrades the transcript (no time, no title) and never fails the item. ChatGPT times may be epoch seconds or ISO 8601 strings, and a time of any other form is left out. A real export from each vendor is checked once before the release note goes out (task 4.2).

## Risks

- **Vendor schema drift.** A changed export reads as `partial` or unrecognised, never silently empty. The registry entry is the place to fix it.
- **Large single files.** A 1 GiB `conversations.json` passes the 512 MiB cap and stops as `partial`. The cap is a setting.
- **Dedup by bytes.** A re-export of an unchanged conversation produces the same transcript bytes and is skipped as already imported. A changed conversation is a new Source.
