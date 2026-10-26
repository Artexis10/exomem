## ADDED Requirements

### Requirement: A corrected Source preserves the previous original

`capture_source` SHALL accept a correction naming one predecessor, its historical page hash and a reason.
It SHALL create one successor and an immutable correction receipt atomically, without rewriting predecessor bytes.
The predecessor SHALL have an existing canonical identity and satisfy current point authority.
A stale hash or an existing successor SHALL refuse the correction without an orphan capture.

#### Scenario: A corrected original replaces a retyped capture

- **WHEN** a caller captures a corrected original with the predecessor's current historical hash
- **THEN** the old Source and its original remain byte-identical
- **AND** the successor and receipt explicitly identify the correction
- **AND** two concurrent corrections of that predecessor cannot both commit

### Requirement: Source consumers follow the canonical correction chain

Page reads, compiled-source admission and compilation proposals SHALL resolve a cited Source to its current successor.
Custody-page downloads SHALL resolve its current original; explicit artifact paths SHALL remain exact.
The resolver SHALL authorize every Source independently and use canonical receipts rather than derived graph currency.
Historical reads SHALL remain available with `follow_corrections=false`; exact unit reads SHALL remain exact.

#### Scenario: An existing citation selects the corrected original

- **WHEN** a client reads or compiles from a corrected Source citation
- **THEN** it receives the current Source identity and current contents with an explicit resolution envelope
- **AND** existing compiled citations remain unchanged

#### Scenario: A successor is withheld or the chain is invalid

- **WHEN** a successor is withheld, missing, cyclic or malformed
- **THEN** current-source resolution returns the ordinary content-free unavailable response
- **AND** it exposes no successor reference, reason, hash or hop count
- **AND** it does not present a known historical original as current

#### Scenario: A vault export retains history

- **WHEN** a vault with corrected Sources is exported and restored
- **THEN** every Source version, original artifact and correction receipt remains preserved
