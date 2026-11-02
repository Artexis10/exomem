## Why

A Markdown file captured as a Source original is stored as `<name>.md.bin`. The media registry lists no extractor for `.bin`, so the page never asks for text extraction and the file's words are not searchable. An import already stores a non-UTF-8 Markdown original as `<name>.md.txt`, and the text extractor reads it.

## What Changes

- A captured Markdown original is stored as `<name>.md.txt`. It stays outside the graph input set, and its text is extracted in the encoding its bytes show.
- Capture and import share one rule for the stored suffix.

Out of scope: Sources captured earlier keep their `.md.bin` file and stay without extracted text.

## Capabilities

### Modified Capabilities

- `attachment-source-ingestion`: a captured Markdown original has its text extracted.

## Impact

- Code: `src/exomem/add.py`, `src/exomem/adoption_plan.py`.
