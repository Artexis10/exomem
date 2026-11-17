# Design

## Context

`collection_claims.route` is the one router. A compiled-note or Evidence write calls it with the note's terms and facets; a turn calls it with terms only (no facets). `claims.match` declares frontmatter predicates over `type`, `category`, `project` and `tags`; a full match wins `strong`, otherwise shared words decide.
`records_disposition.disposition` turns a `strong` route into `file`, `ask`, `hold` or `append_occurrence` by the effective capture prominence, and only for failure-shaped notes.
`context_task` already projects methods from a source page, binds each to its source version, and resets held methods when the source changes, retires or is withheld.

The incident this answers: a failure note whose project was dropped on write lost its route to a wider, undeclared collection, so no disposition fired and nothing pointed at the owner's capture procedure.

## Goals / Non-Goals

Goals: a declaration orders candidates before words do; a declared route is acted on; a collection can point at its capture method; the method reaches the agent on the write receipt and on the turn.

Non-goals: a workflow store, a scheduler, a new retrieval lane, a catalogue of domain workflows, widening the unreflected-observation family beyond what it already admits, and any change to how a write derives its `project` facet.

## Decisions

### D1. A declaration decides; words only rank what no declaration settles

Three tiers, in order.

1. Every `claims.match` predicate holds: `strong`, as before.
2. Otherwise, among collections the page does not contradict, one that holds some predicates and is itself a valid word route ranks ahead of every collection that holds none: by predicates held, then coverage, then name. A tie on predicates held and coverage stays silent. A partial match never becomes `strong`.
3. Otherwise coverage alone decides, unchanged.

A declaration is evidence about a page only together with a word route. A single predicate such as `type: [failure]` is shared by many collections and says nothing about which one a page belongs to, so tier 2 still needs the coverage floor and the subject signal. Without that gate a one-predicate declaration would capture every page of its type.
Turn routing passes no facets, holds no predicate, and so takes tier 3 exactly as before.

Alternatives weighed: counting predicates held as a coverage bonus (it mixes two kinds of evidence and moves the floor), and promoting a partial match to `strong` (it would file records on partial evidence).

### D2. The declaration, not the page's type word, lifts the failure-shape gate

A `strong` route that carries `matched_predicates` was chosen by the owner's own declaration, so it is acted on whatever the note's type. Any other `strong` route, which means a natural-key anchor, keeps the failure-shaped gate. `FAILURE_SHAPES` is a vocabulary kept in code; it stays only for the undeclared case, marked as old debt, and is not extended.
The instruction that files no longer says "recurring incident", because the collection is named by the payload.
The unreflected-observation family needed no change: it routes through the same function and its entry carries no type check, so an edit of a declared note re-offers its disposition while its entry is open and untriaged, as for any other route.

### D3. The pointer lives on the collection and is resolved for the caller at delivery

The collection is the destination the router already resolves, so it carries the method reference, and the manifest stores it as the author wrote it. Parsing a manifest is far more frequent than a route to it, and a page's existence is not the manifest's validity, so parsing does not resolve it.
Resolution is a separate step for the caller asking. It uses the existing link resolver, confines the result to the vault, and takes the page's identity and title through the existing identity reader. Visibility is decided by the page's own release decision for that caller; a page the caller cannot read and a page that does not exist return the same nothing, so neither the advisory, the inspection finding nor the turn packet shows that a withheld page exists.
`due_state.routing_targets` carries the raw reference, not a resolved method. Resolving there would run once per reconcile for no caller and would decide visibility for nobody. The write resolves the winning collection's method once, for its own caller.
A failure to resolve costs the advisory its `method` and nothing else.

Reuse check: the closest option is the method projection plus the link resolver plus `claims`. The gap is only a manifest pointer and its delivery. No maintained library covers it, and the added upkeep is one manifest key, one resolver module of about 90 lines and two delivery sites.

### D4. The write advisory names the method in a validated key and one instruction clause

`records_routing.method` is `{title, read_ref}` and rides on `strong` and `moderate` routes. The compact terminal validates it like its other keys: exactly those two fields, bounded, and a malformed `method` drops the whole advisory, as every other malformed key does. A `file`, `append_occurrence` or `ask` instruction appends one clause telling the agent to read `method.read_ref` and follow it when it acts; `hold` acts on nothing and omits it. The clause has to fit the existing instruction bound, and a disposition that no longer fits drops itself, not the route.

### D5. The turn serves the declared page through the existing method kinds

For a collection-manifest lead that declares a method, `context_task` projects the declared page by its own release decision, as it does a lead. A skill page becomes a skill item; a page that lists methods serves those; any other page becomes one `read_required` item whose `read_ref` is the page's path, as for any served method until its approval verdict is `approved`. The item carries the declared page's own source version, so a revision, retirement or withholding resets the method through the existing generic reset, and `_revalidate` and the guarded release treat a page served whole as holding its own ref.
The declaration is read from the manifest as released to the caller. The declared page is the page's own to release: a title reference is visibility-filtered by the resolver and a path reference reaches the page's release decision in the projection, and the terminal guarded recheck covers both.
A page that is both a lead and a declared method is served once (by object identity, since a copy that repeats an id must still inherit no approval).
Cost: at most one source projection per declaring lead, none for a lead that declares nothing. A path or an Exomem reference needs no resolver. A title, a stem or a short page id goes through the broad resolver only when it is already resident (`find.borrow_query_resolver`, which never builds it and schedules the build on a miss only when a watcher owner exists, as `working_set` decides), because building it reads the whole vault: a cold resolver costs that turn the method, and the block reports the gap as `lane_failed`, which makes it `partial` instead of `ready`. The miss comes before any page is looked up, so it names no withheld page. A failure on the page itself is not reported, because for a path an error would show that a withheld page exists. Guidance recommends the `exomem://` ref form, which needs no resolver and survives a rename or a move. The method is advisory, so a failure on its page costs the method and never the task context. The deadline is checked before each lookup, and one that fires reports the packet partial with the existing `budget` missing reason.

Boundary: removing the declaration while the method page stays unchanged does not reset a method a client holds, because that source is unchanged and the item's own source version still validates. It resets on the page's next revision.

## Risks / Trade-offs

- A provisional coverage floor and the failure-shape vocabulary stay as they are; this change adds no constant of either kind.
- A title reference breaks when the page is renamed; the resolved `read_ref` is the stable reference, and the finding tells the owner when a declaration no longer resolves.
- Inspection reports the finding, validation does not, so a bad reference shows up where the collection is inspected and not at the write that declared it. Resolution at validation would make a manifest depend on a page that can legitimately appear later.

## Migration Plan

Additive and optional. A manifest without `capture` behaves as before, and a vault without partial declarations routes as before. No data migration. The shipped skill scaffold and its generated plugin copies carry one documentation paragraph and one capture-skill sentence.
