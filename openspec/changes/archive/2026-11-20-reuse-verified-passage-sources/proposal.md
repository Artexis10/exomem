## Why

A passage request reads each selected Source page in full and hashes it, to bind its results to the page's current bytes. The page cache keeps parsed pages within a byte bound. On a corpus larger than that bound, the cache evicts pages as fast as it fills, so the request also parses each page again. During a capture this work is about a third of a passage request's time, and four callers then see p95 above 700 ms.

## What Changes

- A passage request reuses the selected chunks of a Source that an earlier request verified, while the published corpus records the same page hash and the eligible-set proof still holds. That proof is the recall checkpoint, the catalogue identity and the retirement generation.
- The request reads the page again when the published page hash or the proof changes. A Source edited and republished is read again, and an erased Source is withheld.
- An edit that no file event reports does not move the proof. Until the proof moves, the request answers that Source at its published bytes.
- The reused chunks stay within a fixed count and byte bound, and release with the page cache.

## Capabilities

### Modified Capabilities

- `passage-evidence`: results come from Source bytes that the published corpus proof holds current, and verified chunks are reused under that proof.

## Impact

- `src/exomem/passage_evidence.py`: a bounded memory of verified Source chunks, keyed by the published page hash and fenced by the eligible-set proof.
- `src/exomem/semantic_contract.py`: the published corpus gives the page hash that it parsed for one path.
- `src/exomem/find.py`: releasing the page cache also releases the memory of verified chunks.
