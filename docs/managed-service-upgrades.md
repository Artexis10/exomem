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
leaves it alone. Failed staging leaves the old worker serving and removes the
release directory it created, also when wheel verification fails.

Once the target is confirmed active, the operator also re-runs `install-hook`,
from the newly promoted release, for every local Claude Code profile that
already wires the retrieve nudge hook: the default `~/.claude` plus any
sibling `~/.claude-*` profile whose `settings.json` (resolved through a
symlink, if it is one) names `exomem-retrieve-nudge.sh`. A profile that never
wired the hook is left untouched. This never fails or rolls back the upgrade
itself -- a per-profile error, such as the installer's own refusal of a
group-writable config, is reported under `hook_refresh` in the upgrade result
and surfaced by `exomem doctor` (`upgrade.hook_refresh`), not raised. Set
`EXOMEM_DISABLE_UPGRADE_HOOK_REFRESH` to opt out.

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

## Rehearse a release on a copy

Rehearse each release on a copy of the live vault and state before you install
it. The rehearsal runs the same handoff on a throwaway managed service, so it
shows what the live switch will do. The checks judge what callers see, not
whether a repair runs. Install the release only when all four pass:

- `handoff.ready_after_ms` is under 120000 (two minutes).
- `/health/ready` stays ready for 45 minutes after the switch.
- Every full lexical repair in that window publishes.
- During each repair in that window, one MCP read and one MCP write succeed,
  and neither returns `MUTATION_BUSY` or `VAULT_LOCK_TIMEOUT`.

A failed check holds the install. A repair that holds `/health/ready` at
`not_ready` fails, and so does a write refused to its caller. Run the same read
and write in the live install window; there, use a write that is safe to repeat,
such as a scratch note.
`ready_after_ms` counts from the old worker's stop, so it includes the state
migrator when `handoff.migration.state` is `ran`.

The copy needs disk for the vault and the state directory, plus one release
environment. The throwaway service needs memory for two workers during the
standby warm. Set its `MemoryMax` from the serving worker's resident size.

The first start on a copy always repairs the catalog once. The freshness
checkpoint covers each page's absolute path and change time, and a copy keeps
neither. Let the current release settle on the copy first. Only the switch that
follows tells you about the candidate.

The supervisor verifies its systemd identity, so the throwaway service runs as
a transient user unit. Keep its paths short: the control sockets under the
runtime directory have a 104-byte limit.

1. Create the scratch root: `install -d -m 700 /tmp/xr /tmp/xr/{managed,home,logs,lease,cwd,state}`.
2. Create the launcher with `uv venv /tmp/xr/launcher` and the serving version's package and profile extras.
3. When no import or bulk apply is running, copy the vault to `/tmp/xr/vault`.
4. Compute the copy's state key with the key command below.
5. Create the state leaf: `install -d -m 700 /tmp/xr/state/KEY`.
6. Copy the live vault state directory into the state leaf with the state copy command below.
7. Set `vault_identity` in `/tmp/xr/state/KEY/.state-migration.json` to `KEY` with a JSON editor.
8. Write `/tmp/xr/rehearsal.env` with mode 600 from the template below.
9. Pick a free loopback port as `PORT`.
10. Start the transient unit with the `systemd-run` command below.
11. Wait until `curl http://127.0.0.1:PORT/health/ready` answers 200 and the latest repair event is `lexical_repair_finished`.
12. Run `/tmp/xr/launcher/bin/python -m exomem.service_upgrade --runtime-dir /tmp/xr/managed --status` and confirm `phase: ready` with no pending transition.
13. Note the time, then switch with the `service_upgrade` command below.
14. Read `last_transition.handoff` in the printed result.
15. Poll `/health/ready` and read the log events below for 45 minutes after the noted time.
16. While a repair runs, send one MCP read and one MCP write to `http://127.0.0.1:PORT/mcp`.
17. Stop the unit: `systemctl --user stop exomem-rehearsal.service`.
18. To rehearse the next release from this one, keep `/tmp/xr`. Otherwise remove it: `rm -rf /tmp/xr`.

Expect all four checks to pass in the 45 minutes after the noted time.

```sh
/tmp/xr/launcher/bin/python -c 'from pathlib import Path; from exomem.state_paths import vault_state_key; print(vault_state_key(Path("/tmp/xr/vault")))'
```

The service writes its state while you copy it. The state copy command copies
each SQLite database through SQLite's backup API, so each copy is consistent. It
tells a database by its file header, skips SQLite's side files and rebuild
temporaries, and skips files that disappear during the copy:

```sh
/tmp/xr/launcher/bin/python -I - LIVE_STATE_DIR /tmp/xr/state/KEY <<'PY'
import contextlib, shutil, sqlite3, sys
from pathlib import Path
src, dst = Path(sys.argv[1]), Path(sys.argv[2])
for path in sorted(src.rglob("*")):
    target = dst / path.relative_to(src)
    try:
        if path.is_symlink():
            continue
        if path.is_dir():
            target.mkdir(mode=0o700, parents=True, exist_ok=True)
            continue
        if path.name.endswith(("-wal", "-shm", "-journal")) or ".rebuild-" in path.name:
            continue
        with path.open("rb") as handle:
            database = handle.read(16) == b"SQLite format 3\0"
        if database:
            live = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=120)
            copy = sqlite3.connect(target)
            with contextlib.closing(live), contextlib.closing(copy):
                live.backup(copy)
            continue
        shutil.copy2(path, target)
    except (FileNotFoundError, sqlite3.OperationalError):
        if path.exists():
            raise  # a locked or unreadable database must stop the copy, not shorten it
        target.unlink(missing_ok=True)
PY
```

Write the environment file from this template, not from a copy of the live
file. It gives the copy its own paths and leaves out every variable that reaches
a live or external system. Replace only the marked live values, so the timings
match, and leave out a marked line that the live file does not set. Use the live
model cache path for `HF_HOME`, or for `HF_HUB_CACHE` when the live file sets
that; the hub stays offline:

```sh
EXOMEM_VAULT_PATH=/tmp/xr/vault
EXOMEM_STATE_ROOT=/tmp/xr/state
EXOMEM_LOG_DIR=/tmp/xr/logs
EXOMEM_WRITER_LEASE_STATE_DIR=/tmp/xr/lease
HOME=/tmp/xr/home
XDG_STATE_HOME=/tmp/xr/home/.local/state
XDG_CONFIG_HOME=/tmp/xr/home/.config
XDG_DATA_HOME=/tmp/xr/home/.local/share
EXOMEM_REST_API_KEY=NEW_RANDOM_VALUE
EXOMEM_DISABLE_MEDIA_EXTRACTION=1
EXOMEM_ASR_PREWARM=0
EXOMEM_DISABLE_CLIP=1
HF_HUB_OFFLINE=1
HF_HOME=LIVE_VALUE
EXOMEM_PRELOAD_MODELS=LIVE_VALUE
EXOMEM_STANDBY_WARM_SECONDS=LIVE_VALUE
EXOMEM_COLD_START_SECONDS=LIVE_VALUE
```

The worker also inherits the user manager's environment. Run `systemctl --user
show-environment` first, and add `-p UnsetEnvironment=NAME` to the unit for each
variable there that reaches a live or external system.

```sh
systemd-run --user --collect --unit=exomem-rehearsal.service -p Type=simple -p MemoryMax=MEMORY_LIMIT -p KillMode=control-group -p SendSIGKILL=yes -p TimeoutStopSec=30 -p EnvironmentFile=/tmp/xr/rehearsal.env -p WorkingDirectory=/tmp/xr/cwd /tmp/xr/launcher/bin/python -m exomem.service_manager serve --runtime-dir /tmp/xr/managed --worker-python /tmp/xr/launcher/bin/python --host 127.0.0.1 --port PORT --unit-name exomem-rehearsal.service
```

```sh
HOME=/tmp/xr/home UV_CACHE_DIR=LIVE_UV_CACHE EXOMEM_DISABLE_UPGRADE_HOOK_REFRESH=1 /tmp/xr/launcher/bin/python -m exomem.service_upgrade --runtime-dir /tmp/xr/managed --package-version VERSION --profile standard
```

`UV_CACHE_DIR` keeps staging on the existing package cache instead of the empty
home. For a candidate that is not published, pass `--wheel` and
`--source-revision` in place of `--package-version`, as for a live upgrade. The
candidate stages under `/tmp/xr/releases`, never under the live service root.

The log is JSON lines. Read these events and messages after the noted time:

- A `lexical_repair_started` event with `fields.kind` `full` starts a full
  repair. Its `lexical_repair_finished` event must report `result` `published`.
  `targeted` and `catch_up` are the usual delta repairs.
- `standby built a detached lexical catalogue` and `adopted retrieval catalog
  healed=` show that promotion adopted the standby's catalog.
- `managed retrieval catalog delegated to background repair` or `maintained
  retrieval catalog warm-up failed` explain a slow or failed adoption.

The rehearsal runs without other writers. During a live full repair, a bulk
writer outside the service makes every publication of that repair abort. Pause
bulk writers for the live install window.

The rehearsal does not cover a collection store schema change or the move of
file collections into the store. Both need the writer lease coordinator, and
the copy runs without one so that it cannot reach the live coordinator.

## Release retention

After the target is confirmed active, the operator removes the release
directories that the service no longer needs. It keeps the serving release, the
release that the promotion replaced, and every release that `active.json` or
`transition.json` in the runtime directory records. The launcher venv lives
outside `releases/` and is never removed. The operator removes only directories
that the service user owns, that are not symlinks, and whose names are 32
lowercase hexadecimal characters. It keeps every other entry. An unreadable or
malformed manager record stops retention before it removes anything. A removal
failure or an interrupt never fails the operator's result. An interrupted
`scripts/upgrade.sh` stops itself, but the promoted release keeps serving: check
`--status`, then run `scripts/upgrade.sh` again to finish the CLI sync.

The manager's records are the only proof of use. A process outside those
records is not protected, and a later upgrade can remove the release it runs.
Never run a canary, a hand-pinned unit or a script from a release interpreter.
Give each such process its own independent interpreter.

`scripts/upgrade.sh` prints one summary line from the `release_retention`
report in the upgrade result:

```text
Release retention: removed 3 freed_bytes 2147483648 failures 0 skipped no
```

`removed` counts the deleted directories, and `freed_bytes` is the allocated
space they released. `at_least` before that number means that a failed or
interrupted removal freed space it could not count. `failures` counts the
directories it could not remove; the next upgrade tries them again. `skipped`
names why retention stopped early, for example `interrupted` or an unreadable
record.

`scripts/upgrade.sh` runs the `exomem.service_upgrade` module installed in the
launcher venv. Retention therefore reaches a host only after an offline
launcher upgrade, described above. Until then, the summary line reads
`Release retention: this operator predates release retention`. To remove unused
releases before the launcher upgrade, run the operator from a newer interpreter.
It stages and promotes a worker release like `scripts/upgrade.sh`, but it skips
the CLI sync and the managed-install manifest helpers:

1. Install this release in an interpreter outside the service, such as a tested checkout's venv.
2. Read the runtime directory from the rendered unit's `--runtime-dir` argument.
3. Read the profile from `service_profile` in `~/.config/exomem/managed-install.json`.
4. From the checkout root, run `<operator-python> -m exomem.service_upgrade --runtime-dir <runtime-dir> --profile <profile>`.
5. Read the printed JSON result: it contains a `release_retention` report.

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

The exception is a catalog the candidate can never prove: one whose schema or
semantic identity is an earlier release's (a release that bumps the lexical
catalog schema, for instance). The serving worker keeps that catalog current for
its own code, so waiting would only burn the warm budget. The standby instead
builds its own release's catalog into a lexical rebuild temp beside the live one,
holding the temp's advisory lock for its whole life. The build takes no
publication barrier and changes no byte the serving worker owns; `lexical` is
ready once it exists, and a stopped standby cancels and removes it. At promotion,
once it owns the vault, the standby adopts that temp in place of the live catalog
and records `lexical_catalogue: adopted` (or `rebuild-after-promotion` when it
could not, leaving the ordinary repair to rebuild). The promoted worker's warm
then re-parses exactly the pages whose file signature changed since the build and
admits retrieval, without a whole-catalog rebuild. Adoption replaces a private
state file, so promotion also rebuilds the private-identity inventory once, in
the background, off the request thread. A catalog at the current schema that
merely lags is still the serving worker's to repair, as above.

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
preload policy did not load. When promotion adopted a catalog the standby built
detached, `carried_from_standby` names `catalogue_handoff` and not `lexical`: the
promoted warm re-parses the pages that changed since that build in place of the
whole-catalog repair, then warms the lexical caches the standby deliberately
did not build over the old catalog.

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

The standby warm budget (300 s unless the unit sets it) is the ceiling on how
large a vault this handoff covers: the detached build measured 54-60 s at
4.6k pages, so a vault several times larger needs a longer
`EXOMEM_STANDBY_WARM_SECONDS` before a schema-bumping upgrade.

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
part of it spent waiting for the replacement. It is measured from the old
worker's stop, so it includes the offline migrator's own run when a migration
ran; `migration.state` says whether it did. When it is close to
`unavailable_ms`, the cutover itself was cheap and the replacement's own warm
is what the outage was.

Both fields appear on a failed handoff too, and there `handoff.replacement_waiting`
carries what the replacement last reported waiting on — a readiness reason such
as `retrieval_unavailable`, with the lexical repair phase when there is one, or
the cutover component it was still warming. Together they separate a
replacement that burned the whole window on a background repair from one that
died two seconds after the stop, which need different responses. Both are
recorded from the readiness contract's own fixed vocabulary and are
observations only: no budget, wait or outcome depends on them. A failure before
the old worker stopped carries neither, because there was no replacement to
measure.

`replacement_waiting` is a separate field from `handoff.waiting`, which keeps
its own meaning: the component a *standby* was warming when it was discarded.
An upgrade can record both — a candidate discarded on its warm budget, then a
one-worker replacement that never reported ready — and that combination is
precisely the one worth reading closely, so neither value overwrites the other.

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

### A collection store schema change

A release that changes only the collection store's schema records `migration`
as `skipped` with the reason `declared_none`, because the store schema is not a
state migration. The new worker migrates the store itself once it acquires the
writer lease. Until then, the old worker keeps the lease and serves.

A release from before the schema change cannot open the migrated store. When
the vault's marker routes collections to the store, or the store records a
pending intent, the supervisor compares the
newest store schema that an upgrade target reads with the schema of the live
store. It refuses a target that cannot read the live store before the serving
worker stops, so that worker keeps serving. Releases 0.114.0 through 0.118.x read
only up to schema 10, so the supervisor refuses them on a store at schema 11.
0.119.0 and later read schema 11 and stay valid targets.

The supervisor runs this check, so the supervisor's release decides it. A
supervisor started on 0.120.0 or earlier does not have the check. After a
seamless upgrade replaces its worker, it still accepts 0.114.0 through 0.118.x
until the service unit restarts. After an export to files, no collection routes
to the store, so the check refuses nothing and an earlier release is the way
back.

Started by hand on a migrated store, a release from 0.114.0 through 0.118.x
cannot open it. On a vault with a collection store fence, every write on that
host then refuses with `WRITER_LEASE_REQUIRED`. Roll forward to recover.

0.119.0 changed the store schema, and 0.120.0 is the first release that migrates
a store from an earlier release. Back up the store with the earlier release
before the upgrade, as "A release that changes the collection store's schema" in
`docs/deployment.md` describes.

### Moving file collections into the store at a handoff

From the release that enables `collections-migrate-v1`, a seamless upgrade can
move the vault's file Records and Planning collections into the collection
store. A target that keeps that release fence off declares no such migration,
and its upgrade runs as the sections above describe.

The supervisor decides whether to ask for this step, so the launcher's release
decides it. A seamless upgrade replaces only the worker. Upgrade the launcher
offline to a release with this step before you rely on it.

The step has four parts:

- The standby pre-imports every file collection into private staging under the
  vault's state directory, as the last part of its warm. It takes no lease and
  changes no vault file. It does this only when the supervisor tells it that
  this handoff can run the step: the target declares the migration and the
  operator credential file is usable. Otherwise it removes the staging that an
  earlier warm left. Its cutover component, `collections_migration`, is ready
  once the pre-import ran, however it ended. A pre-import that fails or finds
  nothing to move leaves no staging.
- After the old worker stops, the supervisor runs the migrator only if the
  target declares the migration, the operator credential file is usable, and
  the vault holds the staging or a published store that the migration has not
  finished.
- The migrator compares every collection with what the standby imported. When
  nothing changed, it keeps the staging. When a collection changed, it imports
  all of them again inside its budget.
- The migrator then publishes the store, cuts the coordinator's store fence,
  records the compatibility descriptor, publishes the replica and installs the
  marker.

The supervisor waits 15 s for the migrator. The migrator abandons a migration
that it has not sealed 9 s after the supervisor started it. The other 6 s are
for the steps after the seal, which always finish once they start. They took
about 3 s on a test vault with about 3,900 items, and they grow with the store.

#### The operator credential file

The migrator needs the coordinator's operator credential to cut the store
fence. The supervisor reads it from one file, and only when it starts the
migrator. It gives the credential to the migrator's environment alone, never
to the serving worker or the standby, and never on a command line.

1. Find the directory that holds the unit's `service.env`, usually `~/.config/exomem`.
2. Create the file `operator.env` in that directory.
3. Write one line in it: `EXOMEM_LEASE_COORDINATOR_OPERATOR_TOKEN=<operator token>`.
4. Run `chmod 600 operator.env`.
5. Make sure that the service user owns the file and that the file is not a symlink.

Do not add `operator.env` to the unit as an `EnvironmentFile=`, and do not put
the operator credential in `service.env`. Either way, every worker would get it.

The supervisor reads the file at each handoff, so a new or changed file needs no
restart. A missing file, an unreadable file, a file that is not a regular file,
or a file that another user can read or write means no migration. The release
still completes, in file mode.

#### What the handoff record shows

`handoff.migration.collections` reports the step. It is absent when the target
declares no collections migration.

- `{"state": "skipped", "reason": "nothing staged"}`: the standby prepared
  nothing, for example because every collection is already in the store.
- `{"state": "skipped", "reason": "operator credential file missing"}`: the
  file is absent. The other reasons are `unreadable`, `is not a regular file`,
  `is not private to its owner` and `holds no operator credential`.
- `{"state": "skipped", "reason": "target migration probe failed"}`: the
  target release could not report whether it has the migration. Check that
  release before you upgrade to it again.
- `{"state": "ran", "outcome": "migrated"}`: the vault now routes every
  migrated collection to the store.
- `{"state": "ran", "outcome": "abandoned"}`: the migrator published nothing.
- `{"state": "ran", "outcome": "blocked"}`: a collection failed its proof, and
  the migrator published nothing.
- `{"state": "ran", "outcome": "pending"}`: the migrator published the store
  but did not finish, and the supervisor's rerun did not finish it either. The
  service runs, and no write is lost. See below.
- `{"state": "ran", "outcome": "unsafe"}`: the migrator stopped while the live
  store was set aside, or left a store or marker that cannot be read. The
  upgrade fails. See below.
- `{"state": "ran", "outcome": "timed_out"}`: the supervisor stopped the
  migrator at the end of its 15 s in a handoff that also migrated state, which
  the stop may have cut. The upgrade fails. See below.

An outcome can carry a `code`, which names the refusal or error that the
migrator ran into. `"code": "timed_out"` means that the supervisor stopped the
migrator at the end of its 15 s, and the outcome is what the stopped migrator
left: `abandoned`, `pending`, `migrated` or `unsafe`. After a rerun, `first_attempt` holds the outcome and code of
the attempt before it. The migrator's full report goes to the service journal. For
an abandoned migration, `abandoned_because` is `deadline`, `live_store_changed`
or `refused`. For a blocked one, the report names the collection and the failed
check.

#### An abandoned migration

An abandoned migration publishes nothing. The new release serves the vault in
file mode, and the next handoff tries the migration again. A vault whose store
changes during every handoff, or whose collections take longer than the
budget, migrates offline instead:

1. Stop the Exomem service.
2. Run `(set -a; . ~/.config/exomem/operator.env; exomem collections migrate --vault <vault> --apply)`.
3. Start the Exomem service.

Step 2 reads the operator credential from the protected file in a subshell, so
the credential never enters your shell history or your login shell. Use the
directory that holds your `operator.env` if it is not `~/.config/exomem`. The apply reports
`marker_admitted` when the migration is finished. For a blocked collection,
fix its files as the report says, then repeat the steps.

#### A pending migration

When the migrator publishes the store and then fails before it installs the
marker, the supervisor runs it once more before it starts the new release. The
migrator that failed keeps its lease for the lease time-to-live,
`EXOMEM_WRITER_LEASE_TTL`, 30 s by default, so the supervisor waits that long
first. The rerun adds the time-to-live and up to 15 s to the handoff. When it
finishes the migration, the outcome is `migrated` and the new release serves
the store. The supervisor tries only once, because each attempt costs a
time-to-live of downtime, and a fault that persists does not clear on a second
try.

A `pending` outcome means that the rerun did not finish the migration either.
The supervisor starts the new release anyway, and the service answers reads.
On a first migration, what happens to writes depends on how far the migrator
got:

- When the migrator had cut the coordinator's store fence, the fence refuses
  every write until the migration finishes.
- When it failed before the cut, the fence is not enrolled. The vault stays in
  file mode, and writes go to the files, which are still canonical.

In both cases no write is lost. On a vault whose store was already live, the
next migration restores or supersedes the published store as the offline apply
does.

The next handoff finishes the migration. To finish it sooner, fix the cause
that `code` names, then do the offline steps under "An abandoned migration"
above. Either way, the migrator first compares every collection with the files
that it imported. When a file changed meanwhile, for example through a sync
client, it imports the collections again before it installs the marker. A
migration that runs within the time-to-live after a failed one is refused with
`COLLECTION_STORE_SERVICE_ACTIVE` and stays `pending`.

#### A migrator that timed out or left the store unsafe

For the outcomes `timed_out` and `unsafe`, the supervisor reports
`recovery-required`. A stopped migrator that left a published store gets the
same rerun and the same `pending` as one that failed.

1. For `timed_out`, wait for the lease time-to-live, 30 s by default.
2. For `unsafe` with no `collections.sqlite` in the vault's state directory,
   rename the most recently modified `collections.sqlite.superseded-<suffix>`
   there back to `collections.sqlite`.
3. Run the private `--resume` command above.

The resume runs the migrator again. The result is `migrated`, or `pending` as
described above.

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

## When the service starts

The supervisor opens the public listener, the local listener and the control
socket before it starts the first worker. Until that worker reports ready, the
phase is `starting`, and every call gets an immediate HTTP 503 with the body
`Request not dispatched`. No call executes and none waits in the queue, because
a cold warm, such as a full catalogue rebuild, can outlast any queue budget.
When the first worker reports ready, ingress serves calls and the phase becomes
`ready`. A first worker that fails to start still stops the supervisor with a
failed exit status.

## When the worker crashes

A worker that exits outside a transition no longer stops the supervisor. The
public listener, the local listener and the control socket stay open, and the
supervisor replaces the worker in place:

1. The supervisor sets the phase to `restarting` and pauses ingress. New calls
   queue under the admission budgets below.
2. The supervisor proves that the exited worker and every descendant it left
   have exited. A child that called `setsid()` is included.
3. The supervisor starts the active release under the cold-start window.
4. When the replacement reports ready, ingress resumes and the phase returns to
   `ready`.

A call that the dead worker was answering gets the retryable error described
in the managed-service spec. A call that arrives during the restart is held for
up to the 45-second queue budget. If the replacement reports ready inside that
budget, the replacement serves the call. Otherwise the call is answered as not
dispatched and can be retried. A production worker takes about 40 to 80 seconds
to warm, so a call that arrives early in the restart usually gets the
not-dispatched answer.

The supervisor makes one attempt for each exit, and restarts a serving worker
in place at most 3 times in any 10 minutes. The supervisor stops with a failed
exit status in these cases:

- A fourth exit falls inside the window.
- A descendant of the exited worker is still alive after 10 seconds.
- The replacement exits before it reports ready, or does not report ready
  inside the cold-start window.
- The restart fails with an unexpected error.

systemd then cleans the unit's cgroup and applies `Restart=`, as it did for
every crash before in-place restarts. `--status` reports the restarts inside
the window, the limit, the window and the last exit status under
`worker_restarts`. The journal records each restart with the worker's exit
status. A negative status is the signal that killed the worker.

The supervisor runs from the launcher venv, so a worker handoff does not
change this behaviour. It takes effect after the offline maintenance that
changes the stable supervisor, described above, which restarts the unit once.

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

## Independent read-only recall monitoring

The installed `exomem.recall_canary` module runs public MCP cases without a source checkout.
Its `--check availability` mode checks one response per case by default.
Each configured tool needs a successful positive case with its declared references and material.
An expected abstention checks only its declared negative case.
The report retains latency results, even when the availability check passes with insufficient latency samples.

An `expected` object uses either the existing indexed fields or `outcome` plus `response_schema`, never both.
The schema format uses JSON Schema Draft 2020-12 against the complete parsed response.
Use one `contains` witness to require source identity and nonblank material from the same result or parent.
Use `prefixItems` with `minItems` when the source contract requires rank.
Local schema references work; the observer never retrieves external schema resources.
An invalid schema prevents connection. A response mismatch fails the case with `unexpected-response`.
An evaluation error reports `invalid-expectation`, fails the check and leaves availability incomplete; it does not establish a service outage.

An independent author derives each key from source truth and checks wrong identity, missing identity, blank material and material under another parent.
Schema syntax validation cannot prove those obligations.
Keep frozen keys and earlier failures unchanged; replacement keys start a new declared series without exposing unused cases.

The default `--check acceptance` mode retains the complete warm programme contract.
It requires its sample count, warm proof, known contention and verified deployment identity.
The observer package identity describes the caller, while the deployment identity comes from the operator.
Neither identity proves the other.

The `exomem.canary_systemd` adapter installs only `exomem-recall-canary.service` and `exomem-recall-canary.timer`.
The oneshot has a finite timeout, and systemd prevents concurrent executions of that service.
It has no runtime dependency, restart policy or repair action.
An observer update requires an explicit install; worker upgrades never change its interpreter.

1. Choose an independent interpreter with a noneditable observer wheel, so its version stays pinned through worker upgrades.
2. Prepare owner-only environment and case files, so credentials and source truth stay outside repositories.
3. Run the adapter's `install` command with its configuration options; the timer stays disabled.
4. Run the adapter's `enable` command; systemd loads the named credential before it enables the timer.
5. Read the adapter's `status` command or doctor's `recall.canary` check; the latest evidence appears with its state.

The install options are `--observer-python`, `--environment-file`, `--mcp-url`, `--cases`, `--token-env` and `--report-file`.
Paths must be absolute; the report directory must be owner-only.
The adapter uses systemd's native loader for the existing environment file and never copies the credential.
A missing credential leaves the timer unconfigured.
An unavailable target or authentication failure becomes a failed observation while the timer stays scheduled.

`status` reads the unit, execution properties and latest atomic report without probing the target.
It distinguishes absent, disabled, unconfigured, failed, stale and healthy availability evidence.
An old successful report cannot hide a later failed execution.
Changing the endpoint or case truth invalidates the old report.
The adapter's `uninstall` command removes only these units and retains the private configuration and report.

Failures reach the failed unit, local journal and doctor.
They do not send a push notification; delivery beyond those local surfaces remains open.
