## Context

Measured on the live alpha, 2026-09-10:

| Observation | Value |
|---|---|
| Endpoint serving both consumers | one, reached on the pooled host by Substrate and the direct host by the provisioner |
| Active time, 1–10 September | 771,672 s of a 849,600 s window, 91% |
| Compute billed | 198,578 CU-seconds, 55.16 CU-hours |
| Average size while active | 0.257 CU, the 0.25 floor |
| Autosuspend | provider default, five minutes |
| Always-on floor | 0.25 CU x 730 h = 182.5 CU-hours per month |
| Free allowance | 100 CU-hours per project per month |

Two hostnames hid the sharing. A per-service DSN audit that stops at the hostname concludes two independent databases exist; the endpoint id behind the pooled and direct names is the same.

## Goals / Non-Goals

**Goals.** Let the database suspend when the fleet is genuinely idle. Keep pickup latency proportionate to the cadence that already governs the work. Make the budget preflight able to name this failure class rather than tolerate it as an unexplained baseline.

**Non-Goals.** Relocating any store, which is rejected above. Reducing query volume for its own sake, since a serverless bill counts active time and not queries. Chasing storage cost, which is under a cent a month.

## Decisions

**Backoff, not a wake channel.** The obvious alternative is to keep a short poll and have the API poke the worker in-cluster when it inserts an operation. That buys back the latency but adds a listener, a port, an auth story and a new failure mode, for a workload whose upstream submitter is itself moving to a five-minute cadence. Backoff alone is the proportionate answer here, and a wake channel remains available later if pickup latency ever becomes the constraint rather than the bill.

**Both loops, identically.** `production.py` and `volume.py` carry the same four-line loop: run once, and sleep a fixed interval if there was nothing to do. Both get the same escalation so neither silently pins the endpoint open on behalf of the other.

**Sixty-second autosuspend.** With a five-minute suspend timer and a five-minute cadence the endpoint is awake continuously by construction, because the next wake arrives as the timer expires. Shortening the timer is what converts a widened cadence into an actual suspend, and it is a provider setting rather than code.

**Move the missed-run alert with the cadence.** The scheduler contract asserts `missedRunAlertAfterSeconds: 180`, and the chart fails the render if it changes without the pinned digest changing. A five-minute cadence under a three-minute missed-run threshold would alert on every healthy tick, so the threshold moves in the same commit or the change ships a permanent false alarm.

## Risks / Trade-offs

- **Pickup latency.** Worst case rises from about a second to the idle interval. Mitigated by the reset-on-work rule, so a busy fleet runs at the old cadence and only a genuinely idle one waits.
- **A widened schedule is a real reduction in reconciliation frequency.** The reconciler documents a twenty-minute renewal margin, which a five-minute cadence still satisfies with four ticks of headroom, far above alpha scale. That margin is the number to re-check before widening further.
- **Suspend adds a cold start to the first request after idle.** A lifecycle operation takes minutes, so a second or two of wake is immaterial there. It is visible on the first interactive request to Substrate after a quiet period, which is the honest cost of letting the database sleep.
- **The saving is all-or-nothing.** Any component left inside the window keeps the endpoint awake and returns the bill to its current figure, so partial delivery of this change is worth nothing and should not be merged as an increment.
