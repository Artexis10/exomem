## Why

A curation run applies one step per `resume` call, and every `resume` carries the same run and plan identities. The common invocation boundary treats an identical mutation within 600 seconds as a retry and returns the stored reply. So the second `resume` of a run replays the first one's reply, and an agent advances a 33-step run by one step every ten minutes.

A maintenance job start has the same shape and is already exempt in code, but the contract does not say so.

## What Changes

- A curation `resume` runs its next step each time it is called. It is not answered from the implicit retry window. The run store already commits each step exactly once, so a lost reply costs nothing: the next `resume` reports every committed step.
- The implicit retry requirement names its two exemptions: a curation `resume` and a maintenance job start.

## Impact

- Code: the common invocation boundary in `writer_lease.invoke_command`.
- Specs: `write-path-resilience`, "Implicit Acknowledgement Recovery Retains Results For 600 Seconds".
- Explicit idempotency keys keep their 24-hour contract for every call, a `resume` included.
