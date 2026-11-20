## MODIFIED Requirements

### Requirement: A passage result is bound to the current page

The passage path SHALL derive a result's passage and span from the Source's current bytes: the bytes that the published corpus proof holds current.
It MAY reuse a Source's selected chunks that an earlier request verified, while the published corpus records the same page hash and the eligible-set proof under which they were read still holds.
That proof SHALL name the recall checkpoint, the catalogue identity and the retirement generation, so that a watched edit or a Source erasure ends the reuse.
It SHALL NOT reuse chunks cut from bytes whose hash differs from the hash that the published corpus records for the page.
A Source edit that no file event reports does not move the proof, so until the proof moves a request MAY answer that Source at its published bytes.
It SHALL withhold a selected chunk whose stored text differs from the text of that chunk derived from the current bytes.
It SHALL withhold every chunk of a Source whose passage count, number of keys and number of passages in its body are not all equal.
The reply SHALL state the number of withheld chunks.

#### Scenario: A stale chunk is withheld

- **WHEN** a selected chunk's stored text does not equal the chunk derived from its Source's current bytes
- **THEN** that chunk is not returned
- **AND** the reply counts one withheld chunk

#### Scenario: A repeated selection reuses verified chunks until the page changes

- **WHEN** a passage request selects a Source whose chunks an earlier request verified, and the published page hash and the proof are unchanged
- **THEN** the request answers from those chunks without reading the Source page again
- **AND** after the Source is edited and republished, the next request reads the page again and withholds the chunk whose stored text no longer matches
