## ADDED Requirements

### Requirement: An agent can rename a recording's speaker labels

A governed operation that agents can call SHALL rename the speaker labels of one recording. It SHALL take the recording's Source and a map from current labels to names. In each line of the extraction, part pages included, that starts with a label in the timed or the merged-turn grammar, it SHALL rewrite only that label, in place. It SHALL rewrite `speakers` from the labels that the lines then carry, and record each name with the label it replaced in `speaker_names`.

#### Scenario: An agent names one speaker

- **WHEN** the person tells the agent that Speaker B is the host, and the agent renames `Speaker B` to `Host` for that recording
- **THEN** every line labelled `Speaker B` reads `Host`, in the page and in its part pages
- **AND** `speakers` lists `Host`, and `speaker_names` records that `Host` replaced `Speaker B`

#### Scenario: A page without timestamps is renamed

- **WHEN** a recording's extraction holds merged turns such as `[Speaker B]: Welcome back.`, and the agent renames `Speaker B` to `Host`
- **THEN** that line reads `[Host]: Welcome back.`

#### Scenario: A line without a label stays byte for byte

- **WHEN** the extraction starts with a line that carries no label, and a hand-edited line runs on to a second line
- **THEN** the rename leaves the unlabelled line and the second line unchanged

### Requirement: A rename changes only labels

A rename SHALL write through the governed writer, guarded by each page's content hash. It SHALL NOT replace text in the transcript body, so the words of every line stay unchanged. It SHALL set `speaker_verification` to `human-verified` only when every label carries a name that a person gave. It SHALL refuse a name that is not one line or that holds a character of the label syntax, and a label that the recording does not carry, each with a code that names the rule.

#### Scenario: The words of a line stay unchanged

- **WHEN** the agent renames `Speaker B` to `Host`, and a line's words mention "Speaker B"
- **THEN** that line keeps those words

#### Scenario: A partial naming is not human-verified

- **WHEN** a recording has two labels and the agent names one of them
- **THEN** `speaker_verification` keeps its earlier value

#### Scenario: A name with label syntax is refused

- **WHEN** the agent gives a name that holds `]:`
- **THEN** the operation refuses with a code that names the rule, and no page changes

### Requirement: Names given by a person survive a reprocess

A reprocess that produces speaker labels SHALL carry each name of the prior extraction to the new label whose lines overlap that name's lines most. A line's span SHALL run from its start to the next line's start. A name SHALL carry only when the overlap covers more than half of the new label's speech time. A name on lines without a start time SHALL NOT carry. The result SHALL list each name that it could not carry. The prior extraction page SHALL keep its names.

#### Scenario: A name carries to the new label

- **WHEN** the owner reprocesses a recording with labels, and the new `Speaker A` covers mostly the lines that were named `Host`
- **THEN** the new extraction's lines for that label read `Host`, and `speaker_names` records it

#### Scenario: A reprocess without labels carries no names

- **WHEN** the owner reprocesses a named recording without labels
- **THEN** the result lists every name as not carried, and the prior extraction page keeps the names

#### Scenario: Names on merged turns do not carry

- **WHEN** the owner reprocesses with labels a recording whose named extraction holds merged turns without timestamps
- **THEN** the result lists every name as not carried, and the prior extraction page keeps the names

### Requirement: Keeping a voice is an owner action on a personal install

A personal install SHALL let the owner keep a voice from one label of one recording, through `enroll-speaker` and through its terminal Home. Both SHALL show that label's sample lines and wait for the owner to confirm. At confirmation, the install SHALL compute the label's ECAPA centroid from the label's timed line spans, and store it in the local voice-profile store under the given name. The action SHALL NOT be exposed as an MCP connector tool. A rename's reply SHALL name this action.

#### Scenario: The owner keeps the host's voice

- **WHEN** the owner runs `enroll-speaker` with a recording, the label `Host` and the name `Host`, and confirms the sample lines
- **THEN** the voice-profile store holds a `Host` profile with a 192-dim centroid
- **AND** a later diarised recording with that voice is labelled `Host`

#### Scenario: An agent cannot keep a voice

- **WHEN** an agent lists the connector's tools
- **THEN** no tool or action enrols a voice profile

### Requirement: A Cloud vault keeps a voice without a stored embedding

For a Cloud vault, the personal install SHALL read the original through the owner's signed-in Cloud download. It SHALL check the original's SHA-256 against the Source, and SHALL delete its copy after it computes the centroid. Cloud SHALL store no speaker embedding for any recording.

#### Scenario: A Cloud vault keeps no embedding

- **WHEN** the owner keeps a voice from a recording in a Cloud vault
- **THEN** the original is downloaded, checked and deleted on the personal install, and the cell stores no embedding
