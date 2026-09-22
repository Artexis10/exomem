## Context

The resolver in `working_set_resolve.py` assembles categorical evidence per anchor
(`candidates_for`), adds `graph_corroboration` (`add_graph_corroboration`) and derives a
status (`_status_for`): `exact_alias`, or two deciding kinds, resolves. The constitution
is unchanged by this change: the server measures, the brain reasons, no server-side
reasoning model, no float leaves the server.

## Goals / Non-Goals

**Goals:** no anchor is served unless the turn's own words reached it or the agent chose
it; natural short references reach their anchor; CI can see a dense-vault false
activation; acceptance is judged on real turns.

**Non-Goals:** new anchor kinds or folder-named anchors (they belong with
`make-activation-conventions-vault-owned`); rendering `partial` candidates in the hook
(a later amendment to the host-turn change); tuning recall itself.

## Decisions

### 1. Independence is defined, not assumed
Two kinds are independent only if they cannot both follow from one upstream fact.
`retrieval` and `vector_band` both follow from "a ranking engine placed this near the
turn", and `graph_corroboration` between two such candidates follows from the same fact.
So: `resolved` requires `exact_alias`; or a strong worded contact (`lexical_overlap` on
two or more terms, or `claims_match`) plus any other kind except `usage_prior`; or the
weak worded contact `rare_term` plus another contact kind. Any set of retrieved kinds and
qualifiers without a worded contact is `partial`, and so is `rare_term` with qualifiers
alone. Words and a ranking engine agreeing is two facts; a ranking engine agreeing with
itself is one.

*Alternative considered:* keep the rule and threshold recall scores. Rejected: a score
threshold is a tuned float with no meaning across vaults, and the neighbourhood clause
would still admit candidates the turn never named.

*Amended by `close-memory-loop` D3, retrieval-carried packets.* Nothing above changes:
retrieval alone still never resolves an ANCHOR, whatever it scores and however many
retrieved kinds and qualifiers stack. The one retrieval-alone case sits outside this rule
rather than inside it, and only reaches a turn this rule has already abstained on. When
resolution reached no anchor at all, one scored recall runs over the compiled knowledge
base (the raw-material folders excluded — a captured source or a preserved piece of
evidence is never served as durable memory), and a single page that DOMINATES it may
CARRY a packet. Dominance is a NAMED-CONTACT test, not a score threshold: a hit is a
candidate only when at least two of the turn's stems that it matches are DISTINCTIVE in
this corpus (document frequency at or below `max(3, ceil(0.5% of indexed pages))`,
measured against the same catalogue the ranking uses), and those stems must sit
together — two within `RETRIEVAL_CARRY_RARE_WINDOW` tokens of each other, or
three anywhere in the turn — because two distinctive words nine tokens apart
are two things a speaker mentioned rather than a name. What survives is the set of pages the
turn named, and a packet is carried only when there is exactly one of them:
two named pages abstain, since the gap between their scores says nothing
about which was meant. Superseded and archived pages are excluded before
that count, because a page and the page it superseded answer to one phrase. Rarity needs a
corpus: below `RETRIEVAL_CARRY_MIN_PAGES` the carry does not run at all,
because in a vault holding no ordinary prose every ordinary word is rare by
measurement. An absolute score floor was
tried first and removed: `-bm25()` is not comparable between corpora, so the same page
for the same turn scored 13.16 at one corpus size and 6.81 at another, and a two-line
stub sharing three ordinary words with a long turn outscored the page the turn was
actually about. Rarity is corpus-relative, so it does not drift as the vault grows; that page's units are served under one anchor entry of
kind `page` at status `retrieval_carried`, marked `generation.carried_by = "retrieval"`,
and no continuity token is minted from it. It never runs when any anchor resolved, when the
turn is ambiguous, or when the agent named a sense; a near tie takes the abstention the
turn already had. The rejected alternative stands as written — a score is still no way to
RESOLVE an anchor, and this is not one: it decides whether a turn that resolved nothing is
served a named page's own material or nothing at all, which is a choice between serving and
abstaining rather than between two senses.

### 2. `retrieval` is about the anchor's own page
The neighbourhood clause is removed. It was written so that a hub whose members are
recall hits would be reached; on a dense vault it reaches every hub for every turn. A hub
whose members are hits is still reachable by corroboration (decision 3) once one member
anchor is reached by the turn's words.

### 3. Corroboration needs an independently reached partner
`graph_corroboration` is granted to a candidate only when it is typed-linked to another
candidate that carries a worded contact. Two anchors the turn both named still
corroborate each other, so the canonical "directly linked anchors are complementary"
scenario is unchanged.

### 4. One rare word is contact
At index build the index records, for each normalised term occurring in any anchor's
title or aliases, how many anchors it names. A new kind, `rare_term`, is granted for one shared
term when that count is at most `RARE_TERM_MAX_ANCHORS = 3`. The count is structure the
server measures; it is not a relevance score and it never leaves the server.

Measured on the owner's index (255 anchors, 650 distinct title terms): 622 terms name at
most three anchors, so rarity in the catalogue is cheap and cannot carry a resolution by
itself. Everyday one-word references in the test turns named one or two anchors each,
while the vault's generic structural words named between 6 and 22. Hence `rare_term` is a separate, weaker kind
than `lexical_overlap`: it resolves with the anchor's own page in recall, the vector band
or claims, and stays `partial` with `category_match` or `graph_corroboration` alone. The
kind is visible in the packet, so the agent can see how thin the contact was.

Lexical comparison folds regular plurals on both sides (`posts`/`post`,
`batteries`/`battery`); `exact_alias` stays exact.

### 5. A title's leading name is a name
A page titled `Bike (Trek 520, 2019)` is called "the bike". The index derives one short name per anchor
from a title with a trailing parenthetical or a ` - `/` — ` qualifier, and admits it as an
alias only if no other anchor's names include it and every term of it names at most
`RARE_TERM_MAX_ANCHORS` anchors (counts taken before derived names are added). The
second guard came from the real-vault run: one page titled with a product name, a dash
and a subtopic took the bare product name as its alias although twenty other pages
begin with the same word, and resolved alone for any turn naming the product.
Uniqueness is recomputed at build, so a
second bike page retires the short name and both fall back to lexical contact. Owners
keep full control through frontmatter `aliases`.

*Risk:* a unique common word resolves alone ("bike" in a turn about someone else's bike).
Accepted: the anchor is the owner's only thing of that name, the packet is bounded, and
the alternative is that the most natural reference never works. The critic should attack
this decision specifically.

### 6. The agent decides uncertain turns
An `unresolved` abstention already lists its `partial` candidates with their evidence.
That list is the hand-off to the reasoning agent, which can call again with `anchor`
set. Semantic references with no shared word (an everyday word for a page titled with its
technical name) arrive this way, as `partial` on `vector_band` or
`retrieval`.

### 7. Instruments
- Seeded probe corpus (`build_soundness_probe_corpus`), separate from the pre-registered
  fixture set: one dense cluster (at least 20 interlinked pages, at least 6 of them
  anchors); `T10` a negative twin whose recall hits fall inside the cluster; `T11` a
  turn whose only link to an anchor is a recall hit on the anchor's neighbour; `C10` a
  one-word reference to a uniquely named resource. It is scored against the real
  resolver in its own test module. The pre-registered cases and the agent-arm module
  are not changed.
- `scripts/activation_real_turns.py --snapshot DIR --turns FILE`: runs each turn in one
  resident process after the recall index is built, prints per-turn status, anchors and
  evidence, and compares against expected anchor paths in the private file. Refuses a
  `--turns` or `--snapshot` path inside any repository checkout, as
  `private_vault_snapshot.py` does.

## Risks / Trade-offs

- **Recall drops.** Turns that resolved on retrieved evidence alone now abstain with
  candidates. That is the intended trade: a wrong packet presented as `resolved` costs
  the agent more than an honest candidate list.
- **Rare-term threshold.** Three is the shipped default, judged against one vault. It
  is structural (a count of anchors) and bounded. It moves into the conventions registry
  (`resolution.rare_term_max_anchors`) with `make-activation-conventions-vault-owned`,
  where an agent can change it through the governed write path and the digest in the
  cache key makes the change effective on the next call.
- **Benchmark numbers move.** Expected: no digest has frozen and no paid run has been
  made.

## Migration

Sidecar schema version bump, one rebuild. No owner action.

## Open Questions

- Whether a `vector_band` that is an outlier against the whole catalogue should count as
  a worded-equivalent contact. Deferred until a real-vault run with embeddings enabled
  shows how often an outlier band is wrong; the script in decision 7 measures it.
