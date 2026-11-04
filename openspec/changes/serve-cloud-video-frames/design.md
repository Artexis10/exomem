## Context

See proposal.md. The Cloud image already installs PyAV through `media-cpu`.
`video_frames._get_frames_local` reads the complete original into a snapshot,
then runs the decoder synchronously. The media worker already supplies disposable
processes, memory brakes, a vault lock and optional node slots. Its durable
runner requires writer authority and publishes processing results, so it cannot
own a read request.

## Goals / Non-Goals

The Cloud path must preserve native frame results and exact disclosure without
moving credentials into a decoder process. It must use the existing file-custody
and resource machinery.

This change does not persist scene frames, add a model, create a job queue,
change the generic hydration cancellation API, or activate a live cell.
The local synchronous reader and other media operations retain their contracts.

## Decisions

### Reuse media selection and the default media budget

Add a closed `frames` engine to the maintained engine definitions. It selects
on-demand reads, not an extraction stage or model. Cloud defaults remain off;
the existing controller's per-cell and fleet engine selection carries the value.
Use the configured default media budget unless the operator supplies a frame
budget through the existing budget map. Do not add a second feature switch,
source-size refusal or fixed per-tenant budget.

Default-off preserves the existing heavy-engine admission contract. Its cost is
that a deployment must select the capability before clients can use it.
The operator pays that configuration cost through the existing engine route.

### Use an ephemeral frame child

A lightweight `video_frames_worker` owns the parent runner and child entrypoint.
The child uses native runtime bootstrap and the existing vault media lock before
loading native decode code. The runner uses media admission, pressure checks,
hard limits and `SlotClient` when configured. It neither recovers nor claims
`MediaJobStore` rows.

All hydration, hashing and decoding run in this child. Moving only decoding
would leave network or hashing work outside the enforceable request deadline.
The child reads a retained seekable original through existing held-file primitives;
it does not copy the video into the serving process.

### Keep authority in the parent with one preparation handshake

The child acknowledges path-independent admission before it touches the original.
Until the first authorization succeeds, original-specific failures return the same refusal as a missing path.
This hides storage failures and preparation interruptions for withheld originals.
The affected request loses detailed preparation diagnostics; admission and lock refusals keep their existing outcomes.

The child pins the original through `open_original` and retains it through
`hold_generic_file`. It reports the canonical path, complete file version,
digest and size, then waits before decoding.

The parent opens the same retained source. It accepts the child's digest only
for an exactly matching version and passes that artifact proof through both
`content_permits` and the `video_frame` binary boundary. Frame checks retain
`derived=True` and full disclosure. The parent then permits decoding.
It checks the held version and current authority again before returning images.
Both authorization decisions use the existing scopes that read policy and host
state outside the request snapshots. Otherwise the final check can reuse a
revoked decision that the request pinned before decoding.

This prevents a changed or withheld original from being decoded or released.
Concurrent edits can cost the caller a refused read and a narrower retry; that
cost is preferable to releasing a different original under an old decision.
Transporting principals and verified session context to the child would add a
second authority path. Reusing download recovery unchanged would substitute
download authorization for the frame boundary and require unrelated metadata.

### Give the Cloud invocation an async cancellation owner

The current MCP binding is synchronous. The pinned framework shields its thread
runner from cancellation, so setting a flag only in the request context's finalizer
cannot stop a still-running synchronous call.

At the existing server registration, wrap only the selected canonical Cloud
video command in a feature-local async owner. Run the existing bound invocation
through AnyIO with `abandon_on_cancel=True`; bind one cancellation event for the
synchronous frame runner and set it in `finally`. Public operation normalization
still selects the existing canonical command before this point. Media status
uses its separate contributor and is unchanged. Do not add a dispatch table or
change `RequestBudget` for other commands.

The runner polls cancellation, the existing deadline with its delivery reserve,
and resource pressure throughout preparation and decoding. Direct synchronous
runner calls use the existing configured request budget as their deadline.
It keeps the child leader unreaped until it stops the whole owned process group.
It then reaps the child and any tools adopted by the server through the existing
group reaper, closes held descriptors and releases the slot. The Cloud server is
PID 1; on an ordinary host, its init process owns orphaned non-child zombies.
Hydration can spawn a decrypt process, so stopping only the Python child is
insufficient. Interrupted cache reservations remain with
`clear_dead_reservations`; the runner does not delete foreign files or rows.

### Bound the small child protocol and native output

Use strict structured request, prepared-original, permission and result messages.
Validate lengths before reading payloads; never use pickle or an unrestricted
`communicate()` buffer. Bound the aggregate JPEG reply using the existing frame
count and encoded image dimensions. The worker's allocation ceiling does not
claim an outbound MCP transport limit. Keep resized
candidates rather than retaining every full-resolution decoded frame.
Compute duplicate-detection hashes from the original decoded images before
resizing. Preserve the local reader's selection and hash-failure behavior.

The protocol is a closed implementation format, not a classifier of natural
language. Malformed or oversized messages mean an invalid child result; refusal
protects the serving process. Its false-refusal cost is one failed frame read.

## Verification

The outcome check calls the registered Cloud tool over an isolated temporary
vault with a real small video and retained-original storage. It checks native
JPEG blocks, timestamps, hydration, exact original identity and no canonical
writes. The same registration must prove cancellation reaches owned work.

Use deterministic child doubles for withheld-before-decode, version change,
authority revocation, stalled preparation, malformed or oversized output, and
slot contention. Add a real codec child check for the proposed stream path and
record its memory and duration under the existing resource policy. Do not infer
model quality or client rendering from these protocol checks.

## Risks / Trade-offs

A background media owner can delay frame reads. Return a bounded busy result;
do not add foreground scheduling or preemption.

The preparation handshake adds a small protocol to maintain. It preserves one
authorization path and avoids a new scheduler or durable store.

## Migration Plan

1. Deliver the reviewed source with the frame engine off by default; existing cells keep their discovery.
2. Qualify the published package and image through the ordinary release owner; no deployment is implied.
3. Under existing operational authority, select the frame engine on an admitted cell; verify its resource and client acceptance.
4. To undo, remove the frame engine selection; media status and existing artifacts remain available.
