## Context

`egress.filter_withheld_entries` is the terminal entry filter every dispatcher result passes. It decided the fields it enumerated (`path`, `target`, `id`, the mutation vocabulary) and dropped an entry naming a withheld page. Derived graph and review structures use other names for the same thing, and some compute counts and caps before any decision. A filter at the end cannot repair a number or a cap, and cannot see an identifier in a field it does not read.

## Goals / Non-Goals

**Goals:**

- A restricted caller's derived answers read as if withheld pages were absent, measured by twin vaults.
- The owner's answers and costs do not change.
- A write by a restricted writer neither resolves onto nor changes a page it may not see.

**Non-Goals:**

- Change any tool schema or description.
- Change the owner's view under any policy.

## Decisions

### Identifier fields are enumerated, with a suffix rule for new ones

The entry filter decides the listed fields and any key ending in `_path`, `_key`, `_anchor`, `_source`, `_target` or `_ref`. A non-path value in such a field is never decided, so the rule costs nothing where it does not apply, and a new derived field fails closed on arrival. A `file:` node key and a vault URI are unwrapped; an extensionless reference is also decided as the page it names. A proposed relation `bullet` is scanned for wikilinks; an entry whose text links a withheld page is dropped rather than rewritten, because the text is the proposal. The filter decides each page once per call, and lists each directory it resolves a spelling in once per call, so a payload that names the same pages in many fields costs no more than one that names them once. A `context` unit seed that resolves to no unit is not restated in `seed` to a caller other than the owner: the filter decides the page a reference names, so the restated reference would survive for an absent page and not for a withheld one.

### Decide before assembly, for callers other than the owner

`restricted_release_filter` returns `None` for the owner and on an ungoverned vault, and the per-page release predicate otherwise. Derived structures take it and apply it where they choose candidates: a graph walk treats a withheld page as excluded (never a seed, hop, endpoint or edge author), timelines treat a pointer to a withheld page as a pointer to nothing, entity lookups decide matches before the status, and listings collapse a folder that holds only withheld files. `visible_page_filter` keeps a reference that names no file (an unresolved link, a placeholder), because nothing can be withheld there and the absent twin keeps it too. The owner's calls keep their original shape.

### Writer links resolve over the writer's view

`vault.writer_link_visibility` is the same predicate for writes. `normalize_wikilink` and the corpus resolution used by the capture sweep accept it and restrict stem, title and path matches to it. A path naming no file (the writer's pending page, a forward reference) is unaffected. The semantic contract judges a candidate page with the same predicate for that page's own relation targets (`SemanticCorpusContext.with_candidate`), so a relation only a withheld page answers is unresolved there too; every other page keeps its whole-vault resolution.

### Write doors decide their target first

`write_target_withheld` is true only for an existing file the writer may not see. Each door checks it at the point where it already answers a missing target, and raises that same answer, so the refusal is the absent refusal by construction.

A folder can hold pages the writer may not see, and every answer about deleting it (counts, refusals, what was trashed) moves with them, so a restricted writer's folder delete is refused with one `AUDIENCE_RESTRICTED` answer whatever the folder holds; a declared recursive delete is refused before anything is read. What it prevents: a delete that reports on, or trashes, withheld pages. Cost when it fires wrongly: a restricted writer asks the owner to remove a folder; that writer pays. A creation door whose destination a withheld page occupies refuses it with its ordinary occupied-destination answer (`FILE_EXISTS`, `DEST_EXISTS`, `ENTITY_EXISTS`), naming no path the writer did not supply, and never overwrites it. A move with `update_wikilinks` rewrites every linking page, withheld ones included, so the vault stays consistent, and reports counts and touched paths for the linking pages the writer may see; the activity log keeps the full figures.

### Link resolution follows the reader's view

The graph resolves each wikilink once, over the whole vault, and stores only the result. No stored-schema change is needed to re-resolve it: the dependency index already records every page's raw link targets and their lookup keys. For a reader other than the owner, a per-request view re-resolves lazily and only where a request looks: for each page a graph walk touches, and for each page whose recorded targets name one of those pages, a target whose whole-vault candidates (full path, stem, title) include a page the reader may not see is resolved again with those candidates removed, and that page's link edges are re-derived over the reader's view. Only those candidates are decided. The owner never builds a view.

The same view applies where a read surface resolves links itself: inbound links count a bare link when the target's basename is unique among visible pages, and pack neighbours resolve and are decided before ranking and the cap. The page provenance strip never removes a bare link: whether a visible page, a withheld page or no page answers it, it is listed as the reader would see it in a vault without the withheld pages, and the page body already shows it. A path, or a link that carries a folder, names a location and is still removed when that location is withheld.

### Activation resolves over the caller's view

The activation index counts names, aliases and name terms over every anchor at build time; a withheld anchor can make a visible name ambiguous, a turn word common, or retire a derived short name. For a caller other than the owner, `working_set_resolve.audience_view` decides only the anchors the turn's words reach, counts a name term's visible owners until it is common among them, restores a derived short name that only a withheld anchor retired, and drops the anchors decided withheld before evidence is assembled; an anchor reached by other evidence is decided before resolution. No per-audience table is stored, and the decisions stay within the activation path's filesystem ceilings. Such a packet is not cached: keying the cache by audience and purpose would be a second copy of the release plane, and purpose never enters a cache key.

The guard omits L0 material silently, as `LEVEL_NONE` defines: a `withheld` marker, or a `withheld` abstention, tells the caller that something it may not know of exists. Material released at a notice level keeps its markers. A packet whose every anchor the guard removed at L0 abstains as `unresolved`. The vault freshness key counts and digests every file, and the index generation advances on every write, so a restricted caller receives neither; its continuity token carries generation 0, which continuity never compares.

### Counts and ranks follow filtering; whole-vault aggregates are the owner's

Review queues (attention, activation, relation debt, stale, contradiction) decide each finding's page and related pages before fusion, so ranks, scores, totals and summaries are computed over what the caller receives. `inbound-links` counts the links it lists after deciding their source pages.

### Relation review is the owner's under a governed policy

Relation proposals pair pages over link resolution and unit relations across the whole vault, and the relation queue ranks and counts them across every eligible page. Under a governed policy `suggest-relations`, the relation queue, and triage or acceptance of a relation candidate are served to the owner only; another audience receives the `audience_restricted` refusal (`AUDIENCE_RESTRICTED` for an action), decided from the principal and the policy before any page or candidate is read, so the answer is the same whatever page or reference is named. The per-reader candidate decisions built for these generators stay in place for the owner/remote audience split and are not reached by another audience. What it prevents: proposals, ranks and totals that move with pages the caller may not see. Cost when it fires wrongly: a restricted caller cannot review relations, which is owner work; that caller pays, and the owner never does.

A whole-vault aggregate cannot be recomputed from a filtered result: an audit, a registry inferred from the corpus (directly, or as the corpus side of a diff), the activation coverage block and the relation queue's coverage block. Under a governed policy these are served to the owner only, as the relation census is (`egress.owner_only_aggregate`); every other bound audience receives `available: false` and `reason: "audience_restricted"`, decided from the principal and the policy before anything is read.

Recall diagnostics are computed over the whole corpus before release decisions: lane statuses, fusion weights, raw scores and the emit count (`explain`), per-lane ranks and graph in-degree (`signals`), and the keyword-fallback marker (`degraded`). A restricted caller does not receive them; its hits and their order are unchanged. The BM25 IDF and fusion-order residual among visible hits is a known limit.

Recall runs without the graph lane and graph enrichment for such a caller (`ask_memory`, a `context` query and evolution timelines): graph hops, in-degree and enrichment follow link resolution over the whole vault, and the recall cache shared across principals is keyed by that switch, so the decision is made before the cache is consulted. The owner's recall is unchanged. What it prevents: hits whose presence depends on how a withheld page resolves a visible link. Cost when it fires wrongly: a restricted caller recalls without hop neighbours; that caller pays, and the owner never does.

### Controls

- Dropping an entry, collapsing a folder, and answering a withheld write target as missing. What they prevent: a restricted caller learning that a withheld page exists, what it is called, or what it links, and a restricted writer changing it. Cost when they fire wrongly: a restricted caller misses an entry or cannot write a page it could not read either; that caller pays. The owner never pays: every one of these is `None` or `False` for the owner.
- Entity creation when the only match is withheld proceeds, and the owner reconciles the duplicate later. That trade-off is accepted: the alternative answers differently for a withheld entity and an absent one.
- The `audience_restricted` refusal of whole-vault aggregates. What it prevents: audit findings, inferred counts, denominators and coverage that move with pages the caller may not see. Cost when it fires wrongly: a restricted caller receives no aggregate, which is owner work under a governed policy. That caller pays; the owner and every caller under an empty policy are served as before.
- Hiding recall diagnostics from a restricted caller. What it prevents: lane and rank numbers that reveal whether a withheld page matched. Cost when it fires wrongly: a restricted caller cannot inspect ranking; the hits are unchanged. The owner keeps every diagnostic.
- Omitting L0 material without a marker, and abstaining as `unresolved`. What it prevents: an existence signal for material the caller may not know of. Cost when it fires wrongly: a restricted caller is not told that a section lost something; that caller pays, and material at a notice level is still reported. The owner never pays.
- Not returning the vault freshness key or the index generation to a restricted caller, and not caching its packets. What they prevent: a file count, digest and write counter that move with withheld pages, and one audience's resolution served to another. Cost: a restricted client loses a diagnostic stamp and repeated identical turns are compiled again. That caller pays; the owner's packet and cache are unchanged.
- A call no surface bound is not decided by these filters, as the owner is not. Every surface binds a principal before the dispatcher, whose entry filter still decides what an unbound call may read.

## Risks / Trade-offs

- The activation guard decides every page that bears a name a unit's prose uses, and drops the unit when any of them is withheld, although a vault without the withheld page would serve it. Per-audience name tables are deferred to the owner/remote audience split; `test_residual_prose_naming_a_page_a_withheld_page_shares_a_name_with_is_dropped` pins the current answer.
- A creation door reveals that a path is occupied by refusing it, and `remember` gives a new page a suffixed path when a withheld page holds its slug. That is inherent to writing without changing the withheld page; `test_a_withheld_occupant_is_refused_as_any_occupied_path` pins the refusal.
- A move whose link rewrite would change a page in an append-only tree is refused even when that linking page is withheld from the writer, so the refusal shows that such a page links the moved one; the page is not changed.
