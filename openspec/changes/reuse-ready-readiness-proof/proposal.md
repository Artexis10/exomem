## Why

Every Exomem Cloud cell answers a kubelet readiness probe every 5 seconds, and each probe ran the full readiness proof: catalogue checks over SQLite, a coordination-status thread and a package-metadata lookup. On production 0.108.0 cells that proof was the largest part of 33–47 millicores of idle CPU per cell, against a 10-millicore target.

## What Changes

- `/health/ready` may answer from a ready proof for up to 30 seconds. A not-ready or failed proof is never reused, and a standby's answer is never reused.
- A reused answer is void as soon as the process records an admission change (warming, an unready mark from recall or the lexical store, re-admission) or promotes a standby.
- The response reports `proof_age_seconds`, so a reused answer says how old it is.
- A change that only a fresh proof can detect, such as another process advancing a projection, now reaches readiness within 30 seconds instead of on the next probe.

## Impact

- Affected spec: `install-readiness` (Runtime Health Distinguishes Transport And Recall Admission).
- Affected code: `src/exomem/server_assets.py`, `src/exomem/runtime_readiness.py`, `src/exomem/service_standby.py`.
