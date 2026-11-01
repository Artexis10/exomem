## 1. Read plain text in the encoding its bytes show

- [x] 1.1 Add `extract.decode_plain_text` and use it in the text, RTF and calendar extractors; record a non-UTF-8 codec in `extracted_by`.
- [x] 1.2 Make `charset-normalizer` a direct dependency; update `uv.lock` with the pinned uv.
- [x] 1.3 Tests: a UTF-16 and a UTF-32 file with a byte-order mark, a Latin-1 file, a UTF-8 file with a stray byte and undecodable noise each read as stated; a UTF-16 calendar extracts its event.

## 2. Keep a non-UTF-8 Markdown original readable

- [x] 2.1 Store it as `.md.txt` in the adoption plan, and derive the page's media type from the stored file.
- [x] 2.2 Test: the artifact keeps its exact bytes, ends in `.md.txt` and has the `text` extraction kind, and its page keeps `imported_from` after the extraction request.

## 3. Refuse a backslash name as an unsafe path

- [x] 3.1 Refuse a backslash in an observed source name with `UNSAFE_PATH` (`INVALID_PATH` at plan time).
- [x] 3.2 Test: a name with a backslash is skipped with `INVALID_PATH` and its neighbours import.

## 4. State the collision rename

- [x] 4.1 Tests of the behaviour the code already has: a taken page name gets the next free name and the existing page stays as written; two notes of one title take the plain and the `-2` name.

## 5. Proof and closure

- [x] 5.1 Run the raw profile of the collection-ingest benchmark before and after on seed 7 and compare `raw.by_class.*.readable_observed_of_filed` and `landing.lost`.
- [x] 5.2 Sync the delta into `openspec/specs/adoption-studio/spec.md` and `openspec/specs/automatic-media-processing/spec.md`, archive with `openspec archive import-text-in-any-encoding`, and run `openspec validate --all --strict` before and after.
