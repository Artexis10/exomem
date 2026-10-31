## Why

Own-note import (`adopt_as: note`) creates each draft note with its own governed commit. Each commit rewrites the top index, the sub-indexes, the log and the cited Source, and runs its own derived batch. On the same host one note costs about eight seconds, and one Source item costs about one second. An own-note apply call therefore spends roughly half of its time repeating that per-commit work for notes of one chunk.

## What Changes

- After a call has made and timed one note alone, the other notes of an apply chunk commit as one canonical batch, as many as fit in the time that the call has left. Each note keeps its own draft token, its own Source closure and back-reference, and its own contract verdict. The batch writes one Recent-activity bullet and one log entry per note, in one write each, so the index and the log end as they do after one commit per note.
- A batch that refuses falls back to creating each of its notes alone, so each item gets the outcome that it gets today. The fallback stops when the call has no time for one more note, and the continuation makes the rest.
- The semantic writer gains a batch commit for structural creations: draft notes that carry no relation-review artifact. A creation that needs the full relation review, that replaces a page, or that binds a vocabulary entry is refused from the batch and commits alone.
- The vocabulary gate accepts one sealed derived-write manifest per primary page. It validates each manifest against the complete batch and refuses a claim that two manifests share.
- A batch of several pages reports its write advisory as `not_required`. Today only the first note of a call receives an advisory under fast acknowledgement.

## Impact

- Affected specs: `adoption-studio`, `semantic-write-contract`.
- Affected code: `src/exomem/adopt.py`, `src/exomem/adoption_run.py`, `src/exomem/note.py`, `src/exomem/semantic_writes.py`, `src/exomem/vault.py`, `src/exomem/vocabulary_auxiliaries.py`.
