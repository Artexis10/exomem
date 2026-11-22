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

#### Scenario: Normal processing of a photo and a video

- **WHEN** a worker processes a governed photo and a governed video with previews enabled
- **THEN** each filing has one preview, with image pixels for the photo and one poster frame for the video
- **AND** the worker keeps the existing media outputs and original bytes

#### Scenario: The decoder is unavailable

- **WHEN** the decoder cannot produce a preview for an eligible filing
- **THEN** the preview is unavailable and the original’s other media stages still run

#### Scenario: Publication is busy

- **WHEN** a preview decodes but its guarded publication is busy
- **THEN** the worker retries preview publication within a bounded attempt limit
- **AND** a completed extraction stays completed

#### Scenario: Processing is replayed

- **WHEN** processing runs again with the same original digest and preview format
- **THEN** the filing still has one preview bound to that digest

### Requirement: Preview reads use the original's live access rules

Where `read_media` is offered, its preview action SHALL accept the canonical original path. It SHALL check provable denials before hydration and require current full release and derived-pixel access before returning the held preview. A video poster SHALL also satisfy the video frame boundary. An allowed caller whose current preview is missing SHALL receive a distinct unavailable result. The read SHALL use the existing read budget.

#### Scenario: A caller requests a stored preview

- **WHEN** an authorized caller selects an original with a current preview
- **THEN** the read returns the stored JPEG without decoding the original again

#### Scenario: Release is denied

- **WHEN** the current policy denies derived pixels, even if the caller can download the original
- **THEN** the preview request refuses without revealing whether the preview exists
- **AND** a denial that the precheck can prove leaves the original evicted

#### Scenario: The filing now names another original

- **WHEN** the original changes and processing has yet to publish a matching preview
- **THEN** an authorized request reports the preview unavailable

### Requirement: Removal retires the filing's preview

Media removal cleanup SHALL retire the associated preview record. A preview that remains after an interrupted cleanup SHALL be unavailable as soon as its original can no longer pass the read boundary. Eviction of an original to its store SHALL preserve the preview.

#### Scenario: The original leaves its path

- **WHEN** an original is removed from the vault or moved away from a filing
- **THEN** the former filing's preview is no longer readable
- **AND** media removal cleanup retires the record for the previous path
