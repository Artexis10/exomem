## ADDED Requirements

### Requirement: A further occurrence of an item gets its own row
A new observation can be a further occurrence of an item whose row the natural key already names, such as a second trial or a later service. The agent SHALL keep each occurrence as its own row. It SHALL NOT overwrite the earlier row and SHALL NOT drop the new observation. Where the current key cannot tell the occurrences apart, the agent SHALL change the key through `restructure` under the served `proactive_capture` disposition and the automatic-restructure bounds.

#### Scenario: A second occurrence gets its own row under silent capture
- **WHEN** `proactive_capture` is `silent`, a collection is keyed by part, and the user reports a second service of a part on a new date
- **THEN** the agent rekeys the collection by part and date, adds the second service as its own row, keeps the first row unchanged, and asks nothing

### Requirement: A new value of the same fact updates its row
For a new value of the same fact, such as a corrected price or a changed status, the agent SHALL update the row that the key names. Telling an occurrence from a new value SHALL be the agent's judgement of meaning, never a word list.

#### Scenario: A corrected value updates the row
- **WHEN** the user corrects the price stored for an item
- **THEN** the agent updates that item's row and does not restructure the collection

### Requirement: A further occurrence follows the capture disposition
Under `advisory` capture the agent SHALL propose the change in the user's words, and under `off` it SHALL initiate no restructure.

#### Scenario: Advisory capture proposes in the user's words
- **WHEN** `proactive_capture` is `advisory` and a second occurrence needs a new key
- **THEN** the agent asks one plain question about keeping each occurrence separately, names no key or field, and changes nothing before the answer

#### Scenario: Off capture initiates no restructure
- **WHEN** `proactive_capture` is `off` and a second occurrence needs a new key
- **THEN** no restructure happens and the earlier row is unchanged
