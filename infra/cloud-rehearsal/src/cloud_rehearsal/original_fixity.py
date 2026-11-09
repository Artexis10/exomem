"""The original-fixity mode: measure the fixity pass on a Cloud cell, then rehearse the runbook's
"Restore one original" on it (verify-stored-originals tasks 5.1 and 5.2).

After provisioning, the mode seeds stored originals and plain notes through the cell's own Exomem.
It times one table rebuild and two passes inside the runtime pod. It then takes a backup, restores
it into a scratch namespace, damages one original in place and runs the runbook's two bash blocks
as written. The receipt holds counts, sizes, timings, hashes and the synthetic fixture's invented
paths; it never holds file contents.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import shutil
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from cellctl.cell_readiness import (
    CELL_HEALTH_PORT,
    CELL_READINESS_PATH,
    ORIGINALS_MEASURED,
    originals_verdict,
)
from cellctl.manifests import namespace_name

from . import images
from .report import StepFailure
from .scenarios import restore_into_scratch
from .shell import run

if TYPE_CHECKING:
    from .scenarios import Context

DEFAULT_ORIGINALS = 1000
DEFAULT_MEAN_KIB = 512
DEFAULT_NOTES = 2000
# The cell volume is 10 GiB; the seeded bytes sit on it, on the scratch volume and in the backup.
MAX_SEED_BYTES = 2 * 1024**3
SEED_BATCH = 100  # originals per exec, so each stays far inside the 900 s command timeout
CANDIDATES = 10  # the first seeded originals, from which the rehearsal picks the one it damages
SERVER_PASS_TIMEOUT = 900
READINESS_TIMEOUT = 120  # a cached ready answer lives 30 s (server_assets.READINESS_SUCCESS_TTL_SECONDS)
RUNBOOK = images.REPO_ROOT / "docs/runbooks/cloud-operator-restore.md"
RUNBOOK_SECTION = "## Restore one original"
# The lines the runbook's two blocks print when they succeed.
HASH_MATCHES = "hash matches the binding"
ORIGINAL_RESTORED = "original restored"
BREAK_GLASS = "break-glass"  # the kubectl context name the runbook's blocks pass
# The runbook's DIGEST takes the fixity kinds; a preserved original's sidecar binds a `file`.
KIND = "file"
DEVIATIONS = (
    "the scratch restore renders cellctl's manifests as rehearsal step 11 does, not with "
    "`cloud_restore_manifests.py scratch`, because that command reads object-store keys a new cell's "
    "credentials Secret does not hold",
    "the break-glass identity is the disposable cluster's admin kubeconfig with its context renamed "
    "`break-glass`, run with the runner's kubectl",
    "the blocks run from the checkout root with `CELLCTL_PYTHON` set to this interpreter, which imports cellctl",
    "the rehearsal stops between the blocks unless the first exits 0, as the operator stops without "
    "`hash matches the binding`",
    "the rehearsal runs a fixity pass in the pod instead of waiting up to an hour for the cell's next one",
    "`$STAGE` stays in the cell until the cluster is torn down",
)

# Runs inside the cell: governed preserves of random bytes with an exponential size distribution.
SEED = """
import io, json, os, random, sys
from pathlib import Path
from exomem.governance.principal import library_scope
from exomem.preserve import MAX_UPLOAD_BYTES, preserve_stream
vault = Path(os.environ['EXOMEM_VAULT_PATH'])
start, stop, mean, category, keep = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]), sys.argv[4], int(sys.argv[5])
paths, total = [], 0
with library_scope():
    for index in range(start, stop):
        # Seeded by index, so every run draws the same sizes; index 0 is exactly the mean.
        drawn = mean if index == 0 else round(random.Random(index).expovariate(1 / mean))
        size = min(MAX_UPLOAD_BYTES, max(1, drawn))  # preserve refuses a larger upload
        result = preserve_stream(vault, scope='rehearsal', category=category, filename=f'original-{index:05d}.bin',
                                 stream=io.BytesIO(os.urandom(size)), content_type='application/octet-stream')
        paths.append(result.path)
        total += size
print(json.dumps({'first': paths[:keep], 'count': len(paths), 'bytes': total}))
"""

# Runs inside the cell: plain notes with no stored original, which the walk reads past.
NOTES = r"""
import json, os, sys
from pathlib import Path
from exomem.vault import kb_root
folder = kb_root(Path(os.environ['EXOMEM_VAULT_PATH'])) / 'Notes' / sys.argv[2]
folder.mkdir(parents=True, exist_ok=True)
for index in range(int(sys.argv[1])):
    (folder / f'note-{index:05d}.md').write_text(f'# Note {index}\n\nA plain note with no stored original.\n')
print(json.dumps({'notes': int(sys.argv[1])}))
"""

# Runs inside the cell: one table rebuild, one pass that may re-hash every original, one default pass.
MEASURE = """
import json, os, sys, time
from pathlib import Path
from exomem import original_fixity as fixity
vault = Path(os.environ['EXOMEM_VAULT_PATH'])

def timed(call):
    wall, cpu = time.perf_counter(), time.process_time()
    value = call()
    seconds = time.perf_counter() - wall
    return value, {'seconds': round(seconds, 3), 'cpu_seconds': round(time.process_time() - cpu, 3)}

def pass_record(result, timing):
    read = result.rehashed_bytes + result.changed_bytes
    unverified = fixity.status(vault).get('counts', {}).get(fixity.UNVERIFIED)
    return {**timing, 'bytes_read': read, 'budget_bytes': result.budget_bytes,
            'mib_per_second': round(read / 2**20 / timing['seconds'], 2) if timing['seconds'] else None,
            'outcomes': result.outcomes, 'unreadable': result.unreadable, 'rescheduled': result.rescheduled,
            'unverified_after': unverified}

found, walk_timing = timed(lambda: fixity.walk(vault))
full, full_timing = timed(lambda: fixity.run_pass(vault, budget=int(sys.argv[1])))
full_record = pass_record(full, full_timing)
default, default_timing = timed(lambda: fixity.run_pass(vault))
status = fixity.status(vault)
print(json.dumps({
    'settings': {'read_mib_per_second': fixity.read_rate() / 2**20, 'pass_seconds': fixity.pass_seconds(),
                 'cycle_days': fixity.cycle_seconds() / 86400, 'walk_seconds': fixity.walk_seconds(),
                 'budget_floor_bytes': fixity.BUDGET_FLOOR_BYTES},
    'table_rebuild': {'originals': found, **walk_timing},
    'full_pass_under_read_cap': full_record,
    'default_pass': pass_record(default, default_timing),
    'status': {key: status.get(key) for key in ('state', 'coverage', 'counts')},
}))
"""

# Runs inside the cell: what the binding expects and what the file holds, for each path.
FACTS = """
import hashlib, json, os, sys
from pathlib import Path
from exomem.originals import original_state
vault = Path(os.environ['EXOMEM_VAULT_PATH'])
facts = []
for relative in sys.argv[1:]:
    snapshot = original_state(vault, relative)
    found = size = None
    try:
        with open(vault / relative, 'rb') as source:
            digest = hashlib.sha256()
            for chunk in iter(lambda: source.read(1 << 20), b''):
                digest.update(chunk)
            found, size = digest.hexdigest(), source.tell()
    except FileNotFoundError:
        pass
    facts.append({'path': relative, 'state': snapshot.state, 'binding_sha256': snapshot.binary_sha256,
                  'file_sha256': found, 'size': size, 'store_copy': snapshot.reference is not None})
print(json.dumps(facts))
"""

# Runs inside the cell: invert every byte of one original in place, so its size stays and its hash changes.
DAMAGE = """
import hashlib, json, os, stat, sys
from pathlib import Path
path = Path(os.environ['EXOMEM_VAULT_PATH']) / sys.argv[1]
mode = stat.S_IMODE(path.stat().st_mode)
os.chmod(path, mode | stat.S_IWUSR)
try:
    with open(path, 'r+b') as target:
        flipped = target.read().translate(bytes(range(255, -1, -1)))
        target.seek(0)
        target.write(flipped)
        target.flush()
        os.fsync(target.fileno())
finally:
    os.chmod(path, mode)
print(json.dumps({'size': len(flipped), 'sha256': hashlib.sha256(flipped).hexdigest()}))
"""

# Runs inside the cell: what the runbook's second block left in its staging directories.
STAGED = """
import glob, hashlib, json, sys
prior = sorted(glob.glob('/data/.original-restore-*/prior/' + glob.escape(sys.argv[1])))
print(json.dumps({'prior_sha256': [hashlib.sha256(open(path, 'rb').read()).hexdigest() for path in prior],
                  'restored_left': len(glob.glob('/data/.original-restore-*/restored'))}))
"""

# Runs inside the cell: the fixity status, and whether the operator switched fixity off.
STATUS = """
import json, os
from pathlib import Path
from exomem import original_fixity as fixity
print(json.dumps({'off': fixity.disabled(), 'status': fixity.status(Path(os.environ['EXOMEM_VAULT_PATH']))}))
"""

# Runs inside the cell: one pass with the configured budget and read cap.
PASS = """
import json, os
from pathlib import Path
from exomem import original_fixity as fixity
vault = Path(os.environ['EXOMEM_VAULT_PATH'])
result = fixity.run_pass(vault)
counts = fixity.status(vault).get('counts') or {}
print(json.dumps({'outcomes': result.outcomes, 'unreadable': result.unreadable, 'rescheduled': result.rescheduled,
                  'unresolved_outcomes': sum(result.outcomes.get(state, 0) for state in fixity.UNRESOLVED),
                  'unresolved_rows': sum(counts.get(state, 0) for state in fixity.UNRESOLVED)}))
"""

# Runs inside the cell: the `originals` block of the readiness answer cellctl reads, and nothing else.
READINESS = """
import json, sys, urllib.error, urllib.request
try:
    with urllib.request.urlopen(f'http://127.0.0.1:{sys.argv[1]}{sys.argv[2]}', timeout=10) as reply:
        body = reply.read()
except urllib.error.HTTPError as refused:  # a cell that is not ready still answers with its readiness body
    body = refused.read()
print(json.dumps({'originals': json.loads(body).get('originals')}))
"""


@dataclass(frozen=True)
class FixityPlan:
    originals: int = DEFAULT_ORIGINALS
    mean_bytes: int = DEFAULT_MEAN_KIB * 1024
    notes: int = DEFAULT_NOTES


def runbook_blocks(text: str) -> list[str]:
    """The two bash blocks of the runbook's "Restore one original" section, in order.

    The runbook fences each block with plain ``` lines and nests none, so a line scan reads it
    without a Markdown parser dependency. A heading counts only outside a fence.
    """

    blocks: list[str] = []
    block: list[str] | None = None
    fenced = section = False
    for line in text.splitlines(keepends=True):
        if fenced:
            if line.strip() == "```":
                fenced = False
                if block is not None:
                    blocks.append("".join(block))
                    block = None
            elif block is not None:
                block.append(line)
        elif line.startswith("```"):
            fenced = True
            block = [] if section and line.strip() == "```bash" else None
        elif line.startswith("## "):
            section = line.strip() == RUNBOOK_SECTION
    if fenced or len(blocks) != 2:
        raise StepFailure(f"the runbook's {RUNBOOK_SECTION!r} section has {len(blocks)} closed bash blocks, not 2")
    return blocks


def _last_line(text: str) -> str:
    lines = text.strip().splitlines()
    return lines[-1][:300] if lines else ""


def _in_cell(ctx: Context, cell_id: str, script: str, *args: str, what: str) -> Any:
    """Run a script in the cell's runtime container and return the JSON on its last line."""

    pod = ctx.runtime_pod(cell_id)
    if pod is None:
        raise StepFailure(f"the runtime is absent during {what}")
    result = ctx.exec_in(namespace_name(cell_id), pod["metadata"]["name"], "python3", "-c", script, *args,
                         check=False)
    if result.returncode != 0:
        raise StepFailure(f"{what} exited {result.returncode}: {_last_line(result.stderr)}")
    return json.loads(result.stdout.strip().splitlines()[-1])


async def _await_server_pass(ctx: Context, cell_id: str) -> dict:
    """Wait until the cell's own fixity thread has finished the pass it starts with, so it cannot race the damage."""

    pod = ctx.runtime_pod(cell_id)
    if pod is None:
        raise StepFailure("the runtime is absent after the backup")
    # The runtime container, as Context.exec_in names it.
    started = next(entry["state"]["running"]["startedAt"] for entry in pod["status"]["containerStatuses"]
                   if entry["name"] == "exomem")
    started_at = dt.datetime.fromisoformat(started)
    deadline = time.monotonic() + SERVER_PASS_TIMEOUT
    while True:
        answer = _in_cell(ctx, cell_id, STATUS, what="the fixity status read")
        if answer["off"]:
            raise StepFailure("original fixity is off in the cell")
        last = answer["status"].get("last_pass_at")
        if last and dt.datetime.fromisoformat(last) > started_at:
            return answer["status"]
        if time.monotonic() > deadline:
            raise StepFailure(f"the cell's own fixity pass did not finish within {SERVER_PASS_TIMEOUT} s of its start")
        await asyncio.sleep(5)


async def _await_originals(ctx: Context, cell_id: str, unresolved: int) -> dict | None:
    """Read the cell's readiness until its `originals` block counts `unresolved`, or the wait ends."""

    expected = {"state": ORIGINALS_MEASURED, "unresolved": unresolved}
    deadline = time.monotonic() + READINESS_TIMEOUT
    while True:
        seen = _in_cell(ctx, cell_id, READINESS, str(CELL_HEALTH_PORT), CELL_READINESS_PATH,
                        what="the readiness read")["originals"]
        if seen == expected or time.monotonic() > deadline:
            return seen
        await asyncio.sleep(5)


def _verdict(originals: dict | None) -> bool | None:
    """cellctl's own reading of the block, as its alert reads it."""

    return originals_verdict(json.dumps({"originals": originals}).encode())


def _kubeconfig(ctx: Context) -> Path:
    """The disposable cluster's admin kubeconfig, its current context renamed `break-glass`."""

    document = yaml.safe_load(ctx.stack.k3s.kubeconfig.read_text())
    for entry in document["contexts"]:
        if entry["name"] == document["current-context"]:
            entry["name"] = BREAK_GLASS
    document["current-context"] = BREAK_GLASS
    path = ctx.stack.workdir / "break-glass-kubeconfig.yaml"
    path.touch(mode=0o600)
    path.write_text(yaml.safe_dump(document))
    return path


def runbook_env(variables: dict[str, str], kubeconfig: Path) -> dict[str, str]:
    """The environment an operator sets before the blocks: the runbook's variables and the reviewed cellctl Python."""

    return {**os.environ, **variables, "KUBECONFIG": str(kubeconfig), "CELLCTL_PYTHON": sys.executable}


def runbook_script(blocks: list[str]) -> str:
    """Both blocks in one bash, stopping between them unless the first exits 0, as the operator stops."""

    return blocks[0] + '[ "$?" = 0 ] || exit 3\n' + blocks[1]


def _run_runbook(ctx: Context, variables: dict[str, str]) -> dict:
    """Run the runbook's two blocks in one bash, as an operator pastes them, and report what they printed."""

    blocks = runbook_blocks(RUNBOOK.read_text())
    if shutil.which("kubectl") is None:
        raise StepFailure("the runbook's blocks need kubectl on the runner")
    env = runbook_env(variables, _kubeconfig(ctx))
    versions = run(["kubectl", "version", "--output=json"], env=env, check=False, timeout=60)
    try:
        version = json.loads(versions.stdout)
    except ValueError:
        version = {}
    script = runbook_script(blocks)
    result = run(["bash", "-c", script], cwd=images.REPO_ROOT, env=env, check=False, timeout=900)
    printed = result.stdout.splitlines()
    return {
        "kubectl_client": (version.get("clientVersion") or {}).get("gitVersion"),
        "kubernetes_server": (version.get("serverVersion") or {}).get("gitVersion"),
        "exit_code": result.returncode,
        "hash_matches_printed": HASH_MATCHES in printed,
        "original_restored_printed": ORIGINAL_RESTORED in printed,
        "stderr_last_line": _last_line(result.stderr),
    }


async def run_original_fixity(ctx: Context, plan: FixityPlan) -> dict:
    """Seed, measure, then restore one damaged original by the runbook; the stage fails on any unmet check."""

    owner = ctx.a
    if not owner.cell_id:
        raise StepFailure("the original-fixity mode requires the provisioned synthetic owner")
    cell_id = owner.cell_id
    scratch = f"exo-scratch-{cell_id}-{ctx.stack.run_id}"
    record: dict[str, Any] = {"state": "RUNNING", "phase": "seed", "cell_id": cell_id, "plan": asdict(plan),
                              "checks": {}, "runbook_deviations": list(DEVIATIONS)}
    problems: list[str] = []

    def save() -> None:
        ctx.report.stages["original_fixity"]["evidence"] = record

    def check(name: str, passed: bool, *, blocking: bool = True) -> None:
        record["checks"][name] = passed
        save()
        if not passed:
            problems.append(name)
            if blocking:
                raise StepFailure(f"the original-fixity check {name!r} failed")

    save()
    try:
        pod = ctx.runtime_pod(cell_id)
        if pod is None:
            raise StepFailure("the synthetic runtime is absent")
        # The runtime container, as Context.exec_in names it.
        container = next(c for c in pod["spec"]["containers"] if c["name"] == "exomem")
        record["cell_resources"] = container.get("resources")
        began = time.monotonic()
        _in_cell(ctx, cell_id, NOTES, str(plan.notes), f"Fixity {ctx.stack.run_id}", what="the note seed")
        candidates: list[str] = []
        seeded = seeded_bytes = 0
        for start in range(0, plan.originals, SEED_BATCH):
            stop = min(plan.originals, start + SEED_BATCH)
            batch = _in_cell(ctx, cell_id, SEED, str(start), str(stop), str(plan.mean_bytes),
                             f"fixity-{start // SEED_BATCH:03d}", str(CANDIDATES - len(candidates)),
                             what="the original seed")
            candidates += batch["first"]
            seeded += batch["count"]
            seeded_bytes += batch["bytes"]
        record["fixture"] = {"originals": seeded, "bytes": seeded_bytes, "notes": plan.notes,
                             "seed_seconds": round(time.monotonic() - began, 1)}
        check("every_original_seeded", seeded == plan.originals)

        record["phase"] = "measure"
        record["measurement"] = _in_cell(ctx, cell_id, MEASURE, str(seeded_bytes), what="the measurement")
        save()
        check("table_rebuild_found_every_original",
              record["measurement"]["table_rebuild"]["originals"] >= seeded, blocking=False)
        check("full_pass_verified_every_original",
              record["measurement"]["full_pass_under_read_cap"]["outcomes"] == {"verified": seeded}, blocking=False)

        record["phase"] = "backup"
        snapshot = await ctx.backup(cell_id)
        check("backup_snapshot_is_64_hex", len(snapshot) == 64)

        record["phase"] = "scratch_restore"
        restore_into_scratch(ctx, cell_id, snapshot, scratch, label="original fixity")
        record["scratch_namespace"] = scratch

        record["phase"] = "server_pass"
        record["status_after_restart"] = await _await_server_pass(ctx, cell_id)

        record["phase"] = "choose"
        facts = _in_cell(ctx, cell_id, FACTS, *candidates, what="the candidate facts")
        intact = [fact for fact in facts if fact["file_sha256"] and fact["file_sha256"] == fact["binding_sha256"]]
        # Fixity restores an original with a media-store copy itself; the runbook is for one without.
        without_copy = [fact for fact in intact if not fact["store_copy"]]
        check("an_intact_candidate_without_a_store_copy", bool(without_copy))
        chosen = without_copy[0]
        original, expected = chosen["path"], chosen["binding_sha256"]
        record["original"] = {"path": original, "kind": KIND, "expected_sha256": expected,
                              "size": chosen["size"], "state": chosen["state"], "store_copy": chosen["store_copy"]}

        record["phase"] = "damage"
        damaged = _in_cell(ctx, cell_id, DAMAGE, original, what="the damage")
        [after] = _in_cell(ctx, cell_id, FACTS, original, what="the damaged facts")
        record["damaged_sha256"] = damaged["sha256"]
        check("damage_kept_size_and_changed_bytes", after["size"] == chosen["size"]
              and after["file_sha256"] == damaged["sha256"] != expected)

        record["phase"] = "alert"
        record["pass_after_damage"] = _in_cell(ctx, cell_id, PASS, what="the pass after the damage")
        check("pass_counted_the_damage", record["pass_after_damage"]["unresolved_outcomes"] == 1, blocking=False)
        record["readiness_after_damage"] = await _await_originals(ctx, cell_id, 1)
        check("readiness_counted_one_unresolved", _verdict(record["readiness_after_damage"]) is True
              and record["readiness_after_damage"] == {"state": ORIGINALS_MEASURED, "unresolved": 1},
              blocking=False)

        record["phase"] = "runbook"
        record["runbook"] = _run_runbook(ctx, {"CELL_ID": cell_id, "SNAPSHOT": snapshot, "SCRATCH": scratch,
                                               "ORIGINAL": original, "KIND": KIND, "EXPECTED": expected})
        save()
        check("runbook_hash_matches_the_binding", record["runbook"]["hash_matches_printed"])
        check("runbook_original_restored", record["runbook"]["original_restored_printed"])
        check("runbook_exit_zero", record["runbook"]["exit_code"] == 0, blocking=False)

        record["phase"] = "verify"
        [restored] = _in_cell(ctx, cell_id, FACTS, original, what="the restored facts")
        check("restored_bytes_hash_to_the_binding", restored["file_sha256"] == expected)
        staged = _in_cell(ctx, cell_id, STAGED, Path(original).name, what="the staging read")
        check("damaged_bytes_kept_in_stage", staged["prior_sha256"] == [damaged["sha256"]], blocking=False)
        check("no_staged_copy_left", staged["restored_left"] == 0, blocking=False)

        record["phase"] = "pass"
        record["pass_after_restore"] = _in_cell(ctx, cell_id, PASS, what="the pass after the restore")
        check("pass_found_nothing_unresolved", record["pass_after_restore"]["unresolved_outcomes"] == 0
              and record["pass_after_restore"]["unresolved_rows"] == 0)
        record["readiness_after_restore"] = await _await_originals(ctx, cell_id, 0)
        check("readiness_counts_zero_unresolved", _verdict(record["readiness_after_restore"]) is False
              and record["readiness_after_restore"] == {"state": ORIGINALS_MEASURED, "unresolved": 0})
        if problems:
            raise StepFailure(f"the original-fixity checks {problems} failed")
        record["state"] = "PASS"
    except Exception as error:  # noqa: BLE001 - any failure leaves a PARTIAL receipt; cancellation passes through
        record["state"] = "PARTIAL"
        record["error_type"] = type(error).__name__
        # Only the rehearsal's own messages, whose tails of in-cell output come from the synthetic fixture.
        if isinstance(error, StepFailure):
            record["failure"] = str(error)[:400]
        raise StepFailure(f"original fixity stopped during {record['phase']}; inspect the sanitized receipt") from None
    finally:
        ctx.kubectl("delete", "namespace", scratch, "--ignore-not-found", "--wait=false", check=False)
        save()
    return record
