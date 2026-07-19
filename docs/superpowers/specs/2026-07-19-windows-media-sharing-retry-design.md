# Windows media sharing retry and connector edit compatibility

Date: 2026-07-19
Status: approved

## Problem

Exomem 0.25.2 correctly recovered media jobs that had been failed by follower/writer coordination errors. While draining that queue on Windows, existing sidecar updates intermittently failed at the atomic `os.replace` boundary with `WinError 5`. The worker treated the first denial as a terminal media failure. The affected sidecars remained valid and pending, but their jobs stopped automatically.

The same live smoke also proved that ChatGPT can now route to `edit_memory`; however, one invocation encoded each batch edit as a JSON string rather than an object. Exomem rejected the request before mutation, despite the encoded values representing the documented edit shape.

## Evidence and cause

- Every observed sharing failure occurred while replacing an existing sidecar from a same-directory `.exomem-batch-*` staging workspace.
- The failures were live on 0.25.2, spanned multiple media types and path lengths, and left no staging residue.
- The destination sidecars remained intact and in `processing_state: pending`.
- Windows Defender Controlled Folder Access emitted matching audit events for the service's SYSTEM Python process and vault paths. This identifies filesystem/filter contention as a strong environmental signal, not proof of the exclusive blocker.
- Syncthing is independently unhealthy because C: free space is below its 1% floor. That must be repaired operationally, but low disk space does not explain or excuse terminalizing a transient sharing denial.
- The media worker's existing commit loop retries a false compare-and-swap result, but a `PermissionError` escapes immediately and is marked `FAILED` after one attempt.

## Design

### Media sharing violations

Recognize only Windows sharing-style `PermissionError`s from the guarded atomic commit boundary (`winerror` 5 or 32). Requeue the claimed media job with its attempt count preserved, exit the child with the existing lock-unavailable code, and let the supervisor apply the 30-second backoff. A job receives at most three automatic sharing retries; a persistent denial is then retained as an actionable terminal failure.

Each retry reruns the full media commit path and therefore captures fresh source, destination, and mutation guards. Exomem will not loop directly around `os.replace`, weaken path guards, retry arbitrary permission failures, or retry indefinitely.

Startup recovery will requeue only historical failures matching Exomem's exact staged atomic-replacement signature and remaining below the retry limit. Genuine permission failures and exhausted sharing violations remain failed.

### ChatGPT batch-edit compatibility

Keep the public `edit_memory` name and object-array schema. Before semantic validation, normalize each element as follows:

1. Accept an object unchanged.
2. If an element is a string, decode it as JSON and accept it only when the decoded value is an object.
3. Reject malformed JSON, non-object JSON, missing fields, and all other input types through the existing `INVALID_EDIT` path.

This is a compatibility shim for connector serialization, not a relaxation of edit semantics. Expected hashes, semantic preflight, idempotency, and guarded commit behavior remain unchanged.

## Verification

- Unit tests cover first-attempt sharing denial followed by success, bounded exhaustion, attempt accounting, startup recovery selection, and preservation of unrelated permission failures.
- Unit tests cover object edits, JSON-object strings, malformed strings, and non-object JSON.
- Existing transactional-write, media-worker, command-surface, generated-capability, OAuth, and package tests remain green.
- Production smoke verifies public readiness and tool fingerprint, OAuth refresh metadata, authenticated `edit_memory` routing, recovery of the exact sharing failures, queue progress, and zero new coordination failures.

## Rollout

Ship as 0.25.3, upgrade the laptop writer without changing its CUDA Torch build, restart the service, and requeue only eligible sharing failures. Keep the desktop on follower status until it is upgraded from 0.24.0. Separately free space on C: and restore Syncthing health; explicitly allow the deployed service Python in Controlled Folder Access before changing CFA from audit to enforcement.
