<!-- authority:non-specification -->

# Collection ingest benchmark: October 2026

This record measures how Adoption Studio imports an existing collection, before and after the change `make-collection-adoption-lossless`. The contract is `openspec/specs/adoption-studio/spec.md`, as that change amends it. The instrument is `benchmarks/collection_ingest/`. Its README gives the commands and defines each metric.

## Inputs

- **Fixture.** The generator writes an invented collection from seed 7, profile `standard`: 1,000 Markdown notes in 23 folders, 100 attachments and 49 junk files.
  - The notes use every wikilink form, embeds, front matter, inline tags, aliases and some ambiguous basenames.
  - 100 notes are in Japanese, and 31 of those have NFD-normalized file names.
  - The attachments are PDF, DOCX, XLSX, PPTX, ODF, EPUB, HTML, RTF, EML, ICS, PNG, HEIC, OGG and MP4, with 17 groups of identical bytes.
  - Fixture tree SHA-256: `3ad641b60eb1ab5291662d35d2fdefb764ec8567fa9bfa852d5abbada2f71bfa`. Both runs used the same tree.
- **Answer key.** The generator writes the key from its own seed, never from product output.
- **Journey.** One stdio MCP session on a disposable vault and state runs `start`, `select`, `plan`, `apply` and `finish`. It follows any `continuation`, then repeats the whole journey on the unchanged collection.
  - The driver rules four folders with registered kinds and domains.
  - Search runs lexical only (`EXOMEM_DISABLE_EMBEDDINGS=1`).
  - The `cloud-like` profile sets `EXOMEM_MEDIA_ENGINES=documents,ocr`.
- **Builds.** Before: main at `f59917d7`. After: the change branch at `9f47b66a`. The later pacing commit changes only how `apply` splits its calls; the request budget section measures it.

## Results (personal profile; cloud-like matches on every landing and fidelity row)

| Metric | Before | After |
| --- | ---: | ---: |
| Notes landed | 1000/1000 | 1000/1000 |
| Attachments landed | 17/100 | 100/100 |
| Items silently absent (neither landed nor refused) | 83/1100 | 0/1100 |
| Resolvable links kept as graph links (distinct note–target pairs) | 0/2029 | 2028/2029 |
| Embeds bound to the imported attachment | 0/124 | 124/124 |
| Front-matter tags carried | 0/1359 | 1359/1359 |
| Inline tags carried | 0/941 | 47/941 |
| Aliases carried | 0/198 | 198/198 |
| Pages filed in a kind/domain home | 0 | 146 |
| New Source pages created by the re-run | 1029 | 0 |
| Recall@5 on note probe phrases (en / ja) | 900/900, 100/100 | 900/900, 100/100 |
| Drive wall time, both journeys | 1244 s | 326 s |
| First `apply`, all calls | 540 s in one call | 157 s over 3 calls (6–7 calls after pacing) |
| Re-run `apply` | 527 s | 3.7 s |

**Inline tags.** Inline `#tags` in a note body are not read. `finish` coverage states this gap. The scorer counts an inline tag as carried when the same tag reaches the page by another route.

**Homes.** The 146 pages are the files under the four ruled folders, filed in `Sources/<Kind>/<Domain>/`; 151 files sit under those folders, junk included. Unruled files stay in `Sources/Imported/`.

**Media.** The after-run requested extraction for every attachment. The scratch environment lacked the optional extraction libraries and the OCR binary, so most jobs ended `blocked` with `ExtractionUnavailable`. `finish` reported them so. Under `cloud-like`, it reported the 3 audio files and the 1 video as `engine_disabled`. These states describe the test host, not the product's extraction quality.

**Junk.** 12 of 49 junk files still land: OneDrive-style and numbered conflict copies. The filename-based junk classifier does not recognize them. This is existing behaviour, unchanged here.

## Request budget

`apply` stops before the MCP request budget of 50 s ends and returns a continuation cursor. Over three later runs of the same fixture with the final pacing, 19 of 20 `apply` calls ended in 45.7 s or less.

One call under host load took 51.8 s. The post-terminal media fan-out waited on the graph, which the background media worker held. A client that times out on such a call recovers by repeating the same cursor, which replays the stored result. Moving that fan-out out of the request is follow-up work.

## Limits

- One seed and one host. Wall times are from a shared laptop and vary with load.
- Recall uses a unique phrase on the lexical lane, so it shows that a page is findable, not answer quality.
- Agent enrichment (entities and typed relations) is out of scope for this change and is not measured here.
