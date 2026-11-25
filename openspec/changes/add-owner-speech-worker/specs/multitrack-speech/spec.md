## ADDED Requirements

### Requirement: Each track of a recording is transcribed on its own
Speech SHALL treat each audio stream of a container as a track. It SHALL treat the channels of one stream as separate tracks only when the upload, the owner, an agent or the device's metadata declares them separate microphones, and otherwise as one mixed track. Each track SHALL be transcribed on its own, and the transcript SHALL merge the tracks' segments by time and label each turn with its track's speaker name.

#### Scenario: Two streams, two speakers
- **WHEN** an owner uploads a video with two audio streams, one per speaker
- **THEN** each stream is transcribed separately and each turn carries its stream's speaker name

#### Scenario: Undeclared stereo stays one mix
- **WHEN** an owner uploads a stereo recording without declaring its channels separate
- **THEN** it is transcribed as one mixed track, as before

### Requirement: Track names come from metadata or from people
A track's speaker name SHALL come from the device or track metadata when present, otherwise from the owner or an agent, otherwise a stable default such as "Track 1". A later rename SHALL relabel that recording's turns.

#### Scenario: Name from the recorder
- **WHEN** a track's metadata names its transmitter
- **THEN** the transcript labels that track's turns with that name until someone renames it

### Requirement: Separate files from one session can be grouped
A governed operation SHALL let an owner or an agent group several recordings as one session and name each one's track. It SHALL align them by a shared start time or timecode from their metadata when present, and otherwise from their starts. The session SHALL get one merged transcript, and each file SHALL keep its own.

#### Scenario: Two wireless transmitters
- **WHEN** an agent groups two recordings from one interview, naming one track "Interviewer" and the other "Guest"
- **THEN** the session's transcript interleaves both tracks by time with those names

### Requirement: A track with several voices is diarised within the track
When a job asks for speaker labels, speech SHALL diarise a track that holds several voices, and SHALL label its turns with the track name and a speaker label. It SHALL never diarise a track declared to hold one voice.

#### Scenario: One room microphone at a wedding
- **WHEN** a session holds a lapel track for the officiant and a room track with several voices, and labels are asked for
- **THEN** the officiant's turns carry the lapel track's name and the room track's turns read "Room: Speaker A", "Room: Speaker B"
