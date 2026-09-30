## Why

`activate_context` resolves irrelevant anchors on a real, densely linked vault. Run
against a snapshot of a 4,400-page personal vault with a recall index available (the
condition every live cell is in), every turn tried resolved about five anchors with the
evidence `[graph_corroboration, retrieval]`:

- "What's the capital of Australia?" resolved five unrelated private entities and served
  1,721 characters as `resolved`.
- A turn naming one of the owner's resources by its everyday one-word name resolved two
  unrelated hubs and three people, and never the resource's own page.
- A collection that did resolve correctly by exact alias was joined by four junk
  anchors, so the turn ended as an `ambiguous` abstention.

The canonical requirement already asks for "at least two **independent** kinds". The
implementation does not deliver independence:

1. `retrieval` is granted when the anchor **or any page in its link neighbourhood** is
   among recall's hits. Hybrid recall returns hits for every turn, and a hub or a person
   links dozens of pages.
2. `graph_corroboration` is granted when two candidates are typed-linked. Candidates
   admitted through (1) cluster around the same hits, so they are linked by construction.
3. Two kinds resolve.

Both kinds restate one fact: recall returned something nearby. The pre-registered
synthetic corpus has a sparse graph, so its negative twins abstain and CI is green.

Making the rule sound removes most of what currently resolves, which exposes the second
problem the same run showed: the way a person actually refers to a thing ("the bike",
"invoices") makes no contact at all, because the lexical band needs two shared content
words and compares unfolded tokens.

## What Changes

- **Evidence families.** Contact kinds split into *worded* (`exact_alias`,
  `lexical_overlap`, `claims_match`: the turn's own words reach the anchor's own names,
  terms or claims) and *retrieved* (`retrieval`, `vector_band`: a ranking engine
  surfaced it). An anchor resolves on `exact_alias`, or on two kinds of which at least
  one is a worded contact. Retrieved kinds alone are `partial`, however many co-occur.
- **`retrieval` means the anchor's own page was a recall hit.** A hit in the
  neighbourhood is not contact.
- **`graph_corroboration` needs an independently reached partner.** It is granted only
  when the linked candidate carries a worded contact.
- **One rare word is weak contact.** A new worded kind, `rare_term`, is granted for a
  single shared term that names at most three anchors in the catalogue (measured over
  title and alias terms at index build). It resolves only together with another contact
  kind, never with qualifiers alone: on the measured vault 622 of 650 title terms are
  rare by that count, so one rare word plus a turn cue must not be enough. Lexical
  comparison folds singular and plural. The threshold ships as a default and moves into
  the vault-owned conventions registry with `make-activation-conventions-vault-owned`,
  so an agent can change it through the MCP write path and see it take effect on the
  next call.
- **A title's leading name is a name.** The index derives a short name from a title
  that carries a parenthetical or dash qualifier (`Bike (Trek 520, 2019)` → `bike`) and
  treats it as an alias only while it is unique in the catalogue and every word of it
  is rare there by the `rare_term` measure, so a topic prefix shared by twenty pages
  never becomes one page's name.
- **Uncertain turns go to the agent.** An `unresolved` abstention keeps listing its
  `partial` candidates, so the agent can choose one with the `anchor` override from
  `activate-context-on-host-turns`. The server measures; the agent decides.
- **CI can see this class.** A seeded dense-cluster corpus with a negative twin, a
  recall-neighbour twin and a one-word-reference case runs against the real resolver as
  a standalone check. The pre-registered fixture set and the agent-arm module are left
  untouched: the owner has ruled out the paid agent-arm run, so widening its case count
  would buy nothing.
- **A local real-vault check.** A script runs a private, never-committed list of turns
  with expected anchors against a vault snapshot and reports per-turn resolution. Its
  results are the acceptance evidence for this change and stay in the owner's knowledge
  base.

## Capabilities

### Modified Capabilities

- `context-activation`: the resolution rule, the meaning of `retrieval` and
  `graph_corroboration`, single-term and folded lexical contact, derived short names.

## Impact

- Code: `working_set_resolve.py`, `working_set_index.py` (term-frequency table, derived
  names, schema version), `working_set.py` call sites, benchmark fixtures and scorer
  tests, new `scripts/activation_real_turns.py`.
- Behaviour: fewer anchors resolve. Packets built today from retrieved evidence alone
  become `unresolved` abstentions that list candidates.
- Sidecars rebuild once (schema version).
- Sequencing: lands before `activate-context-on-host-turns` is promoted, because that
  change tells agents to call the tool on every substantive turn. That change rebases
  onto this rule and adds its own clauses (`agent_choice` resolves alone; `continuity`
  with any contact kind resolves).
