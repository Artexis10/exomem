## Purpose

Let an enabled Cloud client inspect a video's native frames without placing decoding or complete-original allocation in the serving process.

## ADDED Requirements

### Requirement: Cloud video reads use explicit engine selection

A Cloud cell SHALL keep on-demand frame reads off by default. Existing per-cell and fleet media selection SHALL enable the frame operation without enabling speech or persistent scene-frame generation. Disabled cells SHALL retain media status and exclude only the video operation. Local frame calls SHALL keep their existing contract.

#### Scenario: A selected cell exposes frame reads

- **WHEN** an operator selects the frame engine for a cell
- **THEN** that cell lists the video operation of the existing media reader
- **AND** an unselected cell retains its previous discovery and admission

#### Scenario: Selecting frame reads does not select transcription

- **WHEN** a cell enables only the frame engine
- **THEN** a frame request can return images without running a model or scheduling transcription
- **AND** the media-status operation remains available

### Requirement: Native frame results preserve the established interface

An admitted frame request SHALL retain the existing path, window and frame-limit inputs, timestamp metadata and ordered JPEG image blocks. The server SHALL preserve exclusion, path and disclosure rules across public and supported compatibility spellings. Unavailable dependencies or resources SHALL return an honest refusal with no partial image result.

#### Scenario: A caller inspects a retained video

- **WHEN** an authorized caller requests a valid window from a retained original
- **THEN** the result contains the existing metadata and native JPEG blocks in timestamp order
- **AND** each image respects the established dimension and frame-count limits

### Requirement: Authorization is bound to the original decoded

The serving process SHALL authorize frames against the exact original identity, version and digest retained for decoding. It SHALL refuse withheld or changed originals before decoding, and check current authority and original version again before releasing frames. A refusal SHALL not reveal an excluded original's existence. Original hydration MAY update verified cache state, but the request SHALL not write knowledge content or durable processing jobs.

#### Scenario: An original changes during preparation

- **WHEN** the decoder prepares one original version and the serving process observes another
- **THEN** the request refuses before decoding
- **AND** it returns no images from either version

#### Scenario: Authority changes while decoding

- **WHEN** a prepared original loses frame disclosure authority before the response is released
- **THEN** the server discards the images and returns the governed refusal
- **AND** no note, sidecar or durable processing job is created

### Requirement: Frame work has a bounded request lifetime

Cloud preparation, hydration, hashing and decoding SHALL run in an owned disposable child under the existing media admission, lock, memory, pressure and slot controls. The serving process SHALL not allocate the complete original or run its decoder. One request deadline SHALL cover preparation through response delivery. Cancellation, deadline or pressure SHALL stop owned descendants and release their resources; a competing media owner SHALL receive a bounded retryable busy outcome.

#### Scenario: A caller cancels a stalled preparation

- **WHEN** the registered Cloud frame request is cancelled while its child is preparing the original
- **THEN** the request owner stops and reaps the child and its descendants
- **AND** it releases the media slot only after that work stops

#### Scenario: Background media owns the slot

- **WHEN** another media operation holds the cell's slot or vault media lock
- **THEN** a frame request does not start competing decoding
- **AND** it returns a bounded retryable busy outcome

### Requirement: Child results are bounded before allocation

The serving process SHALL validate the child's protocol and declared sizes before allocating its metadata or image payloads. A malformed, oversized, incomplete or stale response SHALL produce no image result. The request SHALL retain the existing cleanup and recovery owner for interrupted hydration.

#### Scenario: A child declares an oversized image

- **WHEN** a child declares a payload beyond the frame protocol's admitted size
- **THEN** the parent refuses it before allocating that payload
- **AND** it stops owned work and releases its resources
