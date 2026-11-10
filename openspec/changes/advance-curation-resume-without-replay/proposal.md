## Why

A curation run applies one step per `resume` call, and every `resume` carries the same run and plan identities. The common invocation boundary treats an identical mutation within 600 seconds as a retry and returns the stored reply. So the second `resume` of a run replays the first one's reply, and an agent advances a 33-step run by one step every ten minutes.

A maintenance job start has the same shape and is already exempt in code, but the contract does not say so.

## What Changes

- Each curation `resume` call gets its own implicit identity: the session scope plus the call's request id. A new `resume` call runs the run's next step. A retry that carries the same request id still gets the stored reply. A transport retry with a new request id runs the next step; the plan's approval covers every step, and the reply lists every committed step.
- The per-call identity keeps what an identity gives a call: the wait on a busy or warming writer, and the canonical identity that a vocabulary-bound `resume` needs.
- The implicit retry requirement names its two exemptions: a curation `resume` and a maintenance job start.

## Impact

- Code: the common invocation boundary in `writer_lease.invoke_command`.
- Specs: `write-path-resilience`, "Implicit Acknowledgement Recovery Retains Results For 600 Seconds".
- Explicit idempotency keys keep their 24-hour contract for every call, a `resume` included.
