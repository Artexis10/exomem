# Media processing policy

## Why

Some recordings need speaker labels, and others only need a timestamped transcript. Media processing also competes with interactive reads and writes when both need the same resources.

## What Changes

- Let a caller request or skip speaker labels on an audio or video item. An omitted choice uses the deployment default, which is off by default.
- Preserve the choice across retries and reprocessing. Publish the timed transcript and report unavailable labels if the optional dependency cannot run.
- Make background speech work yield to interactive service pressure and resume from durable state when capacity returns.
- Keep speech recognition and speaker labeling in the existing deterministic media pipeline.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `automatic-media-processing` gains a durable per-item speaker-label choice and reports the outcome.
- `resource-governance` gains background speech admission and safe recovery under interactive contention.

## Impact

The existing media intake, reprocessing, worker supervisor and result-status paths carry the policy. Deployments retain their existing defaults and optional diarization dependencies.
