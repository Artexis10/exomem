## Why

A text file that is not valid UTF-8 is filed as an artifact so its original bytes survive, but part of that raw data stays out of search or gets a wrong answer:

- The text extractor reads a plain-text artifact as UTF-8 and replaces what it cannot decode. A UTF-16 file with a byte-order mark and a Latin-1 file are not found by their own words.
- A non-UTF-8 Markdown file is stored as `.bin`, and no extractor reads `.bin`.
- A file name with a backslash is refused as `NOT_FOUND` although the file exists, and the rename of a taken page name is stated nowhere.

A benchmark run of the raw-data profile on the previous release found nothing lost. It found 0 of 2 UTF-16 files, 1 of 2 Latin-1 files and 1 of 2 invalid UTF-8 files by their own text.

## What Changes

- Plain-text extraction reads the bytes in the encoding they show: a byte-order mark decides, otherwise strict UTF-8, otherwise a confident `charset-normalizer` guess, otherwise UTF-8 with replacement characters as before. The extraction records a non-UTF-8 encoding in `extracted_by`.
- `charset-normalizer` becomes a direct dependency.
- A non-UTF-8 Markdown original is stored as `<name>.md.txt`: still outside the graph input set, and now read by the text extractor. The Source page of an artifact takes its media type from the stored file, so the page keeps its import provenance when extraction updates it.
- A source name with a backslash is refused with `INVALID_PATH`, like the other names the vault cannot keep.
- The adoption spec states two behaviours that the code already has: a planned page name that is taken gets the next free name and no page is replaced, and a name the vault refuses for portability is refused with `INVALID_PATH`.

Out of scope: text suffixes that the media registry does not list (`.csv`, `.json`, `.xml`) stay without an extractor when their bytes are not UTF-8. A UTF-16 file without a byte-order mark whose characters are all ASCII still reads as UTF-8 with NUL characters.

## Capabilities

### Modified Capabilities

- `adoption-studio`: collision renaming, refusal of names the vault cannot keep, and the stored form of a non-UTF-8 text original become stated behaviour.
- `automatic-media-processing`: plain-text artifacts are read in the encoding their bytes show.

## Impact

- Code: `src/exomem/extract.py`, `src/exomem/reserved_paths.py`, `src/exomem/adoption_plan.py`, `src/exomem/add.py`, `pyproject.toml`, `uv.lock`.
- Behaviour: text of non-UTF-8 artifacts becomes searchable once the media worker has extracted it. Original bytes are unchanged. A non-UTF-8 Markdown original imported earlier keeps its `.bin` name.
- Undo: revert the change. Extractions already written stay valid text.
