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

For a personal candidate that has not been published, the staging module also
accepts a local wheel directly. Supply the full source commit that produced the
wheel; staging copies the artifact into its owner-only release directory and
records the declared revision, artifact digest, original filename, and verified
installed interpreter/package identity in `provenance.json` beside it.

```sh
<operator-python> -m exomem.service_upgrade --runtime-dir <runtime-dir> \
  --wheel /absolute/path/to/exomem-0.80.0-py3-none-any.whl \
  --source-revision <40-hex-commit> --profile standard
```

`<operator-python>` must come from a tested checkout or newly installed
interpreter that contains this staging feature; the manager's existing launcher
interpreter stays unchanged and is used only to create the release environment.
`--wheel` and `--package-version` are mutually exclusive. A local wheel is a
candidate, not a public release; it uses the same managed handoff once staging
has verified it. `scripts/upgrade.sh` does not forward wheel sources yet.

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
`--resume` rolls forward the recorded target after process-exit proof, and
waits out the same cold-start budget for the replacement to report ready. Do
not start an older release over a possibly migrated state root. Supervisor or
host restarts interrupt public connections and require normal client
reconnection.

A handoff that is taking minutes is not the same thing as a failed one. Read
`--status` before reaching for `--resume`: a transition still in flight reports
`phase: upgrading` with a `transition` id and a `pending` record naming the
step it is on, and its outcome appears under `last_transition` when it
finishes. Resuming a transition that is still running is refused, but a repair
the replacement is doing in the background is the kind of work that outlasts
an impatient operator's first look.

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
binds its own private socket, **proves** the maintained lexical catalog current
read-only, warms the rebuildable in-memory caches, **adopts** the published graph
snapshot, builds the semantic corpus context, and loads models when preload is
allowed. It takes no writer
lease, publishes nothing, schedules no drain, media or watcher work, and owns no
descendants. In particular it never reconciles or requests repair of the lexical
catalog: that is a publication, and the worker still serving is this vault's
single repair owner. A catalog that is not current leaves `lexical` waiting until
that owner publishes one — which the warm budget covers and its expiry records.
The old worker serves throughout.

Only when the standby reports cutover readiness does the supervisor pause
ingress, drain, stop the old worker and prove its descendants exited, run the
offline migrator if the target declares a state migration, promote the standby
over its private control surface, and resume. The unavailable window is the
drain plus the promotion, not a cold start.

Two budgets divide that sequence, at the stop. Everything up to and including
the migrator runs under the cutover budget, because until the old worker is
signalled the supervisor can still abandon the upgrade and give admission back
to a worker that is serving. Once it has stopped there is no such worker, so
the wait for the replacement to report ready — the promoted standby, or a cold
one-worker start — runs under the cold-start budget instead. Ending that wait
early buys nothing: it turns "unavailable for another minute" into "unavailable
until an operator resumes", and the resume waits on the same replacement. A
candidate that *exits* before readiness, or that answers with a different
release, still fails immediately; only a live candidate on the right release is
waited out.

`/health/ready` reports this as a `cutover` block beside the serving `status`:

```json
{
  "status": "ready",
  "cutover": {
    "components": {
      "lexical": "ready",
      "graph_snapshot": "waiting",
      "semantic_corpus": "waiting"
    },
    "cutover_ready": false,
    "standby": true,
    "carried_from_standby": []
  }
}
```

Adoption is what makes the promoted worker's first governed write incremental:
it proves the inherited sidecar against disk and makes that checkpoint this
process's delta origin, instead of the whole-vault pass a replacement used to
pay for a lineage it could advance. The `cutover` block reports it as
`adoption: {"residue": N, "reason": "adopted"}`. A non-zero `residue` is an
adoption that succeeded *and* owes the drain that many pages — the deferred
writes the outgoing worker left behind. The standby only *records* that debt:
queueing the repair and withdrawing the availability marker are writes to state
the serving worker still owns, so they happen at promotion, and the handoff
record reports `residue_applied`. From then on, reads that require a current
projection refuse until that repair lands. A refused adoption names its
reason instead (for example `residue_exceeds_drain_limit`), leaves
`graph_snapshot` waiting, and the candidate is discarded on budget expiry.

`embeddings` joins `components` only when the process's mode and overrides allow
a model preload (`EXOMEM_PRELOAD_MODELS=1`). A standby answers its own probe as
serving-ready while it warms; `cutover_ready` is what the supervisor polls, and
`components` is what an operator reads to see which component a candidate is
waiting on.

`semantic_corpus` is in the set because the gate that admits governed writes
waits on it. Building it is a read — it walks and parses Markdown and caches in
memory, takes no lock and publishes nothing — so a standby may do it beside the
worker that still owns the vault. On the 0.85.0 upgrade it was not in the set:
the cutover itself took 1.6 s and the promoted worker then refused every
governed write for about thirty seconds with
`MUTATION_WARMING warming_component=semantic_corpus` while it built a corpus its
own standby could have built. A cutover that hands back a worker which refuses
writes has moved the outage, not removed it.

### What a promoted worker does not repeat

A promoted worker runs its own warm-up, and subtracts from it whatever its
standby already finished in the same process and promotion either re-verified or
cannot invalidate. `carried_from_standby` on the `cutover` block and on the
promotion record names what was subtracted, and the worker's `warm complete` log
line repeats it. The graph handoff is carried only when promotion re-proved the
snapshot as `current`; an `advanced`, `unproven` or `rebuild-after-promotion`
verdict leaves it out and the adoption runs again in full. What remains after a
carried promotion is the retrieval catalog check and any model the standby's
preload policy did not load.

A worker that starts cold — no standby, no promotion — carries nothing and runs
every warm-up step, unchanged. If a cutover is fast but the writes after it are
slow or refused, `carried_from_standby` is the first field to read: an empty
list after a promotion means the worker is paying the whole warm again.

### Budgets

| Budget | Default | Override |
| --- | --- | --- |
| Standby warm | 300 s | `EXOMEM_STANDBY_WARM_SECONDS` in the unit's environment file |
| Cutover (pause to the migrator) | 40 s | supervisor `transition_timeout` |
| Drain of active finite requests | 30 s | within the cutover budget |
| Replacement readiness after the stop | 300 s, never under 120 s | `EXOMEM_COLD_START_SECONDS` |
| Detached stream reattachment | 40 s | ingress `reattach_budget` |

The cold-start budget sizes every wait that begins after the previous worker
has stopped: the supervisor's own start, the promotion of a standby, and the
cold one-worker start — in a fresh upgrade and in `--resume` alike. It is one
window, so an operator who lengthens it for a large vault lengthens all of
them. A promoted worker that is still building a component it could not warm as
a standby — a retrieval catalog delegated to background repair, for instance —
answers `/health/ready` with `not_ready` for as long as that repair takes, and
that is what this budget has to cover.

Admission is unchanged through the longer wait: ingress stays paused, and a
request that outlasts its own 45-second queue budget is answered explicitly as
undispatched rather than held. It is never left queued without a bound.

A candidate that does not reach cutover readiness inside the warm budget is
stopped, the handoff record names the component it was waiting on, and the
existing worker keeps serving. The upgrade then falls back to the one-worker
sequence — reported in the handoff record, never silent. A release that predates
standby mode reports `"standby": "unsupported"` and takes the same path.

### Polling a transition

`upgrade` is acknowledged, not awaited: the supervisor answers immediately with
`{"ok": true, "accepted": true, "transition": "<id>"}` and runs the sequence in
the background, because a standby warm is minutes long and holding the control
connection open for it would turn any client read timeout into a false failure
while promotion proceeded regardless. Poll `--status` for the outcome: it
carries `transition` (in flight) and `last_transition` (the finished record,
including the handoff). `scripts/upgrade.sh` does this for you, with a deadline
covering the warm, the cutover and the cold-start budgets — a client that gives
up sooner would report a failure for a handoff that was still going to succeed.

### The migration record

The offline state migrator runs only when the staged target declares a state
migration: the supervisor compares the descriptor set the candidate package
requires with the set the vault's state manifest was published with. When they
match and the manifest is complete, the step is recorded as skipped:

```json
{"handoff": {"standby": "ready",
             "migration": {"state": "skipped", "reason": "declared_none"},
             "promotion": {"snapshot": "current",
                           "revalidated": true, "reproved": false}}}
```

`handoff.unavailable_ms` is the window nobody was served in — pause to resume —
and is the number to compare across releases. `handoff.ready_after_ms` is the
part of it spent waiting for the replacement, from the old worker's stop to the
replacement's readiness; when the two are close, the cutover itself was cheap
and the replacement's own warm is what the outage was.
`migration.state` is `ran` with the reason (`descriptors_changed`, or the
manifest state that was not complete) when it runs. `promotion.snapshot` is
`current` when the checkpoint the standby proved is still the one on disk,
`advanced` when it moved, and `rebuild-after-promotion` when the re-proof failed
— in that last case promotion still proceeds and the coalesced rebuild path owns
the repair.

`promotion.revalidated` says promotion re-checked the snapshot at all;
`promotion.reproved` says it re-ran the whole source proof, which happens only
when the migrator ran. Without a migration, promotion compares the checkpoint
pair instead — a real re-validation against the one writer the sequence has not
already excluded, and deliberately cheaper, because a full source proof would
add seconds to the one window this whole sequence exists to shorten.

### The service environment file

A worker child is spawned with every `EnvironmentFile=` the unit declares,
re-read at spawn time and overlaid in order on the inherited environment. systemd reads that file once,
when the supervisor starts, so without this an edited service environment would
reach a worker only after a full supervisor restart — which is exactly what a
seamless upgrade avoids. The supervisor's own environment is never changed.
This is how `EXOMEM_PRELOAD_MODELS=1` and `EXOMEM_STANDBY_WARM_SECONDS` reach
the next worker: edit `service.env`, then run the ordinary
`scripts/upgrade.sh`. A file that cannot be read is reported in the handoff
record as `environment: ["unreadable: <name>"]`, not just logged.

### Streams through promotion

A long-lived client `GET` stream detached for cutover is reattached with
backoff inside the reattachment budget while the standby is being promoted,
instead of being closed on the first non-success. A replacement that refuses the
original credential (401 or 403) is a definitive answer and still closes the
stream.

## Admission budgets

Admission during handoff allows at most 64 queued requests, 64 MiB total and
32 MiB per request. A queued request has 45 seconds from reservation, including
body intake. The cutover has a 40-second deadline, with up to 30 seconds for
active finite requests to drain. If draining expires, the old worker keeps
serving. Requests refused before dispatch report that they were not executed;
requests already dispatched are never replayed.

A queued request's 45 seconds is deliberately shorter than the replacement
readiness budget, so no client waits on a handoff without an answer: a request
that outlasts it is told explicitly that it was not dispatched and can be
retried. Clients and intermediaries need timeouts longer than the queue budget
to wait through an ordinary handoff; none of them should wait out a slow
replacement's whole warm.
