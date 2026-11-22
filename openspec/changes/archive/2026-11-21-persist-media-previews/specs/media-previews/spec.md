# Spec Delta

## Purpose

Keep a compact visual output from governed media processing so a caller can read the current image or video poster without decoding the original again.

## ADDED Requirements

### Requirement: Media processing persists a bounded preview

When the install enables previews, the existing media worker SHALL queue a preview for each eligible new image or video and keep one JPEG. Preview work SHALL stay within its decoder time and memory limits and leave other media outputs independent. A video SHALL use one poster frame. The longest edge SHALL be at most 512 pixels. The JPEG SHALL carry no original metadata. Its record SHALL identify the exact original digest that produced it.

#### Scenario: The inline debug worker is selected

- **WHEN** the install selects the inline debug worker with previews enabled
- **THEN** generation reports unavailable and admits no preview work
- **AND** an authorized read can still return a current stored preview

#### Scenario: The install disables the media worker

- **WHEN** the global media worker switch is off
- **THEN** preview generation reports unavailable and admits no preview work

#### Scenario: Normal processing of a photo and a video

- **WHEN** a worker processes a governed photo and a governed video with previews enabled
- **THEN** each filing has one preview, with image pixels for the photo and one poster frame for the video
- **AND** the worker keeps the existing media outputs and original bytes

#### Scenario: The decoder is unavailable

- **WHEN** the decoder cannot produce a preview for an eligible filing
- **THEN** the preview is unavailable and the original’s other media stages still run

#### Scenario: Publication is busy

- **WHEN** a preview decodes but its guarded publication is busy
- **THEN** the worker yields to foreground writers and retries within a three-attempt limit
- **AND** a completed extraction stays completed
- **AND** reconciliation leaves the terminal digest settled

#### Scenario: The original store is temporarily busy

- **WHEN** a retryable hydration refusal stops a preview claim
- **THEN** the worker releases the pin and delays the next claim without spending a publication attempt

#### Scenario: A process-mode worker publishes a preview

- **WHEN** a worker has decoded an original and the claim, binding and held version remain current
- **THEN** the worker installs the record under the cross-process consistency guard
- **AND** it flushes the record and its directories through the external state root before acknowledging publication

#### Scenario: The worker stops after installing the record

- **WHEN** a worker stops before acknowledging its final publication attempt
- **AND** the matching record is installed and ready
- **THEN** recovery acknowledges publication and preserves the successful outcome

#### Scenario: Storage cannot confirm the installed record

- **WHEN** publication acknowledgement cannot read or flush the matching record
- **THEN** the worker delays acknowledgement and gives ordinary media jobs priority
- **AND** acknowledgement retries without opening or decoding the original

#### Scenario: The installed record cannot be inspected during generation

- **WHEN** private storage cannot determine whether a current record exists
- **THEN** generation waits for a definite read before decoding or replacing the record

#### Scenario: Processing is replayed

- **WHEN** processing runs again with the same original digest and preview format
- **THEN** the filing still has one preview bound to that digest

### Requirement: Preview demand survives queue and derived state loss

When generation is enabled, Source capture, Evidence capture and discovery SHALL record preview demand when they write a new or rebound eligible filing.
The bounded media reconciliation pass SHALL recover marked current demand that has no queue row or has lost its successful output.
An unmarked historical filing and a marked digest with a terminal failure SHALL stay unqueued.

#### Scenario: Preview queue admission fails after filing

- **WHEN** a filing commits while the preview queue is unavailable
- **THEN** the filing succeeds and retains its preview demand for the exact digest
- **AND** a later bounded reconciliation pass recovers that demand

#### Scenario: Discovery first files a new original

- **WHEN** startup or periodic discovery writes the first exact companion binding of an eligible image or video
- **THEN** that write records preview demand for the digest of the original
- **AND** the same rule applies when the companion already contains unbound prose

#### Scenario: A restore omits the preview record

- **WHEN** a restore keeps a successful preview outcome and omits its record
- **THEN** reconciliation queues the still-current marked digest to rebuild the record
- **AND** both checks confirm the absence and the successful receipt still matches

#### Scenario: A successful preview record cannot be inspected during recovery

- **WHEN** reconciliation cannot confirm the record is absent
- **THEN** it retains the successful receipt and leaves generation unqueued for that digest

### Requirement: Preview reads use the original's live access rules

Where `read_media` is offered, its preview action SHALL accept the canonical original path. Within the existing recovery slot, it SHALL check provable access-tier, cold-blocked, RAW derived-pixel and path-only disclosure and connector denials before hydration. It SHALL require current full release and derived-pixel access, including lifecycle checks and byte-bound grants, before returning the held preview. A video poster SHALL also satisfy the video frame boundary. An allowed caller whose current preview is missing SHALL receive a distinct unavailable result. The read SHALL use the existing read budget.

#### Scenario: A caller requests a stored preview

- **WHEN** an authorized caller selects an original with a current preview
- **THEN** the read returns the stored JPEG without decoding the original again

#### Scenario: Release is denied

- **WHEN** the current policy denies derived pixels, even if the caller can download the original
- **THEN** the preview request refuses without revealing whether the preview exists
- **AND** a denial within the bounded precheck leaves the original evicted

#### Scenario: An organization cap rules out full release

- **WHEN** an applicable path-only organization cap limits a verified session below full release
- **THEN** the preview request refuses before hydrating the original
- **AND** another unresolved scope does not defer that known denial

#### Scenario: The connector boundary cannot load with no policy files

- **WHEN** the current policy has no files and the host's connector boundary is unavailable
- **THEN** the preview request refuses before opening or hydrating the original

#### Scenario: The filing now names another original

- **WHEN** the original changes and processing has yet to publish a matching preview
- **THEN** an authorized request reports the preview unavailable

### Requirement: Removal retires the filing's preview

Media removal cleanup SHALL retire the associated preview record. A preview that remains after an interrupted cleanup SHALL be unavailable as soon as its original can no longer pass the read boundary. Eviction of an original to its store SHALL preserve the preview.

#### Scenario: The original leaves its path

- **WHEN** an original is removed from the vault or moved away from a filing
- **THEN** the former filing's preview is no longer readable
- **AND** media removal cleanup retires the record for the previous path

#### Scenario: The original path becomes a directory

- **WHEN** cleanup confirms a directory occupies the original file's exact path
- **THEN** cleanup retires the obsolete record under the publication guard

#### Scenario: A deletion removes several original filings

- **WHEN** removal fans out to several preview records
- **THEN** cleanup reads their canonical companions in batches of at most 64 records
- **AND** cleanup rechecks the current filing under the publication guard before deleting each obsolete record
