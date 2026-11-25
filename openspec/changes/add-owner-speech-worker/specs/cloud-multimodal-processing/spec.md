## MODIFIED Requirements

### Requirement: A Cloud cell extracts media inside the cell

An Exomem Cloud cell SHALL extract documents, images, audio and video only in its serialized, disposable media worker, inside the cell's own container, except that a cell in remote speech SHALL hand its speech stage to a speech worker that the owner authorized, as the `remote-speech-worker` capability states. The media bytes and every other extraction process SHALL stay in the cell, and no third party SHALL receive media for extraction. Later reads of the stored extracted text SHALL follow the capabilities that own them, including an instrument placement that a tenant opts into under the `sensed-epistemic-model` capability. The serving process SHALL run no extraction engine. It MAY hold the image query encoder that the image lane needs, and the idle reaper SHALL unload that encoder.

#### Scenario: An image is extracted in its own cell

- **WHEN** a Cloud cell processes an uploaded image
- **THEN** extraction runs in that cell's media worker child
- **AND** no third party receives the image or its bytes for extraction

#### Scenario: The serving process holds no extraction engine

- **WHEN** media jobs are running in a Cloud cell
- **THEN** the serving process has loaded no OCR, speech, document or caption engine

#### Scenario: Remote speech goes only to the owner's authorized speech worker

- **WHEN** a cell in remote speech processes an uploaded recording
- **THEN** its speech stage is served only to a principal that holds the owner's speech authorization and the item's live lease
- **AND** no other party receives the recording for extraction
