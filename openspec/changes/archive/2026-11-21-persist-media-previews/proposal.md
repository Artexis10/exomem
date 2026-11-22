# Proposal

## Why

The media worker derives text and index entries, but it keeps no compact visual output. A client needs a small image or video poster without asking the serving process to decode the original again.

## What Changes

- Let the existing media queue admit an opt-in preview item for each new image or video. Keep one JPEG with a longest edge of at most 512 pixels.
- Let a caller select the original's canonical path in `read_media`. Check provable denials before hydration and apply the original's live access rules before returning pixels.
- Bind each preview to the original that produced it. Replace it when that binding changes and remove it through media deletion cleanup.
- Keep preview generation off until the install enables it. A missing preview or decoder has no effect on the existing media outputs.

## Capabilities

### New Capabilities

- `media-previews` stores and returns one version-bound visual output from the existing media worker.

### Modified Capabilities

None.

## Impact

The media worker, local derived state and `read_media` share the existing decoders, binary custody and egress checks. No model, dependency or automatic backfill is required.
