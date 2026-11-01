# Collection ingest rehearsal

A dev-only instrument. It measures what Exomem keeps when an agent adopts an existing
collection through Adoption Studio. The `small`, `standard` and `heavy` profiles write an
Obsidian-style collection of notes with mixed media. The `raw` profiles add messy real-world
input: files with no front matter, other encodings, copies, huge files, deep and odd paths.
It is not shipped and not part of CI.

The instrument sees the product only through public behaviour: files in the vault and MCP
tool results. It reads no product internals, so the same run can score a changed product.

## Parts

| File | Job |
| --- | --- |
| `generate.py` | Write an invented collection and its answer key from a seed. |
| `raw_corpus.py` | Build the messy files, their key rows and the existing-vault pages of the `raw` profiles. |
| `drive.py` | Run the Adoption Studio journey through one stdio MCP session. |
| `score.py` | Compare the finished vault, the drive log and the key. Write one metrics JSON. |
| `control.py` | Prove `score.py` can see success. Score a vault with planted, counted losses. |
| `mcp_session.py` | Shared helper: isolated environment and call ledger. |

## Run it

1. Build a Python environment that has the server and the document extractors. Keep it outside the repository:
   `UV_PROJECT_ENVIRONMENT=$SCRATCH/venv uv sync --frozen --extra media-cpu --no-dev`
2. Generate a collection. The key goes outside `--out`, so the product never sees it:
   `python generate.py --seed 7 --profile standard --out $SCRATCH/collection --key $SCRATCH/collection.key.json`
3. Drive the journey. Pick `personal` or `cloud-like`:
   `$SCRATCH/venv/bin/python drive.py --collection $SCRATCH/collection --workdir $SCRATCH/run --profile personal --python $SCRATCH/venv/bin/python --log $SCRATCH/run.drive.json`
4. Score it:
   `$SCRATCH/venv/bin/python score.py --key $SCRATCH/collection.key.json --drive-log $SCRATCH/run.drive.json --out $SCRATCH/run.metrics.json --python $SCRATCH/venv/bin/python`
5. Check the scorer itself (optional, about 1 minute on the small profile):
   `python control.py --collection $SCRATCH/small --key $SCRATCH/small.key.json --workdir $SCRATCH/control --python $SCRATCH/venv/bin/python --out $SCRATCH/control.metrics.json`
   Every line must print `ok`.

To score messy input, and an import into a vault that already holds pages, use a `raw` profile:

1. Generate it. The page spec of the existing vault goes beside the key, as `raw.key.vault-pages.json`:
   `python generate.py --seed 7 --profile raw --out $SCRATCH/raw --key $SCRATCH/raw.key.json`
2. Drive it with `--existing-pages`, then score it as before:
   `$SCRATCH/venv/bin/python drive.py --collection $SCRATCH/raw --workdir $SCRATCH/run-raw --python $SCRATCH/venv/bin/python --existing-pages $SCRATCH/raw.key.vault-pages.json --log $SCRATCH/run-raw.drive.json`
3. Check the scorer on the raw key (about 1 minute). Every line must print `ok`:
   `python control.py --collection $SCRATCH/raw --key $SCRATCH/raw.key.json --workdir $SCRATCH/control-raw --python $SCRATCH/venv/bin/python --out $SCRATCH/control-raw.metrics.json`

`--workdir` must be empty. All server state, logs and bytecode go under it. Nothing is written
to the repository. Use `umask 022`: permission checks fail under `0002`.

## Generator

Profiles: `small` (60 notes, 15 attachments), `standard` (1,000 notes, 100 attachments) and
`heavy` (standard plus large filler binaries, `--heavy-bytes` sets the size, default 2 GiB).
The same seed gives a byte-identical collection and key (`fixture_tree_sha256` in the key).

`drive.py --include-rule` accepts a JSON selection rule and can be repeated.
The driver applies exactly these rules in both journeys.
For example, `--include-rule '{"path":"Collection","adopt_as":"note","note_type":"insight"}'`
imports the collection's Markdown files as draft notes with a registered note type.
Explicit rules replace the default selection and legacy folder probe. A refusal stops the journey instead of retrying without the rule.
The log records the rules and every Knowledge Base page after each journey, including notes, to detect duplicates.
The driver skips optional Markdown manifests for each run, so those snapshots measure item creation.

### Raw profiles

`raw` (the `small` collection plus 111 raw files) and `raw-standard` (the `standard` collection
plus 371) are new profiles, not options on `standard`. A profile has its own random stream and
its own key, so the bytes and the key of `small`, `standard` and `heavy` do not change, and an old
result stays comparable. The messy files sit beside the notes, in folders that the notes do not
use (`dump`, `legacy`, `types`, `copies`, `deep`, `long names`, `odd names`, `twins`,
`snippets`, `bulk`). The generator needs a Linux file system: it writes names that are legal
there and not on every system.

`--huge-bytes` sets the byte total of the two huge files (default 16 MiB, so the `raw` collection
is about 18 MiB). The text file takes 5/16 of it and stays above the 4 MiB inline-text bound at the
default; below about 13 MiB the text file is small enough to be held as text, and the key says so.
The generator checks every name against the documented limits (255 bytes per name, 4,096 per path)
and writes no control character, so one archive of the tree is never refused as a whole.

Every raw file is a key item with `kind: raw`, a class, the form the contract gives it, its
sha256 and a probe phrase. The key states one outcome per item:

- `filed`: the item has a page. The Source page is the page that the scorer counts, also when an include rule makes an own note.
- `filed-or-refused`: the item has a page, or a refusal that carries a code. The key names no code. The scorer accepts any code and reports the one it saw in `raw.by_class.<class>.refused_by_code`. The backslash name and the name in a folder that ends with a space have this outcome.

An item with neither is `lost`. "Readable" means that `search_memory` with the probe phrase
returns the item's own page in the top 5. The key marks a probe `promised` when the contract
keeps the text in a page. For the other items the scorer reports what it observed.

The key holds the list of classes. `raw.classes` in the key gives each class with its file count and
what the contract promises for it, and each key row names its class and form. Read the contract there;
it has no second copy in this file. The classes cover:

- notes without front matter: `plain-md`, `same-title`, `plain-txt`, `broken-frontmatter`;
- encodings: `line-endings`, `utf8-bom`, `latin-1`, `utf-16-bom`, `shift-jis`, `invalid-utf8`;
- many types and copies: `mixed-types`, `duplicate`, `flat-dump`;
- paths: `deep-path`, `long-name`, `odd-name`, `unsafe-name`, `unicode-twin`, `case-twin`;
- huge files: `huge-text`, `huge-binary`.

### Existing vault

`--existing-pages` makes `drive.py` write four pages into the new vault before the first journey,
dated with the day of the drive. Two take the page name that an incoming item plans: one for a
text original and one for an artifact original. Two take no planned name: an earlier Source of
another collection and a hand-written note.

The spec promises that an import never edits or moves a page that the vault holds. The key adds
one expectation that the spec does not state: an item whose planned name is taken gets the next
free name (`<name>-2`). That rule comes from the page-name allocator in the code. The drive log
records the bytes of each seeded page, so the scorer compares them without the spec. A drive
without `--existing-pages` leaves `existing_vault` `unknown`.

The collection has 23 nested folders, about 10 percent Japanese notes (some with NFD file
names), wikilinks in every written form (path, basename, alias, heading, front-matter
alias), unresolvable and ambiguous targets, embeds, three tag styles, aliases, properties,
sync-conflict copies, zero-byte files, `.obsidian/` and `.trash/`. Attachments reuse the
repository's media fixtures plus generated variants that each carry one known phrase. Some
byte-identical attachments appear under two or three names.

Every note carries one unique probe phrase. All content is invented.

## Driver

The journey is `start`, `select`, `plan`, `apply`, `finish`, then the same five steps again on
the unchanged collection. Then a bounded wait for media extraction (`process_media status`).

- `plan` and `apply` repeat while a result carries `continuation`. The next call of the same
  action passes it back, with the same `plan_id` for `apply`.
- Each `plan` and `apply` page asks for the compact reply. After the last page, or the page that failed, one `status` call
  reads the run for the refusals and the plan. `select` and `finish` ask for the full reply.
- `start` passes `collection=<folder name>`. If the product refuses it, the log says
  `collection_label: unsupported` and the driver retries without it.
- `select` first tries folder rules with `source_kind` and `domain`. If the product refuses,
  the log says `folder_kind_rules: unsupported` and the driver selects the folder plainly.
- `--select-every-file` also names each visible non-markdown file in `overrides`. A product
  that skips such files silently then reports a refusal for each, so the refusal metric is
  not blank.
- `cloud-like` sets `EXOMEM_MEDIA_ENGINES=documents,ocr`. `personal` leaves the engines at
  their default.
- A refusal that carries `retry_after_ms` is repeated after that wait. The log records each wait.

The driver copies the collection into the disposable vault as ordinary files, because generic reads reject hardlink aliases. The fixture originals stay unchanged.
`exomem init` scaffolds the Knowledge Base first, because the server will not start on a bare folder.

## Metrics

Each rate is `{numerator, denominator}`. `measurement_gaps` counts scorer reads and searches that failed; a non-zero gap makes the affected rates lower bounds. A drive step that failed is in `cost.drive_call_failures`, never silent.

| Metric | Meaning |
| --- | --- |
| `landing.notes_landed` | Key notes that have a page whose `imported_from` equals the original path. |
| `landing.attachments_landed` | Same, for attachments. Also given by extension. |
| `landing.items_refused_by_code` | Key items the drive log shows refused, by error code. |
| `landing.items_silently_absent` | Key notes and attachments that neither landed nor appear in any refusal. |
| `landing.lost` | Key notes, attachments and raw items with no page and no refusal recorded by the first journey. `first_journey` says whether the first journey ran to the end, stopped at a step, or is absent from the log, so the items a stopped run never reached are not read as lost by the product. |
| `landing.junk_landed` | Junk files (conflict copies, empty files) that landed. Lower is better. |
| `raw` | Per class and in total: items filed, refused, lost and meeting the key; filed items whose original bytes are held (a stored file with the same sha256, or the original text inside a page file); filed items held in the form the key states (items whose original cannot be compared are shown as `unknown`, not as a mismatch); probe phrases found by search, for all probed items and for the promised ones. State `unknown` when the key has no raw items. |
| `existing_vault` | Seeded pages unchanged, changed and missing; colliding items filed and given a numbered name; bystander pages unchanged; seeded pages found by search. State `unknown` when the key or the drive log has no seeded pages, `partial` when the log lacks a page. |
| `links_preserved` | Resolvable key links where `read_memory(links=true)` shows an outbound link from the note's page to the target's page. Unit: distinct (note, target) pairs. |
| `embeds_bound` | Key embeds where the note's page links to the attachment's page or to a vault file with the same sha256. |
| `fidelity` | Tags, aliases and front-matter properties present on the landed page. |
| `homes.classified_homes` | Landed pages that sit outside `Knowledge Base/Sources/Imported/`. |
| `rerun_duplicates` | Pages with `imported_from` that the re-run added, draft notes with `collection_path` and no `imported_from` that it added, and items with more than one page. |
| `media.states` | One state per attachment. A job state is copied from `process_media status` as is. Without a job: `text-found-no-job`, or `unknown-no-job-no-text`. Not landed: `not-landed`. |
| `recall_at_5` | One `search_memory` query (limit 5) per note probe and per distinct media phrase. English and Japanese are separate. |
| `cost` | Tool calls, wall time per phase and refusals from the drive log. The scorer's own calls are counted apart. |

`samples` holds a few evidence items per metric. Use them to check the numbers by hand.

## Limits

- Search is lexical. The driver disables embeddings, so no model weights or network are needed. Recall scores a probe phrase that is unique in the vault, which is an easy query. Read it as "is the page findable at all".
- A fresh server may lag behind the vault. `score.py` asks every miss once more after `--settle-s` seconds and reports both numbers (`counterpart_in_top5` and `..._first_pass`).
- `landing` and `homes` read the front matter field `imported_from` and the path of each page under `Knowledge Base/`. A product that stores the original path elsewhere needs the scorer changed.
- The `Sources/Imported/` prefix in `score.py` marks an unclassified home. Change that one constant if the product renames its holding folder.
- A link is preserved when an outbound entry names the target page by full path, path under the Knowledge Base, or a stem that no other page shares. A product that writes another form is scored as not preserved.
- `read_memory` lists each target once, as written, without alias or heading. A link and an embed to one page look the same there, and Markdown-style links are not listed. So `embeds_bound` means "the note links to the attachment's page or file".
- Media states reflect the host. Without Tesseract an image job ends `blocked`. Without a speech engine audio and video jobs cannot finish. Record the host with the result.
- The media wait is bounded (`--media-deadline`, default 300 seconds). `media.queue_converged_*` says whether the queue drained.
- One seed and one run per profile. Do not read small differences as signal.
- `heavy` is not exercised by default. It writes gigabytes of filler.
- `control.py` proves the scorer's matching logic, not the product. Its vault is hand-made from the key. With a raw key it plants a lost item, a refused item, wrong bytes, a missing phrase, an original that cannot be compared, an overwritten page, a deleted page and a Unicode twin whose page is gone.
- `raw` counts a refusal only from the journey log (`select.rejected`, `plan.skipped`, `apply.failed`). The residual list of a staged `_Imports/` run is not read, so an item that only that list names reads as lost.
- A text original is held when its bytes appear inside the page file. The check proves the bytes are there. It does not prove the fence around them. It needs the vault's own copy of the original, which the scorer accepts only when its hash equals the key's. Otherwise the item counts under `unknown`, and `measurement_gaps.raw_originals_not_comparable` says how many.
- A rate with denominator 0 means that no item of that class qualifies. It does not mean a perfect or a zero score.
- The seeded pages carry the drive day. A drive that crosses midnight plans some names on the next day. Those items are filed and the seeded pages stay, but they take no numbered name, and `existing_vault.colliding_items.item_took_a_numbered_name` shows it.
- Two names that are equal after Unicode NFC can share one `imported_from`. A twin counts only a page whose `original_sha256` equals its own, so the page of the other twin never stands in for it. A twin page with no `original_sha256` reads as lost.
