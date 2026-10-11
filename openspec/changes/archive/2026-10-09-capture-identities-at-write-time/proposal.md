## Why

People, equipment, organisations and places stay scattered through notes as prose. They
rarely become Entity pages or graph edges unless the owner asks, so the graph cannot
answer "what do I know about this supplier", and the context compiler has few anchors to
resolve: its anchors are entities, hubs and resources, and a vault whose identities live
only in sentences offers it almost none.

Measured on a personal vault on main at `169e6deb`. The recurrence sweep saw 4,100
eligible pages; the vault holds 142 Entity pages and yields 255 activation anchors.

- The sentence grammar (`identity-frames-v1`) found 30 identities in total, 29 of them on
  one page and one on two. Its gate needs three pages, three independent origins and two
  facets, so lowering any one of them surfaces nothing.
- The wikilink lane found 34 unresolved identities: 27 linked from one page, five from
  two, two from three or more. Ten of the 34 are linked only from `index.md` pages,
  including both candidates the queue surfaces today.

Both lanes are nearly empty for the same reason: nothing at write time marks the
identities a note names. No closed grammar will find them in ordinary prose, in any
language, and the server does not read prose for meaning. The agent composing the note
does, and already has a Markdown-native way to say "this is a thing": a wikilink, which
the owner's editor shows as an unresolved node until the page exists. The product
already counts unresolved wikilinks across pages, already re-derives a page's edges when
the linked page is created later, and already reports a write's page-less links on the
write response. What is missing is the instruction to link, a gate that fires on the
second page instead of the third, and a candidate delivered at the moment it appears.

## What Changes

- **Doctrine: link what you name.** Bootstrap guidance, the scaffold skill, the `body`
  argument description of `remember` and `replace_memory`, and the capture hook, capture
  skill and operations reference tell the agent to wikilink the people, organisations, places, equipment and products a
  durable write names, whether or not a page exists yet, and to create the Entity in the
  same turn when the note is about an identity that has none. A count is a prompt to
  consider promotion; the agent still judges whether the identity is stable and useful.
- **The wikilink lane fires on the second page.** Its spread gate moves from three
  distinct eligible pages to two. The sentence-grammar lane keeps its gates.
- **Navigation pages are not evidence.** `index.md` and `log.md` pages no longer supply
  spread, which removes the only candidates the vault surfaces today, both noise.
- **The candidate rides the write that creates it.** When a committed write adds a link
  that brings an unresolved identity to the gate, the response carries a bounded
  `entity_candidate` block: the name, the linking pages, near matches from the registry
  and the routes to `resolve-entity` and `create-entity`. It is derived from vault state,
  so it needs no per-caller ledger; it is advisory and may undercount, with the audit
  family authoritative; it is sent only when the graph index is available; and it
  belongs to the `structural_suggestions` authority class, so an owner who turned that
  class off is not prompted.
- **The server still never creates an Entity**, and no tool gains an argument.

Not in this change: a `mentions` declaration argument or frontmatter key (the first
draft; see the design for why it was withdrawn), server-side extraction of any kind, a
background writer, changes to the sentence-grammar lane, a new backlog queue, or a
vault-owned threshold.

## Capabilities

### New Capabilities

- `write-time-identity-candidates`: the candidate block on the committed write response
  and the write-surface text that teaches linking.

### Modified Capabilities

- `agent-bootstrap-contract`: the link-what-you-name guidance.
- `action-first-audit`, through the still-active change
  `complete-recurring-entity-lifecycle`: that change already rewrites the recurrence
  requirement and has not been archived, so the wikilink lane's gate, its evidence pages
  and the write-time clause are amended in its delta rather than by a second, competing
  `MODIFIED` block here.

## Impact

- Tool surface: no new argument or operation. Two argument descriptions change, so the
  schema fingerprint, capabilities doc and plugin trees are regenerated; a stale client
  schema keeps working.
- Code: `entity_recurrence.py` (gate, evidence pages), a candidate computation beside
  `capture_sweep.py`'s page-less-link hint, the mutation terminal (one more advisory
  carrier), bootstrap guidance in `commands.py`, the scaffold skill, the capture hook and
  its packaged copy.
- Context compiler: every Entity created from a candidate is a new `entity` anchor, and
  every link that resolves is a typed link summary on an anchor row. No compiler change.
- Existing vaults: the wikilink lane surfaces a few more candidates at once (four on the
  measured vault) and stops surfacing index-page noise.
- Spec ordering: the recurrence requirement is edited inside
  `complete-recurring-entity-lifecycle`'s delta, so the rule and the code change in one
  merge and no two active changes state opposite gates. If that change is archived
  before this one merges, the same edits move into a `MODIFIED` block of this change.
