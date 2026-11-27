## MODIFIED Requirements

### Requirement: Link projection decides from the reader's visible pages

Record and planning link projection SHALL decide each bare title link and each memory reference over the pages that the reader may see. The projector SHALL remove every page that the reader may not see before it tests an index entry for emptiness, uniqueness or ambiguity. A stem, a title, a vault-root name or a memory id that matches a withheld page SHALL give the same result as a vault without that page.

The projector SHALL resolve these values from the last published corpus generation when one can be borrowed, and SHALL NOT build, refresh or wait for a corpus generation on the request thread. Without one, it SHALL run the bounded walk unless a generation is coming. The cap of that walk bounds its cost and SHALL NOT decide disclosure: a vault over the cap with a published corpus SHALL keep the links that the reader may see.

A memory reference whose owner an unpublished outside edit may have changed SHALL be resolved only from owners that still hold the id on a live re-read.

#### Scenario: A withheld page does not change a link

- **WHEN** a withheld page shares a stem, a title, a vault-root file name or a memory id with a page the reader may see
- **THEN** the link to the visible page resolves exactly as in a vault without the withheld page
- **AND** the result does not reveal that the withheld page exists

#### Scenario: Two visible pages keep a memory id ambiguous

- **WHEN** two pages that the reader may see hold the same memory id
- **THEN** the projector withholds the memory reference

#### Scenario: Links resolve from the published corpus with per-request visibility

- **WHEN** a published corpus generation exists and a query carries a bare title or a memory reference
- **THEN** the projector resolves the value from that generation without walking the vault
- **AND** it applies the visibility of that request, so two readers of one generation can receive different results

#### Scenario: A vault over the walk cap keeps its links

- **WHEN** a vault holds more Markdown files than the walk cap and a corpus generation is published
- **THEN** bare titles and memory references to pages the reader may see resolve
- **AND** no vault walk runs

#### Scenario: Without a published generation the bounded walk runs

- **WHEN** no corpus generation can be borrowed and none is coming, as with the corpus cache off, background warms off, or vault change events that are not live
- **THEN** the projector runs the bounded synchronous walk with its existing caps
- **AND** a vault over the cap withholds the bare titles and memory references that the walk could not index
- **AND** a query names each returned link field that lost a value in `unresolved_links`, and refuses a selection on such a field with the code `LINK_INDEX_INCOMPLETE` instead of answering without those values
- **AND** a link field whose values all name a page by path loses no value, so a selection on it answers

#### Scenario: A stale memory reference is withheld

- **WHEN** the watcher has seen an outside edit that moved a memory id off a visible page and has not published it
- **THEN** the projector withholds the reference to that id
- **AND** after publication, the id resolves to the page that now holds it
- **AND** the projector does not scan the vault to recover

## ADDED Requirements

### Requirement: A query does not walk for links while a corpus generation is coming

A Records or Planning query in the flat query grammar SHALL NOT walk the vault while a corpus generation is coming: the corpus cache is on, vault change events are live, the borrow found no current generation or its lock held, and a warm that publishes one runs. Such a query SHALL name each returned link field that lost a value in `unresolved_links` and SHALL refuse a selection on a link field with the retryable code `LINK_INDEX_WARMING`. A version-1 query keeps its own bounded walk.

#### Scenario: While a generation is coming a query answers without walking

- **WHEN** a Records or Planning query returns a link field and can borrow no corpus generation while one is coming
- **THEN** the query does not walk the vault
- **AND** it leaves out each bare title and memory reference in that field
- **AND** it names the field in `unresolved_links`, so a value left out never reads as an empty field
- **AND** it names a link column of an expanded child table by the column's name, as the reply's columns name it

#### Scenario: While a generation is coming a selection on a link field is refused for a retry

- **WHEN** a Records or Planning query filters, sorts or aggregates on a link field, or runs a saved view that filters on one, while a generation is coming
- **THEN** it refuses with the code `LINK_INDEX_WARMING` and a retry delay, and returns no rows
- **AND** the refusal names only link fields that the caller's field plan admits
- **AND** the same query answers from the generation once it is published

#### Scenario: A query that finds no current corpus warms it for later queries

- **WHEN** a Records or Planning query finds no corpus generation, one behind the vault's event checkpoint, or the corpus lock held
- **THEN** it starts one background warm that publishes a current generation, or joins the one that runs, and answers or refuses without waiting for it
- **AND** a later query borrows that generation and resolves the value with no `unresolved_links`

#### Scenario: A page write does not leave link selections refused

- **WHEN** a page write leaves the published generation behind the vault and a query that selects on a link field is refused
- **THEN** the same query answers once the warm that the refusal started has published

#### Scenario: Without a warm no generation is coming

- **WHEN** background warms are turned off
- **THEN** no generation is coming, and the query runs the bounded walk
