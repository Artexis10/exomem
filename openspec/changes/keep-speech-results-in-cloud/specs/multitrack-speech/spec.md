## ADDED Requirements

### Requirement: Each track of a recording is transcribed on its own

Speech SHALL transcribe each track of a recording on its own. A track SHALL be one of these:

- an audio stream or a channel that the upload, the owner, the agent or published track metadata declares a separate microphone;
- a member file of a session.

Without a declaration, speech SHALL read the first audio stream as one mixed track, as today. The audio itself SHALL NOT declare tracks. The transcript SHALL merge the tracks' segments by start and label each turn with its track's name.

#### Scenario: Two declared audio streams

- **WHEN** the owner declares a video's two audio streams as the microphones "Host" and "Guest"
- **THEN** each stream is transcribed on its own, and the transcript's turns read `Host` and `Guest` in start order

#### Scenario: An undeclared second stream is not transcribed

- **WHEN** a screen recording carries a full mix in its first audio stream and an undeclared commentary stream
- **THEN** speech transcribes the first stream alone, as before

#### Scenario: An undeclared stereo file stays one mix

- **WHEN** a stereo recording arrives with no declaration and no device metadata about its channels
- **THEN** speech transcribes one mixed track, as before

#### Scenario: A declared dual-mono file is two tracks

- **WHEN** the upload declares the two channels of a recording as separate microphones
- **THEN** each channel is transcribed as its own track

### Requirement: Each track resumes on its own

Each track SHALL keep its own progress record and its own job timeout. A stop SHALL resume the track that it stopped, and SHALL NOT transcribe a finished track again. Status SHALL report the tracks of a recording that waits.

#### Scenario: A stop resumes one track

- **WHEN** a job stops after one track of two has finished
- **THEN** the next run starts the second track from its own progress record, and does not transcribe the first track again

#### Scenario: A long track does not spend another track's timeout

- **WHEN** a two-track recording's first track takes longer than one job timeout and advances its progress record
- **THEN** the stop does not count against the second track, and the first track continues from its record

### Requirement: Track names and start times come from published metadata or from people

Speech SHALL read track names and start times from these published fields: iXML `TRACK_LIST`, BWF `TimeReference`, and the container's stream title and timecode. A published or recorder-family field SHALL declare a stream or channel a separate microphone only where shipped data maps it so. Those mappings SHALL live in shipped data, not in code. A name that the owner or the agent gives SHALL win over metadata.

#### Scenario: An iXML track list names the tracks

- **WHEN** a recording's iXML `TRACK_LIST` names its channels "Host" and "Guest", and the shipped data maps that list as separate microphones
- **THEN** the turns of each track carry that track's name

#### Scenario: A stream title alone declares no track

- **WHEN** a video's second audio stream has the title "Commentary", and nothing declares it a microphone
- **THEN** speech reads the first audio stream alone

#### Scenario: A recorder family is added as data

- **WHEN** a mapping for a new recorder family's track fields is added to the shipped data
- **THEN** speech reads that family's tracks and names, and no code changes

### Requirement: A track keeps a segment only where it is the dominant source

Where a recording has more than one track, speech SHALL keep a track's segment only where that track is the dominant source over the segment's span. A track SHALL be dominant when its speech energy, normalised by its own speech level, is the highest or within a margin of the highest. The margin SHALL live in the speech decoding policy data, and the sidecar SHALL record its revision. The rule SHALL NOT apply to members aligned by an assumed start.

#### Scenario: Bleed is dropped

- **WHEN** the guest's words also reach the host's microphone at a lower level
- **THEN** the transcript holds those words once, in the guest's track

#### Scenario: Real overlap is kept

- **WHEN** the host and the guest speak at once at similar levels
- **THEN** the transcript keeps both segments, each in its own track

#### Scenario: Assumed alignment keeps every member's words

- **WHEN** two session members are aligned by an assumed start
- **THEN** speech keeps the segments of each member, and applies no dominance rule between them

### Requirement: Separate files from one session can be grouped

A governed operation SHALL group separate recordings as one session and name each member's track. The owner or an agent MAY call it. When every member carries a BWF `TimeReference` or a container timecode of the same kind, the operation SHALL align the members by it. Otherwise it SHALL align them by their start, and the session page SHALL record that alignment as assumed.

#### Scenario: Two transmitter files become one session

- **WHEN** an agent groups two files that each carry a BWF `TimeReference`, naming their tracks "Host" and "Guest"
- **THEN** one session page holds a transcript whose turns are aligned by those times and labelled by track

#### Scenario: Files without a shared time are aligned by start

- **WHEN** an agent groups two files that carry no shared time
- **THEN** the session page aligns them by their start and records the alignment as assumed

### Requirement: A session transcript takes its type from the registry

The session transcript SHALL be a page of the note type that the note-type registry marks as the session transcript type, in that type's folder. Code SHALL find that type by its registry attribute, not by an entry key. The page SHALL link each member Source.

#### Scenario: The session page uses the registry's type

- **WHEN** an agent groups two recordings as a session
- **THEN** the session page has the type that the registry marks as the session transcript type, in that type's folder

### Requirement: A session transcript stays current

When a member's extraction or speaker names change, speech SHALL find that member's session pages through the inbound-link index, and SHALL render each one again from its members. It SHALL NOT read every session page to find them. A section that the owner writes on a session page SHALL be kept.

#### Scenario: A member change updates the session

- **WHEN** a member's speaker labels are renamed, or the member is reprocessed, after grouping
- **THEN** the session page's transcript shows the member's new text and names, and the owner's section stays

### Requirement: A track is diarised only when labels are requested and it is not declared single-voice

Speech SHALL diarise a track only when speaker labels are requested for the recording and the track is not declared single-voice. The turns of a diarised track SHALL read `<track name>: Speaker A`. Where diarisation cannot run, the result SHALL report that the requested labels are unavailable, and the track names SHALL still label the turns.

#### Scenario: A room microphone is diarised within its track

- **WHEN** labels are requested for a recording whose "Room" track is not declared single-voice
- **THEN** the "Room" track's turns read `Room: Speaker A` and `Room: Speaker B`

#### Scenario: A declared single-voice track is not diarised

- **WHEN** labels are requested and the "Host" track is declared single-voice
- **THEN** the "Host" track is not diarised, and its turns read `Host`

#### Scenario: A cell labels by track without diarisation

- **WHEN** a Cloud cell transcribes a two-track recording with labels requested
- **THEN** the turns carry their track names, and the result reports that diarisation labels are unavailable
