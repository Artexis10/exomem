## Context

The link projector lives in `record_governance`. A path-shaped link needs no index: it is resolved and
authorized directly. A bare title or a memory reference needs an index of pages, and the projector builds it
lazily, at most once per projector, only when such a value appears.

## Decisions

### Borrow the published generation, never build it

`semantic_contract.borrow_link_snapshot` returns the resolver data and the identity owners of the current
published corpus generation, or `None`. It takes the corpus update lock without blocking, because a
publication holds that lock across a census. It tolerates a pending outside edit, because titles and stems
may answer from the last published generation while existence and authorization stay live. It returns `None`
for every other state, and the projector then takes the existing bounded walk. A request thread never builds
or refreshes a corpus, and an absent corpus is never read as an empty one.

### Visibility is applied at use time, to the whole index

The published corpus is principal-independent. The projector passes a per-request `visible` test to
`normalize_wikilink` for bare links, and filters the owners of a memory id through the same test. The test is
memoised per page for the life of the projector and never emits a release receipt: a receipt claims a release,
so only a link's final target keeps `receipt=True`. This makes the effective index the corpus intersected with
the pages the reader may see, and it keeps that intersection in step with each policy change without a cache
that the policy must invalidate.

### Stale window for memory references

The borrowed generation may predate an outside edit. When a mark covers a visible owner, the projector re-reads
that page's id. An owner whose id changed is dropped, and the reference resolves only when exactly one owner
remains. The projector does not scan the vault to recover, because a scan would put the walk back.

### `normalize_wikilink` step 2

Step 2 tested `cleaned in resolver.kb_stripped`. That set also holds keys of pages outside the Knowledge Base.
The step now tests that `Knowledge Base/<cleaned>` is itself a page, as the targeted branch already did.

The corpus resolver had the same step, and a caller that passes `visible` could reach it. A hidden vault-root
page `Reference/Hidden.md` then resolved `[[Reference/Hidden]]` to a `Knowledge Base/Reference/Hidden.md` that
does not exist, while an absent page left the link unresolved. The step is removed, so a hidden page and an
absent page give the same result.

### Knowledge Base `trash/` on the walk

The identity census prunes the Knowledge Base `trash/` directory, so the published corpus gives a page there no
title or stem, but it keeps the page's identity. The fallback walk reads `trash/` too. It now skips titles and
stems there and keeps identities, so the walk and the borrowed route give the same answer.

### Egress spelling walk

The walk lists each directory once per call. It keeps the entries, so the leaf check reads the entry's file
type instead of making a stat call per reference. A symlink leaf is followed, as before.

## Risks

- A request that races a publication borrows nothing and takes the walk. The walk is correct and bounded, so
  the cost is latency, not disclosure.
- A page that an outside edit has not yet published resolves by its old title or stem until the watcher
  publishes it. This is the existing freshness rule for titles and stems.
- The borrowed route withholds a path-shaped link to a page outside the Knowledge Base and outside the
  curated trees. The cost is a lost link, not disclosure.
- The borrow takes the update lock without blocking, so a query that meets a busy lock takes the walk. In a
  vault over the walk cap, that query withholds its bare titles and memory references.
