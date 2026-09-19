## Context

`complete-recurring-entity-lifecycle` built the candidate lifecycle: bounded promotion and
hydration candidates, ambiguity stops, resolve-before-create, state-resolved closure. Its
evidence source is a closed sentence grammar over page bodies. On a real vault that source
is nearly empty (see the proposal's measurement), so the lifecycle it feeds is idle.

This change adds a second evidence source and leaves the lifecycle alone.

## Goals / Non-Goals

**Goals**

- An identity a note names reaches the graph on the write that names it, when an Entity
  exists, and becomes a promotion candidate on its second independent mention, or its
  first when central.
- Works for any domain, entity type and language, because no server component reads
  prose.
- Reaches the agent on every client without a hook, a skill or a user nudge: the tool
  schema asks, and the write response answers.

**Non-Goals**

- The server extracting names from text. Rejected on constitution (the server measures)
  and on evidence (a closed grammar found 30 identities in 4,100 pages; an open one needs
  a model).
- Automatic Entity creation. A wrong Entity is a durable page that misleads recall and
  the compiler; a missed one costs a later candidate. The agent decides.

## Decisions

### 1. The declaration is an argument, stored as frontmatter

`mentions` is a list of at most 24 items. An item is a string (the name) or a mapping
`{name, type?, central?}`. Names are 1 to 96 characters after trimming; `type` is a key or
alias the entity-type registry resolves, else it is kept as an unresolved type cue and
reported; `central` is a boolean, default false.

Stored form on the page:

```yaml
mentions:
  - Field Recorder
  - {name: Harbour Studio, type: organisation, central: true}
```

Frontmatter, not a sidecar, because the vault is the source of truth and every derived
index must be rebuildable from it. An owner editing the list by hand in their editor is a
first-class path.

What the caps prevent: an unbounded list turning one write into an unbounded number of
registry resolutions under the mutation boundary. What they cost when they bind: items
past the cap are dropped from the declaration and named in the response. Who pays: the
agent, which can split the note.

*Alternative considered:* inferring mentions from wikilinks. Rejected as the only source:
agents link pages that exist, and the identities this change is after are exactly the
ones with no page yet. A wikilink to an Entity page already produces its edge today.

### 2. Resolution at write time reuses the entity resolver, exact and alias only

Each name goes through the resolver `connect_memory(operation="resolve-entity")` uses,
restricted to exact-name and alias matches. One match: an edge. No match: counted as
unresolved. More than one: no edge, reported as ambiguous with the competing refs, and
not counted, because two people sharing a first name must not merge into one candidate.

Edges carry a new origin `declared_mention` so they are distinguishable from authored
semantic relations, are rebuilt from frontmatter like any derived edge, and disappear
when the declaration does.

Resolution failure never fails the write. The note is the durable thing; the declaration
is an enrichment.

### 3. Independence is by origin, as the existing detector defines it

Two declarations count as two when their pages have different origins under
`entity_recurrence._origin_refs`: pages compiled from the same Source or session are one
origin. This keeps one long session that produces four notes from promoting a passing
name. Pages in `Sources/` and `Evidence/` never carry declarations; they are immutable.

### 4. Thresholds are vault-owned

Defaults: `promote_at_independent_mentions: 2`, `promote_central_at: 1`. They live in the
entity-type registry's vault override, beside the types they govern, with the same load
contract and findings. Bounds: integers 1 to 5; a value outside them is ignored with a
finding. `promote_central_at` above 1 is how an owner turns the single-mention path off.

These are counts the server compares against. They are not confidence scores and nothing
is ranked by them.

### 5. The candidate rides the write response, once

When a write makes an unresolved identity cross its threshold, the committed response
carries `entity_candidate`: at most three candidates, each with the normalised name, the
declared type cues, up to eight declaring pages, near matches from the registry, and the
two routes (`resolve-entity`, then `create-entity` or a hydration target). It is the same
class of carrier as `structure_suggestion` and `records_routing`, and follows their
delivery rules (compact detail keeps it; `legacy` detail drops it).

An identity is delivered on a write response once per threshold crossing. It stays in the
`entity_recurrence` attention family until the candidate's state resolves: an Entity now
resolves the name, or the owner dismissed it through `triage_memory`.

What this carrier prevents: the candidate sitting in a queue no agent reads, which is
what happens to the two candidates the vault has today. What it costs when it fires
wrongly: a few hundred bytes on one write response and an agent declining to create.

### 6. Backlog

`review_memory(mode="audit", categories=["undeclared_mentions"])` lists active compiled
pages whose frontmatter has no `mentions` key, newest first, bounded like every audit
family. An empty list (`mentions: []`) is a declaration that the page names nothing and
removes the page from the queue. The family is opt-in and never enters due-state: a vault
adopted with 4,000 undeclared pages must not open with 4,000 items of debt.

The sweep itself is an agent reading pages and calling `declare-mentions`. It costs model
tokens, so the owner starts it and chooses its size.

### 7. Doctrine

One paragraph replaces the conjunction "stable, recurring, central" in bootstrap guidance
and the scaffold skill:

> When you write something durable, declare the people, organisations, places, equipment,
> products and other identities it names in `mentions`. Mark one `central` when the note
> is about it. When a write returns `entity_candidate`, resolve before you create, and
> hydrate an existing Entity before you make a second one. A passing name needs no
> declaration.

The tool schema's own description of `mentions` carries the same sentence in short form,
because the schema is the only text every client is certain to read.

## Risks / Trade-offs

- **Agents ignore an optional argument.** Mitigation: the schema description, bootstrap
  doctrine and the capture-sweep advisory all name it; the backlog queue makes omission
  visible. It is not made required: a required argument would fail writes from older
  clients and cached connector schemas, and a failed durable write costs more than a
  missing declaration.
- **Agents over-declare.** A declaration creates nothing by itself. Over-declaration
  raises candidate volume; the threshold, the origin rule and the ambiguity stop bound it,
  and the owner can raise the threshold.
- **Name variants split a count** ("Harbour Studio" / "Harbour Studios"). Normalisation is
  the identity key the registry already uses; near matches are shown on the candidate so
  the agent can merge by declaring an alias at creation. No fuzzy merging in the server.
- **Frontmatter growth.** 24 short strings at most.

## Migration

None. Pages without `mentions` are unchanged. Derived counts rebuild from frontmatter.

## Open Questions

- Whether `observe_memory` should also accept `mentions` for a single unit. Deferred: a
  unit lives on a page whose declaration already covers it.
