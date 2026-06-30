# Design — Thinking-evolution view (`evolution`)

## Context

Supersession is recorded as a clean doubly-linked list in frontmatter (see
`_scaffold/_Schema/references/supersession.md`, enforced by `audit`):

- The **old** page: `status: superseded`, `superseded_by: "[[new]]"`, a refreshed
  `updated:` date, and a body banner `> [!warning] Superseded … on <date>. Reason: <one-line>`.
- The **new** page: `supersedes: "[[old]]"`.
- `replace` writes both pointers atomically and **refuses to supersede an already-superseded
  page** — so a chain is linear (no forks) and traversable from any member.

`find.ParsedPage` already exposes `.superseded_by` (forward) and `.status`; `find._CACHE`
loads pages cheaply; `context_pack._extract_claims(page)` already turns a page into
structural claims (lede + recognized headline-section lines + `##` outline). `attention.py`
is the precedent for a measurement-only "assemble + bound + explicit truncation" surface
reachable from one registry entry.

## The composition

`evolution(vault_root, *, query, limit, scope, projects, tags)`:

1. `find(vault_root, query=query, scope=scope, projects=projects, tags=tags, limit=…)` —
   topic matches (overfetch a few × `limit`, since several hits collapse into one chain).
2. For each hit, **resolve its chain** via `_resolve_chain`: load the page, walk forward
   following `superseded_by` and backward following `supersedes`, loading each via `_CACHE`,
   guarded by a `seen` set (linear chain, but guard cycles defensively). Collect the member
   pages.
3. **Dedup + filter:** key each chain by its active-head path (the member with no
   `superseded_by`); a hit landing anywhere on an already-seen chain is skipped. Drop chains
   with `< 2` versions (a never-superseded note has no evolution to show).
4. **Order** each chain by the pointer spine: origin (no in-set `supersedes`) → follow
   `superseded_by` → head. The pointer order IS chronological; dates annotate each node.
5. Build a `Timeline` of `Version`s and cap to `limit` chains (find-relevance order),
   reporting truncation.

`find()` and the core ranker are untouched; the new logic lives in `evolution.py` + the
`op_evolution` leaf.

## Output

```
{ "query": ...,
  "timelines": [ {
     "chain_id": <active-head rel path>,
     "topic_anchor": <the hit rel path that surfaced this chain>,
     "span": {"from": <oldest date>, "to": <newest date>, "n_versions": k},
     "versions": [ {
        "path", "title", "status", "date",                 # date = page.updated (or created)
        "claims": {"lede", "sections", "outline"},          # context_pack._extract_claims
        "transition": {"reason": <recorded text>, "date": <supersession date>} | null
     }, ... ]                                               # oldest → newest; transition null on head
  }, ... ],
  "truncation": [...] }
```

## Decisions

- **A dedicated `evolution` tool, not a `find` param.** The intent ("how did my thinking on
  X evolve") routes cleanly to a named tool, and the operation **regroups** hits into chains
  and walks pointers — a different output and shape than search. `find` already gained the
  `pack` return-shape param; a second one would muddy it. Costs one always-loaded tool;
  `attention` set that precedent. *(Rejected: `find(evolution=true)` — overloads find;
  `get(path, evolution=true)` — elegant for a known note but forces a find-then-get two-step
  for the topic-driven intent the user chose.)*
- **Transition reason is the RECORDED text, never generated.** The load-bearing
  pure-substrate decision. The reason a version was superseded comes from the old page's
  banner (`Reason: …`) and/or the `why:` in `log.md` at that edit — surfaced verbatim. The
  server never writes "your view shifted because…". Each version's claims are likewise the
  note's own structural text. The reader infers the evolution.
- **Order by the pointer spine, not by date.** `replace` refreshes the old page's `updated:`
  to the supersession date, so dates alone can mis-order; the `supersedes`/`superseded_by`
  links are the ground-truth sequence. Dates are surfaced as labels + the span.
- **Drop length-1 chains.** A note never superseded has no evolution; including it would
  make `evolution` a worse `find`. The tool only surfaces topics whose view actually moved.
- **Chain resolution is frontmatter-only (no vault scan).** Both pointers are written by
  `replace`, so walking is O(chain length) cached reads. A hand-authored note missing a
  backward `supersedes` simply starts the chain there (graceful partial) — acceptable for
  v1, and `audit` already flags missing pointers. No inbound full-vault scan (unlike the
  context-pack neighbourhood) — chains are short and pointer-linked.
- **`.supersedes` reader on `ParsedPage`.** Mirrors `.superseded_by`: returns the
  `supersedes` wikilink(s) (list), empty when absent.
- **Bounded, no silent caps.** `limit` chains (env-overridable default 10), each timeline
  capped (`KB_MCP_EVOLUTION_MAX_VERSIONS`, default 25 — chains are short); every drop adds a
  `truncation` line.

## Pure-substrate justification

Every field is one of: text copied verbatim from a note (each version's claims, the
recorded transition reasons), a supersession edge the user authored (the chain + its
order), or a date the vault recorded. No content is summarized, paraphrased, or judged; no
generative/reasoning model runs; the vault is unchanged and `find` ordering untouched. This
is the same machinery and status as the context pack's structural claims and `attention`'s
composition. An LLM narrating "how your thinking evolved" across the versions would be the
out-of-bounds step; `evolution` stops at ordered assembly and hands the reasoning to the
brain.

## Risks

- **Schema-fidelity fixture regenerated.** Adding a tool breaks
  `tests/test_mcp_schema_fidelity.py` until `tests/fixtures/mcp_tool_schemas.json` is
  regenerated via `scripts/dump-tool-schemas.py`; the diff must add only `evolution`.
- **Tool ambiguity vs `find`.** Both take a query. The `evolution` description leads with
  "how a conclusion CHANGED over time / its supersession history" and notes it returns
  *timelines of superseded→active versions*, not a search, so natural-language selection
  routes correctly. It also returns nothing for topics with no supersession — honestly
  empty, with a note.
- **Partial chains on hand-authored gaps.** A missing `supersedes` truncates the backward
  walk; surfaced as-is (the chain just starts later). `audit` is the place that enforces the
  pointers, not `evolution`.
