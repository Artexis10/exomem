# Stop the idle reaper throwing away the recall resolver

## Why

A recall call on the production vault measured **34,052 ms, of which 30,787 ms
was one stage** — `graph.resolver`, building the recall wikilink resolver
(#676). Every other lane in that call was fast: `vector: 317 ms`,
`bm25: 24 ms`, `keyword: 121 ms`, `fusion: 74 ms`. Ninety per cent of a
thirty-four-second read was not retrieval.

#677 has since taken most of that off the reader: the cold build is
single-flighted, and every correctness eviction schedules a background rebuild,
so an eviction no longer leaves the vault with no resolver and no plan to get
one.

One path out of that net remains, and it is the one #676 names second.
`unload_ram_caches` deliberately bypasses `_evict_recall_resolver` and clears the
maps directly, precisely so that no rebuild is scheduled — reasonable for an idle
reaper handing memory back, except for what it is handing back.

Measured on a 2,400-page vault, matching the production size:

| | |
|---|---|
| resolver retained in RAM | **3.05 MiB** (1,334 bytes per page) |
| walking the vault | 484 ms |
| admitting the candidates | 5,406 ms |
| reading and parsing every admitted page | **39,079 ms** |

So the idle reaper releases three megabytes, in a process that is also holding a
roughly one-gigabyte embedding model, and charges the next reader about
forty-five seconds to get them back. That is the wrong side of the trade, and the
caller who pays it cannot usefully be told to come back later.

## What Changes

**Idle memory reclamation keeps the recall resolver.** `unload_ram_caches` is
used in two different senses: as an idle memory reaper (`model_reaper`, the
quiet-mode switch) and by `epistemic_graph` to force a re-derivation for
correctness. Only the memory sense changes. Eviction for correctness must keep
clearing it, because there a stale resolver is a wrong answer rather than a slow
one.

## Impact

- Affected specs: `recall-read-path`
- Affected code: `src/exomem/find.py`, `src/exomem/model_reaper.py`,
  `src/exomem/mode.py`

## Out of scope

**The 39 seconds itself.** Removing it means not reading 2,400 files at all —
sourcing `(rel_path, title)` from the lexstore sidecar, whose `pages` table has
`path` but no `title` (only a lowercased `title_lower` inside the FTS index).
That is a sidecar schema change with its own migration. This change makes the
cost rare; it does not make it cheap.

**How often the fallback is taken.** The vault-global availability equality at
`epistemic_graph.py:1133` decides how often a reader lands on this path at all.
#676 correctly puts it last: if the fallback is rare, the fence matters less.
