## MODIFIED Requirements

### Requirement: Unreflected observations is a structured family derived from collection claims
The audit SHALL register an `unreflected_observations` category. An entry SHALL exist per (collection, observation page) when a compiled-note or Evidence write routes to exactly one routing-target collection under the command-surface routing rule (declared predicates first, ranked by predicates held and then coverage; otherwise coverage of the effective claims over the collections the page does not contradict, where a collection that holds some of its declared predicates and is itself a valid word route ranks ahead of one that holds none), and no record in that collection links the page or carries a matching natural-key value; a predicate entry SHALL carry its matched predicates and store the page's folded facets so a serve recomposes it identically. The recompute SHALL additionally group every untracked page older than the discovery lookback that routes to a collection with strength `strong` or by its declared predicates, and that no record reflects, into ONE grouped entry per collection (`kind: backfill`), keyed by the manifest, carrying the count, whether the count is `truncated`, at most a provisional number of observation references and a `signal_version` derived from the collection and its declared `claims` and `claims.match` only, so a decision holds while more matching pages accumulate or records are appended, and a change to the declared claims asks again. The grouped entry SHALL be computed only by the recompute and never on the request path; write-time deltas SHALL leave it unchanged. It SHALL be bounded by a pre-filter admitting a page for a collection only when its title and tags share at least two of the collection's claim words without contradicting its predicates, or its frontmatter satisfies them, and by a provisional maximum of parsed pages per collection per recompute. A collection that reaches that maximum SHALL resume after a cursor keyed by its collection identity and claims signal on the next recompute, carrying forward what earlier windows found, and SHALL report `truncated` beside its count wherever the count is shown until a scan reaches the last page. The grouped entry SHALL be asked about once at every prominence and SHALL NOT be filed in bulk under a standing prominence. Entries younger than a provisional grace window SHALL be stored as pending with their due date and served only after it. The finding SHALL carry the collection, the page reference, the matched terms and a `signal_version` derived from authored state only; its fingerprint SHALL be the collection identity, the page reference and the sorted matched terms, so a re-write adding matched terms resurfaces a dismissed entry while the dismissal record stands. The finding SHALL resolve only when a record in that collection links the page or carries a matching natural-key value, when the page is gone, or when the collection no longer covers the entry's terms — never by time and never by the runtime writing a record. The category SHALL be in the default attention union, in the due-state projection categories and in the structured delta categories, with edit rules of its own, separate from `unreflected_outcomes`, and therefore a registered family for dispositions. Disclosure SHALL require that the requesting audience may read both the page and the manifest; served entries SHALL be recomposed at serve time.

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
- **WHEN** the grouped entry is dismissed and another matching old note appears, or ordinary records are appended to the collection
- **THEN** its fingerprint is unchanged and it stays dismissed, while editing the collection's declared claims produces a new signal version

#### Scenario: A large backfill reaches every page over later recomputes
- **WHEN** a collection covers more old notes than the per-collection maximum
- **THEN** the first recompute reports that maximum with `truncated`, a later recompute resumes after the cursor and reports the larger count, the count stops being truncated once the scan reaches the last page, and another collection's grouped entry, even one declaring identical claims, is unaffected by the first one's budget and cursor

#### Scenario: Only confident routes join the backfill
- **WHEN** old notes share claim words with a collection but route to it only as `moderate`
- **THEN** no grouped entry is produced for them

#### Scenario: Backfill stays off the request path
- **WHEN** a compiled write commits after the grouped entry exists
- **THEN** the write's delta leaves the grouped entry byte-identical

#### Scenario: Reflected pages leave the backfill
- **WHEN** a record in the collection cites one of the grouped notes in `sources`
- **THEN** the next recompute's count excludes that note
