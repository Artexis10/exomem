## Context

`complete-recurring-entity-lifecycle` built the candidate lifecycle: bounded promotion and
hydration candidates, ambiguity stops, resolve-before-create, state-resolved closure, and
a once-per-session read of the `entity_recurrence` family at the balanced and maximal
levels. Its evidence comes from two lanes: unresolved wikilinks and a closed sentence
grammar. On a real vault both are nearly empty (see the proposal), so the lifecycle idles.

This change feeds the wikilink lane and delivers its candidates sooner. It leaves the
lifecycle, the grammar lane and the tool surface alone.

A first draft proposed an optional `mentions` argument stored as frontmatter. An
independent critique (2026-09-19) showed that the alternative it dismissed in two
sentences, wikilinks, is already implemented end to end, including the rebuild trigger
the draft lacked. Decision 1 records that.

## Goals / Non-Goals

**Goals**

- An identity a note names reaches the graph on the write that names it when its page
  exists, and becomes a promotion candidate when a second page names it.
- An identity a note is about becomes an Entity in the same turn, by the agent's
  decision, without waiting to recur.
- Works for any domain, entity type and language, because no server component reads
  prose for meaning.
- Reaches the agent without a user nudge on clients with no hook or skill.

**Non-Goals**

- The server extracting names from text.
- Automatic Entity creation. A wrong Entity is a durable page that misleads recall and
  the compiler; a missed one costs a later candidate.
- A queue of pages that "lack declarations". A page that links nothing may simply name
  nothing, so no such queue can be derived without reading prose.

## Decisions

### 1. The mark is a wikilink, not a new argument

Both designs depend on the same unproven behaviour: the agent marking the identities it
names. They differ in what already exists.

| | wikilink | `mentions` argument (withdrawn) |
|---|---|---|
| agent already knows the affordance | yes | no |
| visible in the owner's editor, in place | yes, as an unresolved node | a frontmatter list |
| cross-page counting | shipped (`entity_recurrence` wikilink lane) | new derived count |
| edges appear when the page is created later | shipped (graph dependency index re-derives the linking pages) | needed a new trigger, absent from the draft |
| page-less links reported on the write response | shipped (`capture_sweep` hint) | new carrier |
| tool-surface change | none | one argument, one operation |
| new egress surface | none; a link is body text, governed as body text | personal names in a frontmatter block released wholesale |

What the wikilink cannot carry is a type cue and a "this note is about it" flag. Neither
needs a channel: the agent supplies the type when it creates the Entity, and an identity
a note is about is handled by decision 2.

If, some weeks after release, the measured wikilink yield is still near zero, a
declaration argument returns as its own change with that measurement as its case.

### 2. Doctrine, and where it is carried

> When you write something durable, wikilink the people, organisations, places, equipment
> and products it names, whether or not a page exists yet. When the note is about an
> identity that has no Entity, resolve it and create the Entity in the same turn, within
> your confirmation rules. When a write returns `entity_candidate`, resolve before you
> create and hydrate an existing Entity before you make a second one. A passing name
> needs no link.

Carried in four places, because no single one reaches every client: bootstrap guidance
at `balanced` and `maximal`; the scaffold skill; the `body` argument description of
`remember` and `replace_memory` (the only text every connected client is certain to
read), in one sentence; and the capture texts that today forbid exactly the
first-mention case the owner asked for. The capture hook allows `create-entity` "only
for a stable recurring identity useful beyond this source"; the capture skill and the
operations reference, each shipped in the scaffold and in the plugin, require an
identity to be "stable, recurring, central". All six files change to "stable, and
central or recurring", and each source and packaged pair stays byte-identical.

This is compatible with the creation criterion `complete-recurring-entity-lifecycle`
teaches (create only when no active Entity resolves and the agent judges the identity
stable, reusable and useful). A count never replaces that judgement; it prompts it.

### 3. The wikilink lane fires on two pages

`SPREAD_MIN_PAGES` for the wikilink lane becomes 2. The grammar lane's three gates are
untouched: its measured population has one identity on two pages, so nothing is gained,
and its gates carry their own precision argument.

What two prevents compared with three: on the measured vault, four identities sit on
exactly two pages and would wait indefinitely for a third. What it costs when it fires
wrongly: one candidate the agent declines or the owner dismisses through
`triage_memory`; the family can also be set quiet. Who pays: the agent, a few hundred
bytes, once.

Independence stays what the lane already uses: distinct eligible pages. Two notes written
in one conversation about one linked name are two pages and do fire. That is accepted:
an agent that deliberately links a page-less name in two separate notes has said twice
that it is a thing. A session stamp that could collapse them does not exist on written
pages today and cannot exist on stateless HTTP, so the guard would cost a frontmatter
field and a degraded mode to prevent a cheap, dismissible prompt.

### 4. Navigation pages are not evidence

Pages named `index.md` or `log.md` do not supply spread and never anchor a finding. The
product already has the predicate: `find_corpus.NAVIGATION_BASENAMES`, which recall, the
lexical store and the audits use to set these pages aside. `log.md` is the vault's own
activity log, not collection storage. They list things, they do not reach for them. On the measured vault this removes ten of 34 identities and
both surfaced candidates, all noise.

### 5. The candidate rides the write that creates it

When a committed durable write adds a body wikilink to a bare name that resolves to no
page and no active Entity, the server looks the name up in the graph's link-dependency
index and keeps the linking pages that are eligible evidence (`counts_as_evidence` and
not a navigation page). If this write takes that set from one page to two, the committed
response carries `entity_candidate`: at most three identities, each with its name, at
most eight linking pages, near matches from the registry, and the routes to
`resolve-entity` and `create-entity`.

The block measures its own quantity, and the audit family stays authoritative. The
dependency index is keyed by a link's casefolded target spelling, and yields the bare
name as a key only when the target has no folder or is exactly the knowledge-base folder
plus the name; the audit lane keys on the NFKC-normalised, whitespace-collapsed
basename. So the block sees pages that wrote the same bare name and misses a page that
wrote a deeper folder-qualified target (`Notes/People/Harbour Studio`), a differently
composed accent or a doubled space. On spelling it can only undercount, and an undercount
costs a block that does not fire for an identity the family still holds. It applies the
family's page-level exclusions itself, because the index knows none of them: ineligible
evidence, navigation pages, pages in the `Entities` subtree (Entity pages link each other
as a matter of form), a page whose own title or stem is the name, and a suffixed name
that stands on a real file. The two can still diverge in either direction, which is why
the family is the authority and the block an early notice. Making the index answer the audit's
question would need a second dependency row per link and a dependency-format bump, which
fails the index's coverage proof until every vault runs one full graph rebuild; that is
too much migration for an advisory, and is recorded as the alternative.

Eligibility is evaluated for the rows the lookup returned, at most sixteen, through the
page state the write's preflight already holds. This is also what keeps the block from
naming a page in an excluded access tier: raw dependency rows carry no tier.

- It fires on the transition only: a page that already linked the identity, or an
  identity already at or past the gate, produces no block. The fact is derived from
  vault state, so it is the same for every caller and needs no ledger, which matters
  because remote HTTP is stateless and a per-caller ledger there either repeats or
  never fires.
- Pages committed inside one mutation batch count once for the block, as the capture
  sweep already treats a multi-write command as one episode. An identity whose second
  page arrives inside such a batch therefore never gets a block, then or later, because
  it is already at the gate when the next write comes; it is in the family.
- It is a `structural_suggestions` disclosure and is withheld when that class is `off`.
- It is sent only when the graph index answers the lookup as `available`. `derived_sync`
  says nothing about the graph, which converges on its own; on `warming`,
  `temporarily_unavailable` or a quarantined graph no block is sent. The candidate is
  still in the `entity_recurrence` family, which balanced and maximal agents read once
  per session.
- It travels through the mutation terminal like `structure_suggestion` and
  `records_routing`: kept at compact detail, dropped at `legacy`.

What the carrier prevents: a candidate waiting in a family that is read once per
session, three at a time, by whichever agent happens to be there, instead of reaching
the agent that holds the context. What it costs when it fires wrongly: `create-entity`
is confirm-required, so an agent that acts on the block may ask the owner one question
mid-task. Who pays: the owner. That cost is why the block fires once, on the transition,
and why the doctrine says a passing name needs no link.

### 6. Backlog

Existing prose stays unlinked until someone links it. An owner-started agent sweep does
that with the tools that exist: read a folder's pages, add links with `edit_memory`. It
costs model tokens, so the owner chooses its size. The pages that already link something
are measured at no cost the moment the gate moves.

## Risks / Trade-offs

- **Agents do not link.** Mitigation: four carriers, the block that rewards linking with
  an immediate result, and the measurement named in decision 1.
- **Unresolved links clutter the editor.** They are the editor's native way of showing a
  page that should exist, and they resolve when the Entity is created.
- **Two lanes, two gates.** The wikilink lane fires at two, the grammar lane at three.
  Stated in the spec so the difference is a decision and not drift.
- **Ambiguous names.** A link resolves by the editor's own rules; a candidate lists near
  matches and the lifecycle's ambiguity stop applies unchanged.

## Migration

None. A few more candidates appear at once on existing vaults and index-page noise
disappears. The recurrence requirement loses its blanket "no write-time work" clause:
the audit sweep still does none, and the one write-time use, the candidate block, is
bounded by a single indexed lookup and at most sixteen page-state reads, measured by
the write-latency task.

## Open Questions

- Whether the wikilink gate should be vault-owned. Deferred: the owner's controls today
  are the family's disposition and the `structural_suggestions` class, and the number is
  the owner's own.
