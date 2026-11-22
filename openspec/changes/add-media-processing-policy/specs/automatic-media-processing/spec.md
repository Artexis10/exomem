## ADDED Requirements

### Requirement: Speech media carries a speaker-label preference

The media intake and explicit reprocessing operations SHALL accept an optional speaker-label choice for audio and video. An explicit choice SHALL override the deployment default for that item. An omitted choice SHALL use the previous explicit choice for that item, if any, or the deployment default. The system SHALL retain the choice across a worker restart, retry, discovery and publication. The system SHALL preserve original media and previous extractions under the existing custody and reprocessing rules.

The command interfaces SHALL accept `diarize` as a boolean on an intake file or media processing request. Direct multipart and resumable uploads SHALL accept the optional `diarize` field as `true` or `false`. The direct and resumable attachment CLI SHALL expose both choices. A caller sets the choice for a held upload when it presents the file handle for preservation.

Capture SHALL disclose when it reuses an existing recording and keeps its choice. A request for speaker labels SHALL refuse a supplied transcript on the same capture. A metadata-only Source correction SHALL carry the latest accepted choice for the original, including a choice in the queue. Bulk backfill SHALL use the stored choice with timed extraction and leave an explicit queued extraction to the worker.

#### Scenario: One deployment processes two different choices

- **WHEN** two queued speech items request opposite speaker-label choices
- **THEN** each job uses its own explicit choice
- **AND** the item that skips labels publishes a timed transcript without running diarization
- **AND** its speaker verification state is `not-requested`

#### Scenario: The worker restarts before extraction

- **WHEN** the worker restarts after intake accepts a speech item with an explicit choice
- **THEN** the recovered job uses the accepted choice
- **AND** the original and job retain their identities

#### Scenario: Requested labels are unavailable

- **WHEN** an item requests labels and the optional diarization capability cannot run
- **THEN** the item publishes the timed transcript if speech recognition succeeds
- **AND** the result reports that the requested labels are unavailable

#### Scenario: A caller changes the choice during explicit reprocessing

- **WHEN** a caller reprocesses completed speech media with a different speaker-label choice
- **THEN** the new extraction uses that choice and the prior extraction stays readable
- **AND** a later reprocess with an omitted choice keeps the new explicit choice
