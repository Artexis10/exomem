## MODIFIED Requirements

### Requirement: Unreflected observations is a structured family derived from collection claims
The audit SHALL register an `unreflected_observations` category. An entry SHALL exist per (collection, observation page) when a compiled-note or Evidence write's terms cover at least the minimum coverage of exactly one routing-target collection's effective claims, or the page satisfies exactly one collection's `claims.match` predicates, and no record in that collection links the page or carries a matching natural-key value; a predicate entry SHALL carry its matched predicates and store the page's folded facets so a serve recomposes it identically. The recompute SHALL additionally group every untracked page older than the discovery lookback that a collection now covers and no record reflects into ONE grouped entry per collection (`kind: backfill`), keyed by the manifest, carrying the count, at most a provisional number of observation references and a `signal_version` derived from the collection and its effective claims and predicates only, so a decision holds while more matching pages accumulate and a claims change asks again. The grouped entry SHALL be computed only by the recompute, bounded by a provisional maximum of parsed pages per recompute and by a cheap pre-filter, and never on the request path; write-time deltas SHALL leave it unchanged. Entries younger than a provisional grace window SHALL be stored as pending with their due date and served only after it. The finding SHALL carry the collection, the page reference, the matched terms and a `signal_version` derived from authored state only; its fingerprint SHALL be the collection identity, the page reference and the sorted matched terms, so a re-write adding matched terms resurfaces a dismissed entry while the dismissal record stands. The finding SHALL resolve only when a record in that collection links the page or carries a matching natural-key value, when the page is gone, or when the collection no longer covers the entry's terms — never by time and never by the runtime writing a record. The category SHALL be in the default attention union, in the due-state projection categories and in the structured delta categories, with edit rules of its own, separate from `unreflected_outcomes`, and therefore a registered family for dispositions. Disclosure SHALL require that the requesting audience may read both the page and the manifest; served entries SHALL be recomposed at serve time.

#### Scenario: A preserved publication artifact opens an entry
- **WHEN** an Evidence artifact tagged with a platform and account value a collection claims is preserved and no record links it
- **THEN** after the grace window the audit reports one `unreflected_observations` finding for that collection and page, and it appears in the default attention listing and the due-state block

#### Scenario: The record settles it by state change
- **WHEN** a record is appended to that collection with a `sources` link to the artifact, or with the artifact's natural-key value
- **THEN** the entry is gone on the next read without any dismissal being recorded

#### Scenario: A private record does not settle another audience's visible gap
- **WHEN** a record reflecting a readable observation is withheld from the requesting audience while its manifest and observation remain readable
- **THEN** the observation remains pending or due for that audience, consistently in inspection, attention and the carrier before and after reconcile

#### Scenario: A returned artifact path identifies the preserved observation
- **WHEN** a record links the artifact path returned by preservation instead of its governance companion page or stable reference
- **THEN** all three declared references settle the same observation, including after reconcile

#### Scenario: Discovery lookback does not expire unresolved work
- **WHEN** an already tracked observation ages past the discovery lookback without a reflecting record or a change to its page or collection claims
- **THEN** it remains unresolved after reconcile; only discovery of previously untracked old pages is bounded by the lookback

#### Scenario: Ties and unclaimed pages stay quiet
- **WHEN** a page's terms cover two collections equally, or no collection's effective claims
- **THEN** no entry is created

#### Scenario: Grace window keeps an episode quiet
- **WHEN** the artifact was preserved less than the grace window ago
- **THEN** the entry is pending: inspection can list it, but no carrier or default listing serves it

#### Scenario: Family disposition applies
- **WHEN** the family's disposition is `quiet`
- **THEN** the audit still measures it and it leaves the default union, the carriers and the write advisories exactly as any other quiet family

#### Scenario: Removing the family from the structured delta stops write-time maintenance
- **WHEN** the category is removed from the structured delta categories
- **THEN** record writes no longer add or settle entries and only reconcile maintains them, proving the mechanism is load-bearing

#### Scenario: Creating a collection backfills one grouped item
- **WHEN** a collection declaring `match: {type: [failure], project: [example-product]}` is created over a vault holding twelve untracked failure notes older than the lookback
- **THEN** the next recompute stores one grouped `unreflected_observations` entry for that collection with count 12 and at most the provisional number of references, and no per-page entries for those notes

#### Scenario: The backfill decision holds until the claims change
- **WHEN** the grouped entry is dismissed and another matching old note appears
- **THEN** its fingerprint is unchanged and it stays dismissed, while editing the collection's claims produces a new signal version

#### Scenario: Backfill stays off the request path
- **WHEN** a compiled write commits after the grouped entry exists
- **THEN** the write's delta leaves the grouped entry byte-identical

#### Scenario: Reflected pages leave the backfill
- **WHEN** a record in the collection cites one of the grouped notes in `sources`
- **THEN** the next recompute's count excludes that note

### Requirement: Collection candidacy is a projected audit category resolved by claims
The audit SHALL register a `collection_candidate` category computed over parsed compiled pages the requesting audience may read. A domain term SHALL be a candidate when it is neither breadth vocabulary, a function word, a project key nor a core epistemic category, is distinctive (carried by no more than a provisional share of the units table once the table holds at least a provisional minimum number of units, a verdict stored with the entry so serve-time recomposition from a subset keeps it), is not covered by any collection's effective claims, and its units recur across at least the provisional minimum number of pages and distinct dates spanning the provisional minimum span, with at least the provisional minimum number of units carrying a state-change lexeme, a currency amount or an ISO date, and at least two co-recurring identity terms. One finding SHALL be produced per qualifying term, partitioned by that term, carrying the domain terms (at most six), bounded evidence unit references (at most eight), a strength of exactly `strong` or `moderate`, and a `signal_version` derived from the sorted evidence unit references so new evidence resurfaces a dismissed candidate. The category SHALL be in the due-state projection categories as a recompute-only category and registered as an opt-in attention category, not in the default union. It SHALL resolve when a manifest's effective claims cover the domain terms, with no dismissal memory beyond the fingerprint rule. Every constant SHALL be declared provisional in one module. No finding SHALL report a numeric confidence.

#### Scenario: A recurring longitudinal domain surfaces
- **WHEN** four compiled pages written on three dates two weeks apart carry units tagged with the same domain term, three of them stating purchases, cancellations or amounts, with two recurring identity terms, and no collection claims the term
- **THEN** the audit reports one `collection_candidate` finding for that term with its evidence units, and the due-state block served on bootstrap and recall counts it

#### Scenario: One-off facts stay quiet
- **WHEN** the same term appears on one page, or on three pages written the same day, or without any state-change, amount or date unit
- **THEN** no finding is produced

#### Scenario: A claiming collection resolves the candidate
- **WHEN** a collection is created whose effective claims cover the domain terms
- **THEN** the finding is gone on the next reconcile without any dismissal being recorded

#### Scenario: Opt-in, not default
- **WHEN** the default attention listing is requested
- **THEN** it contains no `collection_candidate` item, while requesting the category explicitly returns it

#### Scenario: Withheld pages contribute nothing
- **WHEN** a page carrying qualifying units is withheld from the requesting audience
- **THEN** its units contribute to no gate, count or evidence reference served to that audience

#### Scenario: Generic vocabulary stays quiet
- **WHEN** three hundred dated state units across sixty pages carry only generic words such as result, technique and workflow
- **THEN** no candidate is produced, strong or moderate

#### Scenario: One real domain is one candidate
- **WHEN** the same table also holds four dated state units across four pages tagged with one domain term and two identity terms that ride the same units
- **THEN** exactly one candidate is produced for that domain, naming all three terms

#### Scenario: Serving is capped per response
- **WHEN** more qualifying candidates are open than the provisional per-response maximum
- **THEN** a served due-state block counts at most that many `collection_candidate` items, strongest and widest first, after audience filtering and triage
