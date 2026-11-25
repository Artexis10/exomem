## Context

`capture_sweep.undeclared_referents` runs after a committed durable page write. It reads the written page's parsed state and the preflight corpus, lists up to 3 wikilinked names that resolve to nothing, and rides the post-batch carrier (`commands._carrying_batch_advisories`) into the compact response. The same seam builds the Entity registry index (path, title, aliases, status) from the preflight corpus's pages, once per write.

The census in the proposal shows the other gap: a note names an existing Entity in prose and never links it. The agent is the semantic decider in this system. The server can still do the deterministic part, finding exact names that the vault's own Entities answer to, and hand the agent a decision that costs one call.

## Goals / Non-Goals

Goals:

- An agent that writes a note naming an existing Entity in plain text learns it in the same response, with a ready call that links it.
- The offer is cheap enough to run on every durable page write at the scale of the largest vaults, with no vault walk and no model call.
- A restricted writer learns nothing about Entities it may not see.

Non-goals:

- The server never writes a link by itself.
- No derived connection is recorded for an unlinked name in this change. The owner chose to also keep a server-derived `mentions` connection for every exact, unambiguous match; a separate change adds it (see Follow-up).
- No fuzzy matching, stemming, inflection or translation. A name the agent spells differently is not offered.

## Decisions

### D1. Read prose, but only for names the vault defines

`undeclared_referents` reads wikilink markup only, so that the server never guesses which prose is a name. This block reads prose, but it looks only for the titles and aliases of the writer's own active Entities. The set of names is data: the vault defines it, and an owner adds a name by creating or editing an Entity. The output is an offer that the agent accepts or ignores; nothing reads the match as meaning. This keeps it outside the unsound patterns (registry C6): no list in code decides what a person meant.

### D2. The match rule

1. Build a name index for the write, from the Entity registry entries that the write's preflight corpus already yields to `undeclared_referents` (`capture_sweep._registry_index`, built once per write): each active, writer-visible Entity's title and aliases, normalised with the existing `entity_candidates.identity_key` (NFKC, casefold, collapsed spaces), as word runs. A key that two or more Entities share is ambiguous.
2. Scan the page body with the regions that are not prose masked out: the Markdown parser's code (fenced and inline), links, images, HTML and headings (`markdown_regions.inert_spans`), existing wikilinks (`vault.find_body_wikilinks`), and any word that has the shape of a URL. Frontmatter is not part of the body. At each whitespace-delimited word, take the longest run of words that is a key, allowing a few punctuation characters at the run's outer edges; a name never spans a line break, and matches do not overlap.
3. Drop ambiguous keys, the page's own Entity (a page never offers a link to itself), and any Entity the page already links anywhere in its body or `## Relations`.
4. "New" is decided against the page's pre-write state, as for `entity_candidate`: an Entity is offered only when the write raised its count of unlinked mentions. Editing an old note does not re-offer names it already held.
5. Keep at most 5 Entities, preferring longer names (more words, then more characters), then first position in the page. Dismissed Entities and a quieted family are filtered before the limit, as for `undeclared_referents`.

### D3. The ready call

The block carries one `edit_note` call with `action: "replace_text"` and one `edits` item per listed Entity. Each item replaces the first unlinked mention with `[[<entity link target>|<name as written>]]`. `replace_text` refuses an `old_text` that occurs more than once, so `old_text` is the shortest span around the mention, extended a word at a time, that occurs once in the page as written; the span stays inside the mention's line and between the masked regions around it, and is at most 400 characters. Items run from the last mention to the first and each is counted against the page as the items before it leave it, so no item finds its text moved or doubled by another. An Entity whose mention has no such span is not listed, so the call links exactly the Entities its block lists. The agent removes the items it does not mean before calling. If the page changed in between, an `old_text` that no longer occurs exactly once refuses like any other stale `replace_text`, and the commit applies all kept items or none. The call is spelled for the serving surface, like every other `next_action`.

### D4. Gates, dismissal and batches follow `undeclared_referents`

The block is withheld when `structural_suggestions` is `off` or `proactive_capture` resolves to `off`. Its family, `unlinked_mentions`, is quietable. A write-advisory fingerprint dismisses one Entity for one page. Inside a mutation batch the page seam holds each page's group, replacing an earlier hold for that page, and the post-batch carrier lists whole pages in commit order until 5 Entities are listed; a page is never cut, because its edits were counted against one another. The compact response projection carries the block.

The block groups by page, because a batch commits several pages and an Entity is listed once per page: `{"pages": [{"note", "mentions": [{"name", "link", "review", "fingerprint"}], "call"}]}`. `review` and `fingerprint` are the write-advisory handle that dismisses one Entity for that page, the same mechanism `undeclared_referents` uses.

### D5. Disclosure

The index is built from the writer-visible registry only. A name that matches only a withheld Entity is never offered, so a restricted writer sees the same block whether that Entity exists or not.

### D6. Cost

The write already builds the registry index from the preflight corpus's pages for `undeclared_referents` and `entity_candidate`, once per write. The name index is built from those entries in one pass, also once per write, so no checkpoint cache is needed and there is no cold state: a write whose corpus has no registry yields no block. The scan is one dictionary lookup per word per candidate run length, bounded by the longest key in words. No vault walk, no catalogue query, no model call. A fault yields no block and never fails the write. The cost is linear in the Entities the writer can see and of the same order as the registry build it follows (measured on generated Entities: 13 ms against 53 to 63 ms at 10,000, 214 ms against 330 ms at 50,000); a cache shared across writes and keyed on the corpus checkpoint would remove both and is not part of this change.

## Risks / Trade-offs

- Short or common Entity names (a two-letter nickname, a name that is also an everyday word) will sometimes match an unrelated word. The offer costs one listed line, the agent decides, and dismissal is per page. The development round measures whether false offers train agents to ignore the block.
- An Entity created after a note was written is not offered for that note until the note is next edited with a new mention. Backfilling old notes is a separate, owner-approved repair.

## Follow-up

- Derived `mentions` connections: the owner chose to keep a labelled connection in the store for every exact, unambiguous match, written in the same commit as the page and without editing the note's text, so that no connection is lost when the agent declines or ignores the offer. A separate change adds it on `epistemic-graph` and reuses this change's name index. It is separate because it changes how the graph is derived, which the store work also changes.

## Proof

- Unit tests over the matcher's branches: title, alias, longest match, ambiguous key, own page, already linked, masked regions, Unicode names, pre-write comparison.
- One workflow test through the MCP door: a note naming two existing Entities and one ambiguous name returns the block with two Entities; the ready call commits both links and nothing else; a restricted writer's block is identical with and without a withheld Entity of the same name.
- Before release, one development round on real models (task 3.1) compares the share of existing-Entity names linked with and without the block, on cases that an evaluation lane writes, at one draw.
