## Why

During a long adoption `apply` on a hosted cell, `/health/ready` kept flipping to
503 (`retrieval_unavailable`) while the agent's own writes were in flight. Each
write moves the live recall projection briefly ahead of the maintained catalogue,
and the readiness probe takes the strict equality proof, so it sees the gap.
Kubernetes then removes the only pod from its Service, and the gateway refuses
the agent's next write with `CELL_NOT_READY`. In one rehearsal the pod log shows
949 readiness 503s and no write 503s.

Per-request recall already serves this lag after the required warm. Only the
health answer is stricter, so a cell that is serving correctly reports itself
unroutable.

## What Changes

- A managed process that has proved its catalogue exactly once since its warm
  began reports `retrieval.state: "lagging"` and stays `status: ready` while the
  catalogue trails a live projection that per-request recall can still serve.
  The answer names the lagging scopes and the seconds this process has seen the
  published catalogue unchanged.
- A fresh process, a lost live or published projection, a changed access-policy
  or catalogue identity, and rollback mode keep today's 503.
- Strict equality still sets `retrieval.state: "ready"` and `admitted: true`.
  `lagging` never sets `admitted`, adds no reason code, and does not change
  takeover eligibility.
- The ready-proof cache never reuses a `lagging` answer.

## Capabilities

### Modified Capabilities

- `install-readiness`: "Runtime Health Distinguishes Transport And Recall
  Admission" gains the `lagging` state. The scenario "Stale catalogue proof
  revokes readiness" becomes three scenarios.
- `recall-read-path`: strict health readiness stays strict. The serving
  `status` may follow a servable lag after a strict proof.
- `runtime-readiness-contract`: a served catalogue lag does not affect takeover
  eligibility.

`instant-start` "Lost projection cannot retain ready admission" is unchanged. Its
outcome, that retrieval reports warming or unavailable until proof converges,
still holds for a lost projection.

## Impact

- `src/exomem/readiness.py`: a `retrieval_proven` flag, set only while managed.
- `src/exomem/runtime_readiness.py`: the `lagging` state and the lag age.
- `src/exomem/lexstore.py`: the fallback half of
  `runtime_retrieval_catalog_admission` becomes a helper. The request path is
  unchanged.
- `src/exomem/server_assets.py`: the ready-proof cache skips `lagging`.
- Health payload gains `retrieval.lagging_scopes` and
  `retrieval.catalogue_unchanged_seconds` for the new state only. Readers that
  test `status` or `takeover_eligible` see no change. Readers that compare
  `retrieval.state` to `ready` see a new state and must treat it as serving.
- Out of scope: making the whole-catalogue rebuild converge under sustained
  writes.
