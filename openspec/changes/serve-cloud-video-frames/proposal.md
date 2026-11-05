## Why

Cloud clients cannot inspect a video's frames through the existing media reader.
The image now includes the decoder, but the local reader copies the complete
original and decodes inside the serving process. Cloud admission needs a bounded,
read-only execution path that preserves the original's disclosure and identity.

## What Changes

- Admit the existing video operation of `read_media` on an explicitly enabled Cloud cell.
- Keep the heavy capability off by default; disabled or busy calls fail softly without starting decoding.
- Prepare, hydrate and decode originals in a disposable child under existing media admission and resource controls.
- Keep authorization in the serving process, bound to the exact original prepared by the child.
- Return the existing JPEG image blocks and timestamp metadata without creating notes, sidecars or durable media jobs.
- Stop owned work on cancellation, deadline or pressure, then release its resources.
- Preserve ordinary local calls, compatibility spellings and the media-status operation.

## Capabilities

### New Capabilities

- `cloud-video-frame-reads`: Governed, bounded on-demand frame reads on Cloud, including original identity, request lifetime and native image results.

### Modified Capabilities

None. The existing local frame interface keeps its contract; this change adds its Cloud admission boundary.

## Impact

The batch uses the existing decoder, original hydration, retained-file proof,
media lock, node slot and memory brakes. It adds a narrow ephemeral child protocol
and a cancellation-aware owner at the existing MCP binding. It does not add a
scheduler, durable job store, model dependency or inference service.
Cloud discovery and owning generated artifacts follow the new default-off configuration.
A source release does not authorize a deployment, configuration write or provider rescan.
