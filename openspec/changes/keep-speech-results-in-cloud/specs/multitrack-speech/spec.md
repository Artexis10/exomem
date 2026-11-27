## ADDED Requirements

### Requirement: Each track of a recording is transcribed on its own

Speech SHALL transcribe each track of a recording on its own. A track SHALL be an audio stream or a channel that the owner or an agent declares a separate microphone, or that published track metadata declares one where shipped data maps it so. Without a declaration, speech SHALL read the first audio stream as one mixed track, as today. The audio itself SHALL NOT declare tracks. The transcript SHALL merge the tracks' segments by start and label each turn with its track's name.

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

- **WHEN** a recording's sidecar declares its two channels as separate microphones
- **THEN** each channel is transcribed as its own track

### Requirement: The owner or an agent declares a recording's tracks

The owner or an agent SHALL declare which audio streams or channels of one recording are separate microphones, with `tracks` on media process or reprocess or on an uploaded file's handle. The recording's Source SHALL record the declaration, and a new extraction SHALL keep it. A declaration SHALL be checked before anything is written. It SHALL name at most a bounded number of tracks, because each track is one full transcription.

#### Scenario: An agent declares a video's two audio streams

- **WHEN** an agent processes a video with `tracks` that name its two audio streams "Host" and "Guest"
- **THEN** the next extraction reads each stream on its own, each line names its track, and the Source records the declaration

#### Scenario: A declaration given at upload

- **WHEN** an agent stores a recording whose file handle declares two tracks
- **THEN** the recording's Source records the declaration before its first extraction

#### Scenario: A new extraction keeps the declaration

- **WHEN** a recording whose sidecar declares two tracks is extracted again
- **THEN** the engine reads those two tracks, and the sidecar still holds the declaration

#### Scenario: A declaration that cannot be read changes nothing

- **WHEN** a declaration names more tracks than the bound, names two tracks alike, or gives a name with label syntax
- **THEN** the request is refused with what to change, such as how many tracks a declaration takes, and the Source and the queue stay as they were

#### Scenario: A repeat of the declaration writes nothing

- **WHEN** an agent sends the declaration that the recording's Source already records
- **THEN** nothing is written and nothing is queued

#### Scenario: A transcribed recording takes a declaration when it is processed again

- **WHEN** an agent declares tracks for a recording whose transcript is complete
- **THEN** process refuses and names reprocess, and reprocess applies the declaration to the next extraction

#### Scenario: A declaration replaces speaker labels only when asked

- **WHEN** an agent reprocesses with tracks a recording whose transcript holds speaker labels or names
- **THEN** the request is refused unless it sets `replace_labelled`, and with it the old transcript stays on a prior page

### Requirement: A declared track that the file lacks is skipped

A declared track that the file does not have SHALL be skipped, and the sidecar SHALL record which track was skipped and why. Where the file has none of the declared tracks, speech SHALL read the first audio stream as one mixed track.

#### Scenario: One declared stream is missing

- **WHEN** a sidecar declares three audio streams and the file has two
- **THEN** speech transcribes the two streams, and the sidecar records the third track as skipped with its reason

#### Scenario: No declared track is in the file

- **WHEN** a sidecar declares only streams that the file does not have
- **THEN** speech reads the first audio stream as one mixed track, and the sidecar records each skipped track

### Requirement: Each track resumes on its own

Each track SHALL keep its own progress record until the worker publishes the transcript. A stop SHALL resume the track that it stopped, and SHALL NOT transcribe a finished track again. A job's stop limit SHALL count the progress of all its tracks, so a stop that advanced any track is progress.

#### Scenario: A stop resumes one track

- **WHEN** a job stops after one track of two has finished
- **THEN** the next run starts the second track from its own progress record, and does not transcribe the first track again

#### Scenario: A stop before publication transcribes no track again

- **WHEN** the worker stops after every track has finished and before the transcript is published
- **THEN** the next run transcribes no track again, and publication discards every track's record

### Requirement: Track names and start times come from published metadata or from people

Speech SHALL read track names and start times from these published fields: iXML `TRACK_LIST`, BWF `TimeReference`, and the container's stream title and timecode. An iXML track's place in the file SHALL be its `INTERLEAVE_INDEX`, and its name SHALL be its `NAME`. A published or recorder-family field SHALL declare a stream or channel a separate microphone only where shipped data, not code, maps it so. A name that the owner or an agent declares SHALL win over metadata.

#### Scenario: An iXML track list names the tracks

- **WHEN** a recording's iXML `TRACK_LIST` names its channels "Host" and "Guest", and the shipped data maps that list as separate microphones
- **THEN** the turns of each track carry that track's name

#### Scenario: An iXML track list alone declares no microphone

- **WHEN** a stereo recording's iXML `TRACK_LIST` names its channels "MixL" and "MixR", and the shipped data maps no field to a microphone
- **THEN** speech transcribes one mixed track

#### Scenario: A stream title alone declares no track

- **WHEN** a video's second audio stream has the title "Commentary", and nothing declares it a microphone
- **THEN** speech reads the first audio stream alone

#### Scenario: A recorder family is added as data

- **WHEN** a mapping for a new recorder family's track fields is added to the shipped data
- **THEN** speech reads that family's tracks and names, and no code changes

### Requirement: A track name can stand in a label span

A track name SHALL meet the label syntax's name rule: one line, no `[`, `]` or `:`, and no longer than a speaker name. A published name that fails the rule SHALL label no track. A declared name that fails the rule SHALL refuse the declaration with the reason. Published names that repeat SHALL take labels that no other track holds.

#### Scenario: A published name cannot forge a turn

- **WHEN** a published track name holds a label bracket, a colon or a line break
- **THEN** that name labels no track, and no line of the transcript reads as a turn or a heading that the name wrote

#### Scenario: A declared name with label syntax is refused

- **WHEN** a declaration names a track `Host]: I approve`
- **THEN** speech refuses the declaration and says that a track's name must not hold `[`, `]` or `:`

#### Scenario: Repeated published names stay distinct

- **WHEN** a file publishes the track names "Input", "Input" and "Input 2"
- **THEN** the tracks read "Input", "Input 3" and "Input 2", and each channel's words appear once

### Requirement: Published microphones that cannot all be labelled are read as one mix

Speech SHALL read a recording as one mix when its published microphones are fewer than two, more than the track bound, or include a track with no usable name.

#### Scenario: One published microphone is one mix

- **WHEN** a mono file publishes one track that the shipped data maps to a microphone
- **THEN** speech transcribes one mixed track with no labels

#### Scenario: A track with no usable name keeps every word

- **WHEN** a file publishes two microphones and one of them has a name that fails the name rule
- **THEN** speech reads the recording as one mix, so no track's words are lost

### Requirement: A track keeps a segment only where it is the dominant source

Where a recording has more than one track, speech SHALL keep a track's segment only where that track is the dominant source over the segment's span. A track SHALL be dominant when its speech energy, normalised by its own speech level, is the highest or within a margin of the highest. The margin SHALL live in the speech decoding policy data, and the sidecar SHALL record its revision.

#### Scenario: Bleed is dropped

- **WHEN** the guest's words also reach the host's microphone at a lower level
- **THEN** the transcript holds those words once, in the guest's track

#### Scenario: Real overlap is kept

- **WHEN** the host and the guest speak at once at similar levels
- **THEN** the transcript keeps both segments, each in its own track

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
