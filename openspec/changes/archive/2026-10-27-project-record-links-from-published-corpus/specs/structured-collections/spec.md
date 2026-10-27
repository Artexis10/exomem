## ADDED Requirements

### Requirement: Link projection decides from the reader's visible pages

Record and planning link projection SHALL decide each bare title link and each memory reference over the pages that the reader may see. The projector SHALL remove every page that the reader may not see before it tests an index entry for emptiness, uniqueness or ambiguity. A stem, a title, a vault-root name or a memory id that matches a withheld page SHALL give the same result as a vault without that page.

The projector SHALL resolve these values from the last published corpus generation when one can be borrowed, and SHALL NOT build, refresh or wait for a corpus generation on the request thread. When no generation can be borrowed, it SHALL run the bounded synchronous walk. The cap of that walk bounds its cost and SHALL NOT decide disclosure: a vault over the cap with a published corpus SHALL keep the links that the reader may see.

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

- **WHEN** no corpus generation can be borrowed
- **THEN** the projector runs the bounded synchronous walk with its existing caps
- **AND** a vault over the cap withholds the bare titles and memory references that the walk could not index

#### Scenario: A stale memory reference is withheld

- **WHEN** the watcher has seen an outside edit that moved a memory id off a visible page and has not published it
- **THEN** the projector withholds the reference to that id
- **AND** after publication, the id resolves to the page that now holds it
- **AND** the projector does not scan the vault to recover
