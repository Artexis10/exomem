## Context

`adoption_plan.decode_text` decides whether an original is a text Source or an artifact: valid UTF-8 without NUL is text. Re-encoding any other file into a UTF-8 page would lose its original bytes, so that decision stays. The fix is at extraction, which is where the text of every artifact enters search.

## Decisions

### D1. One decoding helper for every plain-text extractor

`extract.decode_plain_text` returns the text and the codec that read it. The order is a byte-order mark, strict UTF-8, a `charset-normalizer` guess accepted at or above a confidence bound, and UTF-8 with replacement characters. The text, RTF and calendar extractors all use it.

- The byte-order marks are the closed set that the Unicode standard fixes. UTF-32 LE is tested before UTF-16 LE because its mark begins with the UTF-16 LE mark.
- The bound is 0.9, one minus the library's mess ratio. A probe gave 1.0 for most correct guesses and 0.82 to 0.90 for mostly-UTF-8 text with stray bytes. A single-byte guess for that text turns every valid multi-byte character into mojibake, where replacement characters keep them. A guess that falls under the bound keeps today's result.
- Among accepted guesses, Windows-1252 wins when it is present. The library ranks equally clean single-byte guesses by language coherence, and that read Latin-1 French, Swedish and Portuguese as Windows-1250, which turns a grave "e" into a "c" with a caron. Windows-1252 is the Encoding Standard's default for legacy Western text.
- Line endings read as one newline, as `Path.read_text` read them, so existing extractions do not change shape.

Rejected: decoding in `adoption_plan.decode_text`. A Latin-1 page would hold different bytes from its original. Rejected: a per-extension list of encodings, which a file's name does not fix.

### D2. The extraction records the encoding in `extracted_by`

`ExtractResult` has no encoding field and the sidecar carries one provenance string, `extracted_by`, which already takes suffixes (`+diarized`, `+timed`). A non-UTF-8 read is `text:<codec>`, for example `text:utf-16`. UTF-8 stays `text`. A fallback to replacement characters stays `text`, as today.

### D3. A non-UTF-8 Markdown original is stored as `.md.txt`

The artifact had to leave the `.md` suffix because the graph reads every `.md` under the knowledge base as strict UTF-8. The `.bin` suffix did that, and also left the file without an extractor. `.md.txt` keeps the graph out and the original kind visible, and the media registry reads `.txt` as text. The scope is the adoption plan; capture keeps `.md.bin`.

The Source page of an artifact named its media type after the original's name, which is `.md` and has none. A page without a media type is not in the shape that extraction updates in place, so extraction rebuilt it and dropped the import provenance (`imported_from`, `original_sha256`). `add._render_source` now reads the media type from the stored file, which is the file extraction reads. For every other suffix the original and the stored name agree, so no page changes.

### D4. A backslash in an observed name is refused as `UNSAFE_PATH`

The held-filesystem source spelling read a backslash as a separator, so `a\b.md` looked for `b.md` in a folder `a` and answered "missing". An observed name keeps a backslash as a character that the vault's separators and Windows cannot keep, so the source read refuses it. `adoption_plan.source_failure` maps `UNSAFE_PATH` to `INVALID_PATH`, as for a name that ends with a space.

A name that ends with a space or a dot is refused on purpose (`reserved_paths._normalized_parts`, "platform-aliased components"): Windows drops the trailing character, so the name would not survive a move of the vault. That rule stays.

## Risks

- A wrong single-byte guess reads accented letters as other letters. The original bytes stay in the artifact, and a later better guess re-extracts.
- A suffix that the media registry does not list stays unread when its bytes are not UTF-8. A wider list changes which files the vault watcher treats as media and needs its own change.
