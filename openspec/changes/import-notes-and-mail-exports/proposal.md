# Import notes and mail exports

## Why

The cell files each conversation of a ChatGPT or Claude export as its own Source (change `import-app-exports`). People also keep years of notes in Evernote and years of mail in Gmail. Evernote exports a notebook as one `.enex` file, and Google Takeout exports Gmail as one large `.mbox` file. Today each files as one artifact Source with no searchable text and the upload date, so none of the notes or messages can be found.

## What changes

- **Evernote notes.** A recognised `.enex` file expands into one Markdown transcript per note: title, created and updated times, tags, the web-clip source address, to-do boxes and the note's text. Each note files as an `app-note` Source with its own `item_created`. The note's resources (photos, PDFs) that the media pipeline can index become their own staged files and file as their own Sources, with the note's time.
- **Gmail mail.** A recognised Takeout mailbox expands into one transcript per message: From, To, Cc, Date, Subject and the body text. Each message files as a `correspondence` Source with its `Date` as `item_created`. Only parts marked as attachments, of types the media pipeline can index, become staged files. Messages with a label that the registry entry names in `skip_labels` (Spam, Trash) are left out and counted. Gmail labels are not kept in this batch.
- **One email renderer.** One function turns a message into text, for both `.eml` files and mailbox messages. HTML bodies become text through a new stdlib-based `html_text.py`; `.eml` Sources with HTML-only bodies stop holding raw HTML.
- **Expansion holds at mailbox size.** Derived bytes are bounded by the export's own bytes rather than the zip expansion cap. The entry cap follows the import member limit setting. The expansion record keeps its rows append-only and resumes from its last complete row; a mailbox resumes from a byte offset. A full disk stops as a free-space need.
- **The registry stays vendor facts only.** Each parser owns its container (JSON array, XML, mbox) in code. The pack keeps `file_glob`, `item_keys`, `root`, `label_header`, `skip_labels` and each parser's display `words`.

## Capabilities

### Modified Capabilities

- `app-export-import`: recognition by each parser's container; notes, mail, attachments and skipped labels.

## Impact

- **Code:** `source_formats.py`, `export_transcripts.py`, `export_expansion.py`, `adoption_run.py` (`_export_rules`), a new `html_text.py`, `extract.py` (`_extract_eml` through the shared renderer), `vocabulary/packs/core/source-formats.yaml` and `source-kinds.yaml` (`app-note`), `pyproject.toml` (`defusedxml` from the `media-cpu` extra into core).
- **Order:** this change archives after `import-app-exports`, whose requirements it modifies.
- **Not included:** Apple Mail and other mbox flavours until a real export is checked; Google Takeout photo sidecars (a separate change: they date media and must not file as Sources); linking a re-exported item to its earlier Source.
