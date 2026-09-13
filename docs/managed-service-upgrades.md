<!-- authority:non-specification -->

# Managed Linux service upgrades

Managed mode keeps the public HTTP listener and accepted connections in a stable
supervisor while replacing its private worker. It is opt-in for a Linux or WSL
`systemd --user` release service. macOS, Windows, direct HTTP and stdio installs
keep their existing behavior.

Install a release that includes `exomem.service_manager`, then enable the mode:

```sh
bash scripts/install-service.sh --release --seamless --profile standard
```

The first enablement uses the existing stopped-transition proof and changes the
public listener owner once, so connected clients must reconnect for that
bootstrap. On an existing unit, the installed `service.env` is the authority:
the installer reads its vault, state root, OAuth issuer and other variables and
leaves its bytes unchanged. It refuses configuration changes during bootstrap.
A fresh managed install reads the requested dotenv file.

If first enablement renders the managed unit but fails to start or verify it,
the installer leaves the service stopped with its legacy transition receipt.
Correct the cause, then repeat the same bootstrap options with
`--resume-stopped-transition`:

```sh
bash scripts/install-service.sh --release --seamless --profile standard --resume-stopped-transition
```

Repeat any original `--service-root` and `--package-version` pins. This entry
is refused unless the exact installer receipt proves that the managed unit and
its prior listeners are stopped. If the manager already recorded an active
launcher release, resume pins to that exact version; a different explicit
`--package-version` is refused before installation. A pending manager worker
transition also blocks installer recovery. Once bootstrap succeeds, the receipt is
cleared and the installer cannot re-enter a managed unit. Later worker failures
use the private `--resume` command below, not this bootstrap option.

Later worker releases are staged in a new venv under `<service-root>/releases`
while the old worker serves. The launcher venv at `<service-root>/.venv` is not
replaced. Run:

```sh
bash scripts/upgrade.sh --profile standard
bash scripts/upgrade.sh --package-version 0.80.0 --cli-sync always
```

The script discovers the managed unit, asks its private control socket to
switch to the staged version, verifies the reported active release, then uses
the existing CLI sync and managed-install manifest helpers. `--cli-sync auto`
updates an existing uv-managed CLI, `always` installs it if needed, and `never`
leaves it alone. Failed staging leaves the old worker serving and retains the
candidate directory for inspection; no release environment is automatically
deleted.

For status or recovery, read the runtime directory from the rendered unit's
`--runtime-dir` argument and use the launcher's interpreter:

```sh
<launcher-python> -m exomem.service_upgrade --runtime-dir <runtime-dir> --status
<launcher-python> -m exomem.service_upgrade --runtime-dir <runtime-dir> --resume
```

The private control socket is owner-only. A failed transition after the old
worker stops leaves a recovery record and the public ingress unavailable;
`--resume` rolls forward the recorded target after process-exit proof. Do not
start an older release over a possibly migrated state root. Supervisor or host
restarts interrupt public connections and require normal client reconnection.

Changing the stable supervisor, bind host, port or `service.env` is an offline
maintenance operation. Prepare the replacement package, environment file or
unit edit first. Complete at least one normal `scripts/upgrade.sh` worker
handoff before changing the launcher venv: the private status must show a ready
worker whose active interpreter differs from the launcher, with no pending
transition. If the launcher still owns the active worker, stage the same
currently serving release first with
`bash scripts/upgrade.sh --package-version VERSION` and the matching profile.
From the checkout, capture the current
MainPID and listener, stop the unit, and prove they and the entire service
cgroup are gone before touching the launcher venv or installed configuration:

```sh
set -euo pipefail
. scripts/_service-common.sh
unit="$(exomem_unit_file)"
exomem_service_is_managed "$unit"
service_id="$(exomem_service_id "$unit")"
launcher="$(exomem_service_python "$unit")"
runtime_dir="$(exomem_managed_runtime_dir "$unit" "$launcher")"
status_json="$("$launcher" -m exomem.service_upgrade --runtime-dir "$runtime_dir" --status)"
printf '%s\n' "$status_json" | "$launcher" -c '
import json
import sys

status = json.load(sys.stdin)
active = status.get("active")
if status.get("phase") != "ready" or status.get("pending") is not None or not isinstance(active, dict):
    raise SystemExit("managed service is not ready for maintenance")
if active.get("python") == sys.argv[1]:
    raise SystemExit("stage the current worker release with scripts/upgrade.sh before launcher maintenance")
' "$launcher"
port="$(exomem_service_port "$unit")"
main_pid="$(exomem_service_worker_pid "$service_id")"
listeners="$(exomem_listener_pids "$port")"
[[ "$main_pid" =~ ^[1-9][0-9]*$ && "$listeners" == "$main_pid" ]]
group="$(systemctl --user show "$service_id" --property ControlGroup --value)"
systemctl --user stop "$service_id"
exomem_assert_service_stopped "$main_pid" "$port" "$service_id"
"$launcher" - "$group" <<'PY'
import sys
from pathlib import Path

group = Path(sys.argv[1])
if not group.is_absolute() or group == Path("/") or ".." in group.parts:
    raise SystemExit("invalid service cgroup; maintenance refused")
root = Path("/sys/fs/cgroup") / str(group).lstrip("/")
if root.exists():
    for members in root.rglob("cgroup.procs"):
        if members.read_text().strip():
            raise SystemExit("service cgroup still has processes; maintenance refused")
PY
env_file="$(exomem_service_binding_path "$unit" "$launcher")"
cp -p "$env_file" "$env_file.before-maintenance"
cp -p "$unit" "$unit.before-maintenance"
```

Keep the unit stopped if any proof fails. The backups preserve `service.env`'s
owner-only permissions. Replace a reviewed `service.env` only now, using
`install -m 600 PATH_TO_REVIEWED_ENV "$env_file"`; use
`systemctl --user edit --full exomem.service` for
unit changes. For a supervisor package update, install into the stopped
launcher interpreter with `./.uvbin/uv pip install --refresh-package exomem --python "$launcher" "exomem==VERSION"`; worker releases continue to use
`scripts/upgrade.sh`. Validate the edited unit with
`systemd-analyze verify "$unit"`, then run `systemctl --user daemon-reload`
and `systemctl --user start exomem.service`. Check managed `--status`,
`/health/ready`, and that unauthenticated `/mcp` still returns 401 before
accepting the maintenance. Changing OAuth issuer or signing keys requires
clients to authorize again. If startup fails, leave the unit stopped and
inspect the journal and retained state before another change; do not silently
start an older release over migrated state.

## The standby sequence

A worker release is no longer replaced cold. The supervisor first spawns the
candidate as a **standby** beside the worker that is still serving. The standby
binds its own private socket, warms the lexical catalog, loads models when
preload is allowed, and proves the published graph snapshot read-only. It takes
no writer lease, publishes nothing, schedules no drain, media or watcher work,
and owns no descendants. The old worker serves throughout.

Only when the standby reports cutover readiness does the supervisor pause
ingress, drain, stop the old worker and prove its descendants exited, run the
offline migrator if the target declares a state migration, promote the standby
over its private control surface, and resume. The unavailable window is the
drain plus the promotion, not a cold start.

`/health/ready` reports this as a `cutover` block beside the serving `status`:

```json
{
  "status": "ready",
  "cutover": {
    "components": {"lexical": "ready", "graph_snapshot": "waiting"},
    "cutover_ready": false,
    "standby": true
  }
}
```

`embeddings` joins `components` only when the process's mode and overrides allow
a model preload (`EXOMEM_PRELOAD_MODELS=1`). A standby answers its own probe as
serving-ready while it warms; `cutover_ready` is what the supervisor polls, and
`components` is what an operator reads to see which component a candidate is
waiting on.

### Budgets

| Budget | Default | Override |
| --- | --- | --- |
| Standby warm | 300 s | `EXOMEM_STANDBY_WARM_SECONDS` in the unit's environment file |
| Cutover (pause to resume) | 40 s | supervisor `transition_timeout` |
| Drain of active finite requests | 30 s | within the cutover budget |
| Detached stream reattachment | 40 s | ingress `reattach_budget` |

A candidate that does not reach cutover readiness inside the warm budget is
stopped, the handoff record names the component it was waiting on, and the
existing worker keeps serving. The upgrade then falls back to the one-worker
sequence — reported in the handoff record, never silent. A release that predates
standby mode reports `"standby": "unsupported"` and takes the same path.

### The migration record

The offline state migrator runs only when the staged target declares a state
migration: the supervisor compares the descriptor set the candidate package
requires with the set the vault's state manifest was published with. When they
match and the manifest is complete, the step is recorded as skipped:

```json
{"handoff": {"standby": "ready",
             "migration": {"state": "skipped", "reason": "declared_none"},
             "promotion": {"snapshot": "current", "reproved": true}}}
```

`handoff.unavailable_ms` is the window nobody was served in — pause to resume —
and is the number to compare across releases. `migration.state` is `ran` with the reason (`descriptors_changed`, or the
manifest state that was not complete) when it runs. `promotion.snapshot` is
`current` when the checkpoint the standby proved is still the one on disk,
`advanced` when it moved, and `rebuild-after-promotion` when the re-proof failed
— in that last case promotion still proceeds and the coalesced rebuild path owns
the repair.

### The service environment file

A worker child is spawned with the unit's `EnvironmentFile=` re-read at spawn
time and overlaid on the inherited environment. systemd reads that file once,
when the supervisor starts, so without this an edited service environment would
reach a worker only after a full supervisor restart — which is exactly what a
seamless upgrade avoids. The supervisor's own environment is never changed.
This is how `EXOMEM_PRELOAD_MODELS=1` and `EXOMEM_STANDBY_WARM_SECONDS` reach
the next worker: edit `service.env`, then run the ordinary
`scripts/upgrade.sh`.

### Streams through promotion

A long-lived client `GET` stream detached for cutover is reattached with
backoff inside the reattachment budget while the standby is being promoted,
instead of being closed on the first non-success. A replacement that refuses the
original credential (401 or 403) is a definitive answer and still closes the
stream.

## Admission budgets

Admission during handoff allows at most 64 queued requests, 64 MiB total and
32 MiB per request. A queued request has 45 seconds from reservation, including
body intake. The worker handoff has a 40-second deadline, with up to 30 seconds
for active finite requests to drain. If draining expires, the old worker keeps
serving. Requests refused before dispatch report that they were not executed;
requests already dispatched are never replayed. Clients and intermediaries need
timeouts longer than the handoff budget to wait through a replacement.
