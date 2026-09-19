## Why

People, equipment, organisations and places stay scattered through notes as prose. They
rarely become Entity pages or graph edges unless the owner asks, so the graph cannot
answer "what do I know about this supplier" and the context compiler has few anchors to
resolve: its anchors are entities, hubs and resources, and a vault whose identities live
only in sentences offers it almost none.

The shipped recurrence detector cannot close this. It recognises five closed sentence
shapes (`identity-frames-v1`: "X is a <type noun>", "<Type>: X", relation frames around
an already-registered entity) and asks for three pages, three independent origins and two
facets. Measured on a 4,100-page personal vault on main at `169e6deb`:

- the grammar found 30 identities in total;
- 29 appear on one page, one appears on two, none on three;
- the attention queue surfaces two candidates, both unresolved wikilinks in index pages,
  neither a person, a thing or an organisation.

Lowering the threshold from three to two would surface one more candidate. The detector
is not too strict; it cannot see. Ordinary notes do not name things in copula sentences,
and no closed grammar will, in any language or domain.

The component that can see is already present at every write: the agent composing the
note has read the material and knows which identities it names. Exomem's constitution is
that the server measures and the agent reasons. Today the agent's knowledge of who and
what a note mentions is discarded at the write boundary.

## What Changes

- **Declared mentions.** The compiled-write tools accept an optional `mentions` list:
  the identities the note names, each with a name, an optional entity type and an
  optional `central` flag. The server stores it as `mentions:` frontmatter on the page,
  so it is Markdown-native, visible in the owner's editor and rebuildable without the
  server. `connect_memory(operation="declare-mentions")` sets it on an existing page.
- **Resolved mentions become graph edges at once.** Each declared name is resolved
  against the entity registry with the existing exact and alias resolver. A resolved
  non-central mention yields a `mentions` edge to the Entity page; a resolved central one
  yields `about_entity`. An ambiguous name yields no edge and is reported. Nothing is
  guessed.
- **Unresolved mentions are counted, not created.** The server keeps a derived count of
  independent pages declaring each unresolved identity. An identity becomes a promotion
  candidate when it is declared on a second independent page, or on its first page when
  declared `central`. The thresholds ship as defaults in the vault-extensible entity-type
  registry and the owner may change them.
- **The candidate rides the write response.** The write that crosses the threshold
  returns a bounded `entity_candidate` block (name, declared types, the pages that
  declared it, near matches, the route to `resolve-entity` then `create-entity`), so the
  agent that holds the context acts in the same turn, on every client, hookless or not.
  The same candidate appears in the `entity_recurrence` attention family for later.
- **The server still never creates an Entity.** Creation stays an agent decision inside
  the existing delegation envelope and confirmation ceiling.
- **Doctrine.** Bootstrap guidance and the skill scaffold change from "create an Entity
  only when stable, recurring and central" to: declare the identities a durable write
  names; promote on the second independent mention, or on the first when the identity is
  central to the note; resolve before create; hydrate before duplicate.
- **Backlog.** A read-only queue lists active compiled pages with no `mentions`
  declaration, newest first, so an owner-started agent sweep can declare them in bounded
  batches. The closed-grammar detector stays as a secondary signal; it is not extended.

Not in this change: server-side extraction of any kind, a background writer, confidence
scores, new entity types, or changes to how recall ranks.

## Capabilities

### New Capabilities

- `declared-identity-mentions`: the write-time declaration, its storage, resolution into
  edges, the derived recurrence count, promotion candidates and the backlog queue.

### Modified Capabilities

- `agent-bootstrap-contract`: the identity-capture doctrine every agent receives.

## Impact

- Tool surface: `remember` and `replace_memory` gain one optional argument;
  `connect_memory` gains one operation. Schema fixtures, digests, capabilities and the
  plugin trees are regenerated; connector clients need an action-schema refresh.
- Code: write path (`commands.py`, the add/replace leaves), a new
  `declared_mentions.py`, `entity_recurrence.py` and `attention.py` (candidate source),
  the entity-type registry (thresholds), graph edge origins for `mentions` and
  `about_entity`, scaffold skill references and bootstrap guidance.
- Context compiler: every promoted identity is a new `entity` anchor, and every resolved
  mention is a typed link summary on an anchor row. No compiler code changes.
- Pages without `mentions` behave exactly as today.
