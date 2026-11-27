## 1. Registry and recognition

- [x] 1.1 In `source_formats.py`, let each parser declare the facts it needs and refuse an entry without them. Add entries `evernote-notes` (`*.enex`, `root: en-export`, `item_kind: app-note`, its `words`) and `gmail-mbox` (`*.mbox`, `label_header: X-Gmail-Labels`, `skip_labels: [Spam, Trash]`, `item_kind: correspondence`, its `words`). Add the kind `app-note` to `source-kinds.yaml`. Verify: registry tests for each parser's facts, a missing fact refused, an overlay that changes `skip_labels` and `words`, an overlay that changes `label_header` refused.
- [x] 1.2 Never recognise a derived file (recorded rows, and everything under a recorded export's `items/` and `files/` folders). Verify through the filer: a message whose attachment is named `archive.mbox` files it as a file; a ChatGPT export in a stage with no member manifest still expands.

## 2. Expansion at mailbox size

- [x] 2.1 Bound derived bytes by the export's bytes plus 64 MiB, and entries by `EXOMEM_IMPORT_MAX_MEMBERS`, counting neither skipped nor failed items. Map a full disk during expansion to the `free_space` stop. Verify: a lowered limit stops with `IMPORT_EXPANSION_CAP`; skipped items do not count; an `ENOSPC` during a write parks with a `free_space` need.
- [x] 2.2 Split the expansion record into `rows.jsonl` (appended, each row with its export's resume position) and `state.json` (states and counts), written in the design's order. Resume from the last complete row; discard a torn line; deduplicate rows by path. Count readers read `state.json`; the path map is read once per call. One item is one row listing every path it derived. Verify: crashes between each write step, including between an item's attachment files, file each item once with its attachments and count it once.
- [x] 2.3 Write attachments under `<export stem> files/<item file stem>/`, named by the design's rule (keep extension, NFC, at most 255 bytes, no leading dot, unique case-insensitively). Verify: an attachment's Source kind is decided like a staged file's; `Счёт.jpg`, a name with no extension, two `image001.png` in one message, a leading-dot name.

## 3. Readers

- [x] 3.1 Add `html_text.py`, and route `extract._extract_eml` and the mbox reader through one message-to-text function. Verify: block elements, lists, links, `script` and `style`, entities, malformed HTML, input over 4 MiB; an `.eml` with an HTML-only body files as text.
- [x] 3.2 Add the `evernote-notes` reader: `DefusedXMLParser` fed in 1 MiB chunks with a custom target, resource base64 streamed into a hidden temporary file and renamed or deleted at `</resource>`, transcript written at `</note>`, 64 MiB content cap (move `defusedxml` into core dependencies), transcript fields from the design. Verify through the filer: an invented 30-note export with a JPEG and an unindexable resource; an entity-expansion ENEX refused; a truncated ENEX partial; an oversized note failed while the rest file; peak memory under 256 MiB for a note with a 200 MiB resource.
- [x] 3.3 Add the `gmail-mbox` reader: separator rule, byte-offset resume, 64 MiB per-message cap, `Message-ID` names, labels kept out of the transcript, `skip_labels`, attachments by disposition and indexable type. Verify through the filer: an invented mailbox with plain, HTML-only, multipart, inline-image and attachment messages, two Spam and one Trash; a CRLF mailbox; a forced resume mid-file; a re-import with changed labels is skipped; a message with no `Message-ID` keeps a stable name; an HTML body over 4 MiB is marked cut short.

## 4. Delivery checks

- [x] 4.1 Add a `user-notes/` entry. Run the scoped suites, the guard modules in `tests/pr_guard_modules.txt` and the read-surface test.
- [ ] 4.2 Before the release note goes out, file one real Evernote notebook export and one real Gmail Takeout mailbox, each from an owner's own account, into a sandbox cell. Include a non-English Gmail account when one is available. Check that item counts, created times and skipped counts match.
- [ ] 4.3 Measure the peak disk of a large mailbox import in the large-import rehearsal and record it in the design.
- [ ] 4.4 Archive this change after `import-app-exports`.
