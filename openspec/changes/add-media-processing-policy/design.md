# Media processing policy design

## Context

The durable media queue already retains extraction choices for language and reprocessing. Media children run at lower CPU priority. The supervisor already enforces memory admission and a job watchdog. The parent publication path already defers to foreground writes, with an aging rule to publish results under sustained load.

## Goals

- Carry one per-item speaker-label choice from authorized intake or reprocessing through the existing job.
- Keep foreground service responsive during media compute and let pending work resume from the existing ledger.

## Decisions

Extend the existing intake and reprocessing operations with an optional `diarize` argument. Legacy callers can omit it. The existing speaker outcome metadata communicates labeling success or unavailability. The interface follows the existing-operation guidance in [Writing effective tools for agents](https://www.anthropic.com/research/writing-tools-for-agents).

Carry the explicit preference alongside the existing extraction options. The existing sidecar and durable queue own accepted intent and extraction state. Reuse the pending claim, result commit and prior-extraction path. Keep the optional model failure on the existing soft-fail transcript path.

Keep the existing media priority, queue, watchdog and publication policies. The one-core Linux reproduction showed that a quota shared across many run queues lets media and serving threads spend the period's budget in parallel. The foreground control also throttled, so throttling alone cannot distinguish media contention. An application pause would need to fence native compute away from the child's ledger and publication writes. Use the kernel scheduler and the deployment's CPU placement instead. This follows the simplicity guidance in [Building effective AI agents](https://www.anthropic.com/research/building-effective-agents) and the [Linux bandwidth contract](https://docs.kernel.org/scheduler/sched-bwc.html).

For a Linux deployment that gives the whole service a one-core quota, use the same assigned logical CPU for the server and its descendants. A running media worker retains its lower scheduling priority. It can make progress while a request is served and resume at full available capacity when foreground work subsides. The deployment owns CPU placement, including which CPU each co-located service uses. Other deployments retain the existing controls and verify their own envelope.

## Verification and rollout

Use the `resource-governance` shared-host envelope and the `graph-value-benchmark` scale of 3,600 pages and 20 foreground streams. Compare the same served traffic and media work with and without shared CPU placement. Record success counts and tail latencies for both arms. Prove the serving and media processes share the CPU and quota and retain different priorities. Check that the same speech job makes progress during the burst, publishes one timed transcript and preserves the original. Use the ordinary reviewed release and verify the workflow in an isolated installed runtime.
