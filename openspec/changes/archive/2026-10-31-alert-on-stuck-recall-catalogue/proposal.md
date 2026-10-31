## Why

A Cloud cell can keep serving while its recall catalogue stops advancing.
Writes still succeed and the cell still reads ready, so `cell-unavailable`
never fires, but recall keeps answering from the last published catalogue and
new notes stay out of search. The cell already reports this state in its
readiness answer as `retrieval.state` `lagging`, with the age of the unchanged
catalogue in `retrieval.catalogue_unchanged_seconds`. Nothing reads that answer
on a schedule, so a stall lasts until a person notices missing results.

cellctl derives every cell bearer. Cell ingress admits only the gateway to the
cell port, and admission pins that policy, so a compromised cellctl cannot
widen its own reach. cellctl therefore cannot read readiness on the port that
carries MCP without weakening that boundary.

## What Changes

- Decision: a Cloud cell started with `EXOMEM_CLOUD_HEALTH_PORT` also serves
  its readiness handler on that port, 8766. The port answers only
  `GET /health/ready`, with the same answer as the cell port, and answers every
  other request with an empty 404. Local installs never start it. The kubelet
  probes and the gateway stay on the cell port.
- The cell's `runtime-ingress` policy gains one rule: cellctl's pods, in
  cellctl's namespace, on TCP 8766 only. The chart's admission policy admits
  that rule and refuses cellctl on the cell port, any other peer and any other
  port. cellctl's egress gains TCP 8766 to cell runtime pods only.
- cellctl reads each serving cell's readiness once a minute on the ready pod's
  own IP, all cells at once, with a 3 s bound and a 64 KiB cap per cell. It
  parses only the two fields above and never logs the answer.
- cellctl raises `cell-recall-lagging` through the existing alert receiver
  while a serving cell lags on a catalogue unchanged for at least
  `alerts.recall_lag_warn_seconds`, 900 s in the observability contract. The
  chart renders the threshold into cellctl's environment, and cellctl refuses
  to start without it.
- Decision: the alert resolves only when the cell reads `retrieval.state`
  `ready`. The cell measures the age from when its own process first saw the
  catalogue, so a cell restarted during a stall reports a young age for an old
  stall. A younger lag, a lag with no age, a cell with no ready pod and a cell
  that does not answer each leave the alert unchanged.
- The render version changes, so every cell restarts once on rollout.

## Capabilities

### Modified Capabilities

- `cloud-platform-operations`: monitoring covers recall that serves a stalled
  catalogue, and cellctl reads cell readiness only on the health-only port.

## Impact

- Cell: `src/exomem/cloud_health.py`, `cloud_cell.py`, `server.py`, `server_assets.py`.
- cellctl: `alerts.py`, `reconcile.py`, `k8s_client.py`, `manifests.py`, `state.py`, `main.py`.
- Chart: `templates/cellctl.yaml`, the observability contract copy and its digest in `values.yaml`.
- Contract: `infra/contracts/observability-v1.json` gains `alerts.recall_lag_warn_seconds`.
- Runbook: `docs/runbooks/cloud-dedicated-host.md` gains "Cell recall lagging".
