## ADDED Requirements

### Requirement: GPU Speech Decoding Runs Batched With Timestamped Lines

When the local speech stage runs on CUDA, it SHALL decode through the engine's batched pipeline with the batch size and options that `media_models.json` declares, and it SHALL keep segment timestamps so that each transcript line covers a citable span. When the stage runs on CPU, it SHALL decode sequentially. The sidecar SHALL record the policy revision that produced it.

#### Scenario: A GPU run decodes batched

- **WHEN** the speech stage processes a recording on CUDA
- **THEN** it decodes with the batched pipeline and the declared batch size
- **AND** the transcript lines carry segment timestamps, not one line per decoding window

#### Scenario: A CPU run stays sequential

- **WHEN** the speech stage processes a recording on CPU
- **THEN** it decodes sequentially with the declared sequential options

### Requirement: A Caller Declares One Language Or Several

A processing or reprocess request SHALL accept either one language code or a multilingual mode, and SHALL refuse a request that carries both. A declared mode SHALL follow the same precedence as a requested language and SHALL pin on the sidecar. In multilingual mode the engine SHALL detect the language per decoding window. The sidecar SHALL record `asr_language_mode` as `single` or `multilingual`.

#### Scenario: Several languages declared at first processing

- **WHEN** a caller processes a new recording with the multilingual mode
- **THEN** the engine detects the language per decoding window
- **AND** the sidecar records `asr_language_mode: multilingual` and `asr_language_source: requested`

#### Scenario: Both a language and the multilingual mode

- **WHEN** a request carries a language code and the multilingual mode together
- **THEN** the request is refused with a reason that names the conflict
- **AND** no job is queued
