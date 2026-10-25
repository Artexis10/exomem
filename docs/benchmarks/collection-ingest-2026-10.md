<!-- authority:non-specification -->

# Collection ingest benchmark: October 2026

Adoption Studio imported all 1,000 notes and 100 attachments in both profiles. Repeating each import created no Source pages.
This record reports the fixed fixture, including its limits. It does not measure semantic enrichment or compare runtime speed.

The contract is [adoption-studio](../../openspec/specs/adoption-studio/spec.md).
The [instrument README](../../benchmarks/collection_ingest/README.md) defines the metrics and gives the generation, drive and scoring commands.

## Inputs and source identity

The `standard` fixture uses seed 7: 1,000 Markdown notes, 100 attachments and 49 junk files in 23 folders.
It includes 100 Japanese notes and 31 notes with NFD filenames. The generator writes the answer key before the product runs.

| Input | Identity |
| --- | --- |
| Baseline product | `f59917d741aff2d3701612a7782bfa81b29bb89d` |
| Measured product | `d7142ccf00cd5ccccf91643a816193ccdbaa21b3` |
| Measured `src/` tree | `0e2a034d9d0b799121c807ba94bfc18042dd7385` |
| Instrument tree | `8b1ffd8b0bafc1301afd41dee21817bd7574c0c2` |
| Fixture tree SHA-256 | `3ad641b60eb1ab5291662d35d2fdefb764ec8567fa9bfa852d5abbada2f71bfa` |
| Answer key SHA-256 | `431e2706c1fe261a5ac20a5204e4e1748c856bb92b0695120637385f9c8de59d` |

Both final drive logs record the measured commit and `src_dirty: false`.
The final runs used CPython 3.14.6 and the frozen dependency lock, without optional media extras.
Each profile used its own disposable vault and state through the public stdio MCP interface.
Embeddings and CLIP were disabled, and model downloads were offline. The journey made no provider API requests.
The Cloud-like profile selected `documents,ocr` media engines. Four registered folder rules supplied kind/domain homes.

The final driver places ordinary copies. The historical baseline placed hardlinks.
A fixed-date check on the unchanged baseline compared both placements without applying an import.
Each placement produced 1,149 files, 1,134 inventory rows, and 1,029 identical selected and rendered plan items, with no rejection or truncation.
That supports reuse of the baseline landing, fidelity, home and duplicate counts.
It does not support a speed comparison, an overview comparison or a pack-suggestion comparison.

## Landing and fidelity

Every row below has complete scorer reads: both final `measurement_gaps` maps and all drive/scorer failure maps are empty.
The baseline columns use its recorded personal profile; its Cloud-like landing and fidelity counts match.

| Metric | Before | Personal | Cloud-like |
| --- | ---: | ---: | ---: |
| Notes landed | 1000/1000 | 1000/1000 | 1000/1000 |
| Attachments landed | 17/100 | 100/100 | 100/100 |
| Intended items silently absent | 83/1100 | 0/1100 | 0/1100 |
| Raw key-expected link pairs kept | 0/2029 | 2028/2029 | 2028/2029 |
| Embeds bound | 0/124 | 124/124 | 124/124 |
| Front-matter tags carried | 0/1359 | 1359/1359 | 1359/1359 |
| Inline tags present through another route | 0/941 | 47/941 | 47/941 |
| Aliases carried | 0/198 | 198/198 | 198/198 |
| Classified homes | 0 | 146 | 146 |
| New Source pages on replay | 1029 | 0 | 0 |
| English phrase recall@5 | 900/900 | 900/900 | 900/900 |
| Japanese phrase recall@5 | 100/100 | 100/100 | 100/100 |

Each final first run records 1,112 applied outcomes. Each replay records 1,112 `already-applied` outcomes.
The extra 12 are existing conflict-copy junk false negatives.
The other 37 junk files are accounted for: 22 `JUNK` exclusions, 14 files in two hidden folders, and one hidden file.
Neither final inventory was truncated.

Independent file checks matched all 1,100 originals to the key in each profile.
They also recovered the exact original bytes from 1,017 text captures and matched all 83 artifact copies byte-for-byte.
All 31 NFD-named notes and all 100 Japanese notes landed.

**One answer-key error.** One Japanese alias occurs in three original notes, but the key labels one reference to it as resolvable.
Both plans correctly report that alias as ambiguous. The table preserves the raw 2028/2029 score and the unchanged key.
This exception is an instrument mislabel, rather than evidence that an unambiguous link was dropped.

**Attachment difference.** The [paired cases](collection-ingest-2026-10-attachments.csv) contain 83 gains and 17 unchanged cases in each profile.
The observed increase is 83 percentage points; its paired cluster-bootstrap 95% interval is 70.94–93.41 percentage points.
The bootstrap resamples 65 distinct original-byte hashes, retaining duplicate files together, with 10,000 replicates and seed 2081.
It describes this generated fixture's composition; it does not establish results on real collections.

Inline tags are not parsed. The 47 counted inline tags also arrive through front matter.
Other front-matter properties are not promoted: the scorer reports 0/2252. Their original representation remains in the exact captured bytes.
The 146 classified files came from the four ruled folders; the other intended files stayed in `Sources/Imported/`.

## Calls and extraction

These are final-run observations on one shared host. They are not a comparison with historical elapsed times.

| Observation | Personal | Cloud-like |
| --- | ---: | ---: |
| First plan calls / longest call | 1 / 3.85 s | 1 / 3.63 s |
| First apply calls / longest call | 6 / 48.99 s | 7 / 38.69 s |
| Replay plan calls / longest call | 1 / 8.41 s | 1 / 6.00 s |
| Replay apply calls / longest call | 1 / 8.40 s | 1 / 6.59 s |
| Drive elapsed, both journeys and media waits | 341.8 s | 333.9 s |
| Drive calls / scorer calls | 47 / 1974 | 47 / 1974 |
| Scorer elapsed | 169.4 s | 133.2 s |

All 15 apply calls in these final runs completed inside the 50-second request budget.
Tiny-budget integration and browser checks separately exercised continuation, cursor replay, and reload during partial apply.
The existing post-terminal media fan-out can still wait on the graph under load.
These observations do not remove that risk; persist-only extraction requests remain a prerequisite for the first real collection import.

At first `finish`, personal coverage reported 67 blocked, six done and ten pending artifact extractions.
Cloud-like coverage reported 63 blocked, four engine-disabled, six done and ten pending.
The disabled items were three audio files and one video.
After settling, both queues had zero pending, running or failed jobs; blocked counts remained 67 and 63.
Missing optional libraries caused the blocked jobs. This is not an extraction-quality result.

The scorer's attachment states differ from queue states.
Personal reports 67 `blocked` and 33 `text-found-no-job` attachments.
Cloud-like reports 63 `blocked`, 33 `text-found-no-job`, and four `unknown-no-job-no-text` attachments.
The four unknown scorer rows correspond to the engine-disabled items that `finish` identifies explicitly.

The immediate title-based coverage sample found 4/10 exact targets in each profile.
The later scorer found every note using its independent unique phrase.
These are different probes and observation times; neither measures answer quality.
Agent enrichment, entity correctness and typed-relation quality are not measured here.

## Run receipts

The raw logs retain environment paths and stay outside the source repository.
These SHA-256 values bind the reported results to those files.

| File | SHA-256 |
| --- | --- |
| `final-personal.drive.json` | `8cefcd1b71724edd8fcd39cde216523d7c8d8dda59426788908399357762c04e` |
| `final-personal.metrics.json` | `d5979edadf27608ff60ec2b594b2f2224a515932a72c8bcb836fc214b2711625` |
| `final-cloud-like.drive.json` | `10716d8270ea193812764ed02bf085499b3d107dd29b7c6285ddbb7b2e11d92b` |
| `final-cloud-like.metrics.json` | `fc3b1b185f536fd36e7f1955538253ecc55f89306d6ec3cf49adae8fc50d9b85` |
| Paired attachment CSV | `29cd6212096e3393d8f282255d5195e663f45c851aae9525bf61c51918d8ebb2` |
