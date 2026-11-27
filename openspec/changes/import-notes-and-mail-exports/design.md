# Design: import notes and mail exports

## Context

`import-app-exports` added expansion at the start of `file`. A recognised export is streamed, and one transcript per item is written into the stage under `<export stem> items/`. Rows go into a private expansion record, and each transcript files like any staged file. Some current behaviour this change replaces:
- Recognition assumes a top-level JSON array.
- The caps are the zip expansion caps: 512 MiB of derived bytes and 65,536 entries.
- The record is one JSON file, rewritten whole every 100 items and read several times per `file` call.

Evernote's export DTD (`evernote-export4.dtd`) fixes the ENEX shape:
- the root is `en-export`;
- `note (title, content, created?, updated?, tag*, note-attributes?, task*, resource*)`;
- times are UTC `yyyymmddThhmmssZ`;
- resource data is base64.

Current Evernote apps export one `.enex` per notebook. Gmail Takeout exports one mbox by default, including Spam and Trash. The labels sit in an `X-Gmail-Labels` header, and the export is split across archive parts above a chosen size.

## Goals and non-goals

Goals:
- One `app-note` Source per Evernote note, searchable, with its own `item_created`.
- One `correspondence` Source per Gmail message, searchable, with its own `item_created`.
- Indexable photos and documents inside notes and mail become their own searchable Sources.
- A multi-gigabyte mailbox files whole within the upload limits, with honest counts.

Non-goals:
- Apple Mail, Thunderbird and other mbox flavours, until a real export of each is checked.
- Gmail labels. This batch keeps no labels. Keeping a person's own labels as a page field is a follow-up.
- Google Takeout photo sidecars, which are a separate change.
- Live sync with any mail or notes service.

## Decision 1: each parser owns its container

The registry keeps vendor facts and display words only:
- `file_glob`;
- `item_keys` (JSON), `root` (XML), `label_header` and `skip_labels` (mail);
- the parser's display `words`.

The `parser` value fixes how content is checked: a JSON array for `chatgpt-conversations` and `claude-conversations`, XML for `evernote-notes`, an mbox for `gmail-mbox`. Code branches only on `parser`, so the registry cannot pair a parser with the wrong container. An entry that lacks a fact its parser needs is refused.

A vault overlay may retire an entry, or change its words (`roles`, `untitled`, `attachment`, `words`) and `skip_labels`. The recognition facts of a shipped entry stay fixed: `file_glob`, `item_keys`, `root` and `label_header`.

The transcript words that each parser shows live in `words`, a map with a closed key set per parser. The first batch's `untitled` and `attachment` stay as they are. New keys cover the tags, source, extracted, kept-in-export and cut-short labels.

## Decision 2: never expand what an expansion wrote

Expansion recognises any staged file except derived ones. Derived files are:
- every path in the expansion record;
- everything under a recorded export's `items/` and `files/` folders.

A stage without a member manifest therefore still expands (for example, a local import with a copy failure). An attachment named `*.mbox`, `*.enex` or `conversations*.json` files as a file and is never expanded again.

## Decision 3: caps follow the export, not the zip rule

The zip caps stop a small archive from decompressing a thousandfold. Expanding an export is not decompression: transcripts are smaller than the items they render, and extracted attachments are bytes the export already holds.

- **Derived bytes.** They are bounded by the export's own staged bytes plus 64 MiB.
- **Entries.** They are bounded by the import member limit from the same setting the import Job reads (`EXOMEM_IMPORT_MAX_MEMBERS`). They are counted over transcripts and attachments, never over skipped or failed items.
- **Peak disk.** On the cell's volume, the peak is the export, plus its derived files, plus the index growth. Filing renames into the Knowledge Base on the same volume, so it frees nothing. Admission's `space_factor` of 2 (from `publish-cloud-import-limits`) covers export plus derived up to the 64 MiB overhead. Index growth comes on top: the large-import rehearsal measures the peak (task 4.3), and the `free_space` stop covers the rest.
- **Full disk.** A full disk during expansion stops with the `free_space` need that apply already uses, not an internal error.

## Decision 4: an append-only record that resumes from its last row

The record is two files under `<state>/import-expansion/<import_id>/`.

**`rows.jsonl`.** One item is one row, appended and fsynced. It holds:
- every path the item derived (its transcript and its attachments), its item id, created time, Source kind and status (`derived`, `failed` or `skipped`, with a reason);
- its export's resume position after this item: the next byte offset for an mbox, the next item index otherwise.

**`state.json`.** It holds each export's state (`working`, `done` or `partial`), the counts by status and the derived bytes. It is rewritten after each slice and only ever caches what the rows say.

**Write order for one item:**
1. Write all of the item's files.
2. Append and fsync its one row.
3. After the slice, rewrite the state file.

**Resume.**
- Resume takes each export's position from its last complete row and discards a torn last line.
- On read, rows are deduplicated by path, so a row repeated after a crash between steps 2 and 3 counts once.
- An item whose files were written but whose row was lost is rewritten under the same name with the same bytes.

**Reads.** Callers that need only counts or states (the filing reply, the stage total, the held exports) read `state.json`. The path map comes from `rows.jsonl` once per `file` call.

## Decision 5: mail

**Splitting.** A message starts at a line that starts with `From ` and is either the first line or follows a blank line. A blank line is one that is empty after stripping CR and LF, so a CRLF mailbox splits correctly. `>From ` quoting is left as written, because Gmail's quoting dialect is not documented.

**Resume.** The resume position is the byte offset of the next message, so a slice seeks there. The reader holds one message at a time, and a message over 64 MiB counts as failed.

**Names.** A message's file name is a hash of its `Message-ID`, or of its bytes when it has none.

**Transcript.** It holds From, To, Cc, Date, Subject and the body:
- The body is the `text/plain` part, else the `text/html` part converted to text.
- HTML conversion sees at most 4 MiB, because `html.parser` has had super-linear cases. A body cut there is marked with the cut-short word.
- Labels are not in the transcript, so a later Takeout of the same mailbox deduplicates even when its read and category labels changed.

**Skipped labels.** The entry names the header that carries labels (`label_header: X-Gmail-Labels`) and the labels to leave out (`skip_labels: [Spam, Trash]`). A skipped message writes nothing. Its row has status `skipped` and reason `IMPORT_ITEM_SKIPPED_LABEL`. The filing view counts it in `left_out` under that reason, never as failed or partial, and never toward the entry cap.

**Attachments.** Only parts with `Content-Disposition: attachment` (RFC 2183) whose type the media pipeline can index (`media_types`) are extracted. Inline and `cid:` parts, and types the pipeline cannot index, stay in the kept mbox. The transcript names each part and says whether it was extracted.

**One renderer.** `extract._extract_eml` and the mbox reader call one message-to-text function, which uses `html_text` for HTML-only bodies.

## Decision 6: Evernote notes

**Parsing.** The reader feeds a `defusedxml.ElementTree.DefusedXMLParser` in 1 MiB chunks, with a custom target instead of a tree builder.
- The target keeps the text of `title`, `created`, `updated`, `tag` and `source-url`, and the note's `content` up to 64 MiB.
- It streams each `resource/data` text through an incremental base64 decoder into a hidden temporary file in the stage, hashing the bytes as they arrive and keeping only the current chunk. The resource's type and file name come after its data, so at `</resource>` the reader renames the file to its final name, or deletes it when the type is not indexable. The transcript, whose `en-media` links need each resource's MD5, is written at `</note>`.
- It drops all other text.
- A note whose content passes 64 MiB counts as failed with its reason, and the parser carries on to the next note.

Peak memory is a small multiple of the 64 MiB content bound plus the chunk size; it never holds a resource whole. `defusedxml` refuses entity expansion and external entities. It moves from the `media-cpu` extra into core dependencies, because every cell parses untrusted XML.

**Names.** A note's file name is a hash of its title, created time and content.

**Transcript.** It holds:
- the title, created and updated times, tags and source address;
- the note text, from ENML through `html_text`;
- `en-todo` as `[ ]` or `[x]`;
- each `en-media` as a reference to the resource with that MD5 hash, by its file name.

A resource whose type the media pipeline cannot index is never kept as a staged file. Its decoded bytes are discarded, it stays in the kept `.enex`, and the transcript names it.

**Kind.** Notes file as a new source kind `app-note` ("a note exported from a notes app"), added to the `source-kinds` pack. The key differs from the existing `note` page category.

## Decision 7: attachment files

Attachments go in one sibling tree per export, `<export stem> files/<item file stem>/`, outside the classified `items/` folder. They need no extra include rule and are classified like any staged file. Each carries its item's `item_created`.

Naming:
- the stem is sanitised, keeping letters of any script;
- the extension is kept, or taken from the MIME type when the name has none;
- names are NFC-normalised, at most 255 bytes, never start with a dot, and are unique case-insensitively within the folder.

## HTML to text

`html_text.py` uses the stdlib `html.parser`:
- block elements end a line (the closed set the HTML specification defines, with its one-line reason for the C6 gate);
- `script` and `style` are dropped;
- a link keeps its text, followed by its address.

## Untrusted content

Notes and mail are untrusted. Expansion stores their text as Source text and calls no instruction-following model on it. Extracted attachments go through the ordinary media pipeline (OCR, image and speech models) like any upload.

## Scale

The load target is 10,000 tenants, and expansion runs per cell.
- **Memory** per item is bounded as Decision 6 states.
- **Resume** is O(1) for a mailbox by byte offset. ENEX resume re-streams and skips by index, about a second per 200 MiB.
- **Record writes** are appends.
- **Record reads** of counts are one small file. The path map is one pass per call (about 1.2 s at 500,000 rows).
- **Duration.** A 5 GB Takeout of 200,000 messages files in about 7.5 hours at the measured 441.8 files a minute, with nobody present.

## Risks

- **Localised labels.** Gmail labels may be localised for non-English accounts. `skip_labels` is an overlay value, and task 4.2 includes a non-English account when one is available.
- **Double storage.** The mbox and the `.enex` are kept whole, so extracted attachments are stored twice. This is accepted for exact-bytes custody; only indexable types are extracted.
