## Context

`audit.audit` parses the whole Knowledge Base in `_parse_all` before any stage runs, unless the selection is a projection check, an entity-recurrence sweep or, since #2995, `source_pointer_drift` alone. `_parse_all` admits each page through `egress.content_permits` and the release filter, then parses it. The custody check (`_check_unprocessed_sources` with `families={custody_source}`) then skips every page that is not a captured Source with `custody_only: true`, and looks up the derivatives and predecessors a Source names in a dict of the parsed pages.

A cProfile run of a custody-only audit on a text snapshot of a personal vault (17,399 pages, 6 custody-marked Sources, no budget) took 88.8 s. `_parse_all` took 78.6 s: `content_permits` 59.0 s (the document-container parent check reads each page twice), page parsing 16.3 s. The wikilink resolver snapshot took 9.9 s cold. Coverage evaluation of the 6 Sources took well under a second beyond the resolver.

## Decisions

### D1. Choose custody candidates from frontmatter before authorization

For a selection of exactly `{custody_source}`, `_parse_all` takes a selector that runs before admission. It drops a page only when it has read the page's frontmatter and the frontmatter does not record `custody_only: true`. It reads only the fenced frontmatter block from the file, or the stored body in a store-served vault, and parses it as the full parse does. A page it cannot decide (unreadable, unterminated or over the header bound) stays in. It applies to every corpus page, not only the append-only tree, because the custody check selects captured Sources by type role, wherever they are. Admission and the full parse then run for the few pages that pass, as they do today.

Rejected:
- An append-only parse, as #2995 does for pointer drift. It still admits and parses about 14,100 pages on the measured vault, so it still exhausts the allowance.
- A catalogue or index column for `custody_only`. It would need a schema change and a backfill, and a stale projection would hide a custody Source. Reading frontmatter needs neither.
- Making `content_permits` cheaper. It would help every audit, but it changes an authorization path for all callers and is a separate risk surface.

Reading a page's frontmatter before authorization discloses nothing: the selector only decides which pages to admit, and a page that fails admission contributes no finding, count or name, exactly as today.

### D2. Read named pages on demand

With the narrowed page set, the dict lookup would report every derivative as missing and list a compiled custody Source as uncompiled. The custody-only path therefore passes a lookup that admits and parses one named path when coverage evaluation asks for it, caches the result for the call, and returns `None` for a page that the walk would not reach, that admission refuses or that is a hidden part page. This matches what the full parse would have given for that path. The lookup checks the request budget, so a truncated lookup makes the custody stage's coverage `unknown`, not complete.

### D3. Leave other selections alone

The selector applies only to exactly `{custody_source}`. `unprocessed_source` needs every captured Source, and a mixed selection needs the corpus for its other checks. Neither changes.

## Risks

- A Source whose frontmatter holds invalid YAML or is not a mapping is dropped by the selector. The full parse would also give it no `custody_only` field, so neither path reports it as a custody Source.
- Expected cost on the measured vault: frontmatter reads of every corpus page, the cold resolver, and a handful of admissions and parses, well inside the 50 s allowance. The proof task measures it.
