#!/usr/bin/env python3
"""Measure the Exomem Cloud cell image under runc and under Kata Containers.

Diagnostic harness for the `diag/kata-overhead` branch; it ships nothing. It
drives the published Cloud image with Docker the way the cell runs in the
cluster (read-only root, `/tmp` tmpfs, UID 10001, no capabilities, 2 CPUs,
3 GiB), over the cell's real HTTP MCP endpoint with the cell bearer. Each
session runs both arms in turn on the same host, from a snapshot of one seeded
volume, and every case is recorded per call into `results.jsonl`.

Workloads: (a) search, recall and page reads, (b) governed writes and edits,
(c) a folder import, (d) media extraction of a PDF and images, (e) startup.
A failed workload is recorded and the run goes on with the next one.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import secrets
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixtures  # noqa: E402
import syscalls  # noqa: E402

CELL_ID = "abcdefghijklmnop"
CELL_PORT = 8765
PROTOCOL = "2025-11-25"
IMPORT_NAME = "Project Archive"
RUNTIMES = ("runc", "kata")
# Kata with /data as a loop-device ext4 volume handed to the guest as a block device (a directly assigned
# volume), instead of the default virtio-fs share. It runs on the Docker runtime named "kata".
BLOCK_ARM = "kata-blk"
KATA_RUNTIME_BIN = "/opt/kata/bin/kata-runtime"
MEDIA_DIR = "Knowledge Base/Sources/Media"
# Both arms run the cell with /data on a real Docker volume. The wrapper only creates the scratch
# directories the cell's TMPDIR and caches name, then execs the cell command unchanged.
CELL_COMMAND = (
    'ulimit -c 0; mkdir -p /data/scratch/tmp /data/scratch/cache /data/scratch/export && exec "$@"'
)
# Closed tool-name sets: the grouped execution name stays callable, the public name is listed.
TOOLS = {
    "search": ("ask_memory", "search_memory"),
    "recall": ("activate_context", "recall_context"),
    "write": ("remember", "create_note"),
    "edit": ("edit_memory", "edit"),
    "read": ("read_memory",),
    "media": ("process_media",),
    "import": ("adoption_studio", "import_memory"),
}


class HarnessError(RuntimeError):
    pass


def docker_runtime(arm: str) -> str:
    return "kata" if arm == BLOCK_ARM else arm


def now_ms() -> float:
    return time.perf_counter() * 1000.0


def sh(argv: list[str], *, check: bool = True, timeout: float | None = 900, input_text: str | None = None):
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, input=input_text, check=False)
    except subprocess.TimeoutExpired as expired:
        if check:
            raise HarnessError(f"{' '.join(argv[:6])} timed out after {timeout} s") from expired
        return subprocess.CompletedProcess(argv, 124, "", f"timed out after {timeout} s")
    if check and result.returncode != 0:
        raise HarnessError(f"{' '.join(argv[:6])} failed ({result.returncode}): {result.stdout[-1500:]}{result.stderr[-1500:]}")
    return result


# ------------------------------------------------------------------ MCP over HTTP

def _json_body(response: httpx.Response) -> dict:
    if "text/event-stream" in response.headers.get("content-type", ""):
        for line in response.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[len("data:"):].strip())
        raise HarnessError(f"no SSE data line: {response.text[:200]!r}")
    return response.json()


@dataclass
class Call:
    ms: float
    ok: bool
    structured: dict = field(default_factory=dict)
    error: str = ""
    code: str = ""

    @property
    def unknown_tool(self) -> bool:
        text = self.error.lower()
        return "unknown tool" in text or "no such tool" in text or "tool not found" in text


class Mcp:
    def __init__(self, base_url: str, token: str):
        self.client = httpx.Client(base_url=base_url, timeout=httpx.Timeout(300.0, connect=5.0), trust_env=False)
        self.headers = {
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
            "authorization": f"Bearer {token}",
        }
        self.next_id = 1
        self.names: dict[str, str] = {}

    def close(self) -> None:
        self.client.close()

    def initialize(self) -> None:
        r = self.client.post("/mcp", headers=self.headers, json={
            "jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {"protocolVersion": PROTOCOL, "capabilities": {},
                       "clientInfo": {"name": "kata-overhead", "version": "1"}}})
        if r.status_code != 200:
            raise HarnessError(f"initialize answered {r.status_code}: {r.text[:300]}")

    def list_tools(self) -> list[dict]:
        r = self.client.post("/mcp", headers={**self.headers, "mcp-protocol-version": PROTOCOL},
                             json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        return _json_body(r).get("result", {}).get("tools", [])

    def raw(self, name: str, arguments: dict) -> Call:
        self.next_id += 1
        payload = {"jsonrpc": "2.0", "id": self.next_id, "method": "tools/call",
                   "params": {"name": name, "arguments": arguments}}
        start = now_ms()
        try:
            r = self.client.post("/mcp", headers={**self.headers, "mcp-protocol-version": PROTOCOL}, json=payload)
        except httpx.HTTPError as error:
            return Call(now_ms() - start, False, error=f"transport {type(error).__name__}: {error}")
        ms = now_ms() - start
        if r.status_code != 200:
            return Call(ms, False, error=f"http {r.status_code}: {r.text[:300]}")
        try:
            body = _json_body(r)
        except Exception as error:  # noqa: BLE001 - a diagnostic harness records, never raises, per call
            return Call(ms, False, error=f"unparsable {type(error).__name__}: {r.text[:200]}")
        if "error" in body:
            return Call(ms, False, error=f"rpc {json.dumps(body['error'])[:300]}")
        result = body.get("result") or {}
        structured = result.get("structuredContent") or {}
        if result.get("isError"):
            text = " ".join(str(c.get("text", "")) for c in result.get("content", []) if isinstance(c, dict))
            return Call(ms, False, structured, error=f"tool {text[:400]}")
        error = structured.get("error") if isinstance(structured, dict) else None
        if isinstance(structured.get("result"), dict) and structured["result"].get("error"):
            error = structured["result"]["error"]
        if error:
            code = error.get("code", "") if isinstance(error, dict) else str(error)
            return Call(ms, False, structured, error=f"refused {json.dumps(error)[:300]}", code=code)
        return Call(ms, True, structured)

    def call(self, op: str, arguments: dict) -> Call:
        """Call a logical operation by the first spelling the cell accepts."""
        if op in self.names:
            return self.raw(self.names[op], arguments)
        last = Call(0.0, False, error=f"no spelling of {op}")
        for name in TOOLS[op]:
            result = self.raw(name, arguments)
            if not result.unknown_tool:
                self.names[op] = name
                return result
            last = result
        return last


def hits_of(structured: dict) -> list:
    inner = structured.get("result", structured)
    if isinstance(inner, list):
        return inner
    if isinstance(inner, dict):
        return inner.get("hits") or inner.get("results") or []
    return []


def warming_components(structured: dict) -> list[str]:
    inner = structured.get("result", structured)
    if isinstance(inner, dict):
        return list((inner.get("warming") or {}).get("components") or [])
    return []


def find_path(value, suffix: str = ".md") -> str | None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "path" and isinstance(item, str) and item.endswith(suffix):
                return item
        for item in value.values():
            found = find_path(item, suffix)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = find_path(item, suffix)
            if found:
                return found
    return None


# ------------------------------------------------------------------ the lab

IMPORT_SCRIPT = r'''
import hashlib, io, json, os, subprocess, sys, tarfile, time, uuid
from pathlib import Path
from exomem import cloud_import_job
clock = time.perf_counter
t0 = clock()
cell = os.environ["EXOMEM_CLOUD_CELL_ID"]
data, vault, source, name = Path("/data"), Path("/data/vault"), Path("/input"), sys.argv[1]
archive, count, total = io.BytesIO(), 0, 0
with tarfile.open(fileobj=archive, mode="w") as tar:
    for path in sorted(source.rglob("*")):
        if path.is_file():
            blob = path.read_bytes()
            member = tarfile.TarInfo(name + "/" + path.relative_to(source).as_posix())
            member.size = len(blob)
            tar.addfile(member, io.BytesIO(blob))
            count += 1
            total += len(blob)
t_tar = clock()
import_id = str(uuid.uuid4())
recipient = cloud_import_job.prepare(data, import_id, cell)
t_prepare = clock()
cipher = subprocess.run(["age", "-r", recipient], input=archive.getvalue(), capture_output=True, check=True).stdout
t_encrypt = clock()
request = cloud_import_job.CloudImportRequest(
    import_id=import_id, cell_id=cell, kind="adopt", declared_files=count, declared_bytes=total,
    ciphertext_bytes=len(cipher), ciphertext_sha256=hashlib.sha256(cipher).hexdigest())
result = cloud_import_job.run_import(request, vault, lambda: io.BytesIO(cipher))
t_run = clock()
print(json.dumps({"import_id": import_id, "files": count, "bytes": total, "result": result,
    "read_tar_ms": (t_tar - t0) * 1000, "prepare_ms": (t_prepare - t_tar) * 1000,
    "encrypt_ms": (t_encrypt - t_prepare) * 1000, "run_import_ms": (t_run - t_encrypt) * 1000}))
'''

PROBE_SCRIPT = r'''
import ctypes, errno, json, os, sys
from pathlib import Path
libc = ctypes.CDLL(None, use_errno=True)


class How(ctypes.Structure):
    _fields_ = [("flags", ctypes.c_uint64), ("mode", ctypes.c_uint64), ("resolve", ctypes.c_uint64)]


def failure():
    code = ctypes.get_errno()
    return "errno %d %s" % (code, errno.errorcode.get(code, "?"))


def openat2(fd, resolve):
    how = How(os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC, 0, resolve)
    result = libc.syscall(ctypes.c_long(437), ctypes.c_int(fd), ctypes.c_char_p(b"."), ctypes.byref(how),
                          ctypes.c_size_t(ctypes.sizeof(how)))
    if result < 0:
        return failure()
    os.close(result)
    return "ok"


def renameat2(fd):
    for name in ("probe-a", "probe-b"):
        try:
            os.unlink(name, dir_fd=fd)
        except OSError:
            pass
    os.close(os.open("probe-a", os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600, dir_fd=fd))
    libc.renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    result = libc.renameat2(fd, b"probe-a", fd, b"probe-b", 1)
    outcome = "ok" if result == 0 else failure()
    for name in ("probe-a", "probe-b"):
        try:
            os.unlink(name, dir_fd=fd)
        except OSError:
            pass
    return outcome


def fsync_dir(fd):
    try:
        os.fsync(fd)
        return "ok"
    except OSError as error:
        return "errno %d %s" % (error.errno, errno.errorcode.get(error.errno, "?"))


def replay(root):
    """The probe's own steps through the product's filesystem class, with the errno behind each refusal."""
    from exomem import _held_fs_posix as held
    capabilities = held.Capabilities(True, True, True, True, True)
    fs = held.PosixHeldFilesystem(held._open(root, held._DIRECTORY_FLAGS), capabilities)
    stem = ".gv-replay-%d" % os.getpid()
    names = (stem, stem + "-renamed", stem + "-link")
    steps = {}

    def attempt(label, call):
        result = call()
        steps[label] = "ok" if result.ok else "%s cause=%r" % (result.error.code, result.error.cause)

    try:
        with fs.parent(".").require() as parent:
            with fs.file(parent, names[0], access="write", create=True, exclusive=True).require() as source:
                attempt("write", lambda: fs.write(source, b"probe"))
            with fs.file(parent, names[0], access="mutate").require() as source:
                attempt("link", lambda: fs.link(source, parent, names[2]))
                attempt("rename", lambda: fs.rename(source, parent, names[1]))
            with fs.file(parent, names[1], access="mutate").require() as renamed:
                attempt("unlink", lambda: fs.unlink(renamed))
    except Exception as error:
        steps["exception"] = "%s: %s" % (type(error).__name__, error)
    finally:
        for name in names:
            try:
                os.unlink(name, dir_fd=fs.descriptor)
            except OSError:
                pass
        fs.close()
    return steps


report = {"kernel": os.uname().release, "roots": {}}
for root in sys.argv[1:]:
    entry = {}
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    for label, resolve in (("none", 0), ("no_xdev", 1), ("no_magiclinks", 2), ("no_symlinks", 4),
                           ("beneath", 8), ("all_four", 15)):
        entry["openat2_" + label] = openat2(fd, resolve)
    entry["renameat2_noreplace"] = renameat2(fd)
    entry["fsync_directory"] = fsync_dir(fd)
    os.close(fd)
    try:
        entry["product_replay"] = replay(root)
    except Exception as error:
        entry["product_replay"] = "%s: %s" % (type(error).__name__, error)
    with open("/proc/self/mountinfo") as handle:
        entry["mountinfo"] = [line.strip()[:200] for line in handle if " %s " % root in line]
    try:
        from exomem import _held_fs_posix as held
        caps = held.probe(Path(root))
        entry["product_probe"] = {"relative_operations": caps.relative_operations, "no_follow": caps.no_follow,
                                  "same_device_rename": caps.same_device_rename, "hard_link": caps.hard_link,
                                  "reason": caps.reason}
    except Exception as error:
        entry["product_probe"] = "%s: %s" % (type(error).__name__, error)
    report["roots"][root] = entry
print(json.dumps(report))
'''

FS_SCRIPT = r'''
import json, os, shutil, sqlite3, sys, time
root, count = sys.argv[1], 200
os.makedirs(root, exist_ok=True)
payload, clock, out = b"x" * 2048, time.perf_counter, {}
t = clock()
for i in range(count):
    fd = os.open("%s/f%d.tmp" % (root, i), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.write(fd, payload)
    os.fsync(fd)
    os.close(fd)
    os.replace("%s/f%d.tmp" % (root, i), "%s/f%d" % (root, i))
out["write_fsync_replace_200"] = (clock() - t) * 1000
t = clock()
for i in range(count):
    with open("%s/f%d" % (root, i), "rb") as handle:
        handle.read()
out["read_200"] = (clock() - t) * 1000
t = clock()
for i in range(count):
    os.stat("%s/f%d" % (root, i))
out["stat_200"] = (clock() - t) * 1000
t = clock()
database = sqlite3.connect(root + "/bench.db")
database.execute("create table t (a integer, b text)")
for i in range(count):
    database.execute("insert into t values (?, ?)", (i, "row"))
    database.commit()
database.close()
out["sqlite_commit_200"] = (clock() - t) * 1000
t = clock()
fd = os.open(root + "/big", os.O_WRONLY | os.O_CREAT, 0o600)
chunk = b"y" * (1 << 20)
for _ in range(64):
    os.write(fd, chunk)
os.fsync(fd)
os.close(fd)
out["write_64mib_fsync"] = (clock() - t) * 1000
shutil.rmtree(root, ignore_errors=True)
print(json.dumps(out))
'''

BRAKE_SCRIPT = r'''
import json, os, pathlib
facts = {}
for path in ("/proc/self/cgroup", "/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory.high", "/sys/fs/cgroup/memory.current",
             "/sys/fs/cgroup/cgroup.controllers", "/sys/fs/cgroup/cpu.max"):
    try:
        facts[path] = pathlib.Path(path).read_text().strip()[:200]
    except OSError as error:
        facts[path] = "unreadable: %s" % error
facts["ls_sys_fs_cgroup"] = sorted(os.listdir("/sys/fs/cgroup"))[:40] if os.path.isdir("/sys/fs/cgroup") else "absent"
try:
    from exomem import media_brakes
    facts["media_brakes.read_cell"] = repr(media_brakes.read_cell())
except Exception as error:
    facts["media_brakes.read_cell"] = "%s: %s" % (type(error).__name__, error)
print(json.dumps(facts))
'''

HOST_MEMORY_SCRIPT = r'''
import json, os, sys
cid = sys.argv[1]
skip = {os.getpid(), os.getppid()}


def meminfo():
    out = {}
    for line in open("/proc/meminfo"):
        key, _, rest = line.partition(":")
        out[key] = int(rest.split()[0])
    return out


def kv(path, wanted):
    out = {}
    for line in open(path):
        fields = line.split()
        if fields and fields[0].rstrip(":") in wanted:
            out[fields[0].rstrip(":")] = int(fields[1])
    return out


info = meminfo()
used = info["MemTotal"] - info["MemFree"] - info["Buffers"] - info["Cached"] - info["SReclaimable"]
processes, cgroups = [], set()
for entry in os.listdir("/proc"):
    if not entry.isdigit() or int(entry) in skip:
        continue
    try:
        command = open("/proc/%s/cmdline" % entry, "rb").read().replace(b"\0", b" ").decode(errors="replace")
        cgroup = open("/proc/%s/cgroup" % entry).read()
    except OSError:
        continue
    if cid == "none" or (cid not in command and cid not in cgroup):
        continue
    try:
        rollup = kv("/proc/%s/smaps_rollup" % entry, {"Rss", "Pss", "Pss_Anon", "Pss_File", "Pss_Shmem"})
        name = open("/proc/%s/comm" % entry).read().strip()
    except OSError:
        continue
    processes.append({"pid": int(entry), "comm": name, "cmd": command[:200], **rollup})
    for line in cgroup.splitlines():
        # Only the cell's own cgroups: a shim shares the host service's cgroup, which holds other containers.
        if line.startswith("0::") and len(line) > 4 and cid in line:
            cgroups.add(line[3:])
# A cgroup under another in the set is already counted by its parent.
leaves = sorted(path for path in cgroups if not any(other != path and path.startswith(other.rstrip("/") + "/") for other in cgroups))
groups = {}
for path in leaves:
    base = "/sys/fs/cgroup" + path
    try:
        groups[path] = {"current_kb": int(open(base + "/memory.current").read()) // 1024,
                        **{k + "_kb": v // 1024 for k, v in kv(base + "/memory.stat", {"anon", "file", "kernel", "shmem"}).items()}}
    except OSError as error:
        groups[path] = {"unreadable": str(error)}
# Under a VM, one thread per virtual CPU is named "CPU n/KVM"; this is how many the cell's VM has right now.
vcpu_threads = 0
for item in processes:
    if item["comm"].startswith("qemu"):
        for task in os.listdir("/proc/%d/task" % item["pid"]):
            try:
                vcpu_threads += open("/proc/%d/task/%s/comm" % (item["pid"], task)).read().strip().endswith("/KVM")
            except OSError:
                pass
by_comm = {}
for item in processes:
    row = by_comm.setdefault(item["comm"], {"processes": 0, "pss_kb": 0, "rss_kb": 0})
    row["processes"] += 1
    row["pss_kb"] += item.get("Pss", 0)
    row["rss_kb"] += item.get("Rss", 0)
print(json.dumps({
    "host_used_kb": used, "mem_available_kb": info["MemAvailable"],
    "pss_kb": sum(i.get("Pss", 0) for i in processes), "rss_kb": sum(i.get("Rss", 0) for i in processes),
    "cgroup_current_kb": sum(g.get("current_kb", 0) for g in groups.values()) if groups else None,
    "vcpu_threads": vcpu_threads, "by_comm": by_comm, "cgroups": groups, "processes": processes}))
'''

# What the cell itself sees: its own cgroup and, under a VM, the guest's memory.
CELL_VIEW_SCRIPT = r'''
import json, os
def read(path):
    try:
        return open(path).read().strip()
    except OSError as error:
        return "unreadable: %s" % error
stat = {}
for line in read("/sys/fs/cgroup/memory.stat").splitlines():
    fields = line.split()
    if len(fields) == 2 and fields[0] in ("anon", "file", "kernel", "shmem", "slab"):
        stat[fields[0]] = fields[1]
cpu = {"cpu_count": os.cpu_count(), "affinity": len(os.sched_getaffinity(0)), "online": read("/sys/devices/system/cpu/online"),
       "cpu.max": read("/sys/fs/cgroup/cpu.max"), "cpuset.cpus.effective": read("/sys/fs/cgroup/cpuset.cpus.effective")}
print(json.dumps({"cpu": cpu, "memory_current": read("/sys/fs/cgroup/memory.current"), "memory_stat": stat,
                  "meminfo": read("/proc/meminfo").splitlines()[:6]}))
'''

# A traced container waits on a FIFO until the host has attached strace to its first process, so no syscall
# is missed. The wrapper's own steps run before the attach; `exec` then replaces the shell with the command.
TRACE_GATE = 'mkfifo /tmp/go && read -r _ < /tmp/go; exec "$@"'

# The backup Job's restic calls (backup, forget, check, restore), against a local repository.
RESTIC_SCRIPT = r'''
set -e
mkdir -p /data/host
restic snapshots || restic init
restic backup --json /data/vault /data/host > /tmp/backup.json
restic snapshots
restic forget --keep-last 1 --prune
restic check
restic restore latest --target /tmp/restore
'''

REBUILD_SCRIPT = r'''
import json, time
from pathlib import Path
from exomem import embeddings
t = time.perf_counter()
chunks = embeddings.get_embedding_index(Path("/data/vault")).rebuild_all()
print(json.dumps({"chunks": int(chunks), "seconds": time.perf_counter() - t}))
'''


class Lab:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.image = args.image
        self.out = Path(args.out)
        self.work = Path(args.work)
        self.run_id = uuid.uuid4().hex[:8]
        self.network = f"kv-net-{self.run_id}"
        self.token = secrets.token_urlsafe(32)
        self.fx = self.work / "fx"
        self.snap = self.work / "snap"
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / "logs").mkdir(exist_ok=True)
        self.results = (self.out / "results.jsonl").open("a", encoding="utf-8")
        self.events = (self.out / "events.log").open("a", encoding="utf-8")
        self.arms: list[str] = [arm for arm in args.arms.split(",") if arm]
        self.stems: list[str] = []
        self.import_counts: dict = {}
        self.media_files: list[str] = []
        self.blocks: dict[str, str] = {}  # block-arm volume directory -> loop device
        self.slow = 1  # deadline multiplier: strace slows the traced cell several times over

    # -- records
    def log(self, message: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {message}"
        print(line, flush=True)
        self.events.write(line + "\n")
        self.events.flush()

    def record(self, arm: str, rep: int, case: str, values: list[float], *, unit: str = "ms",
               items: int = 1, ok: bool = True, note: str = "") -> None:
        row = {"arm": arm, "rep": rep, "case": case, "values": [round(v, 3) for v in values],
               "unit": unit, "items": items, "ok": ok, "note": note[:600]}
        self.results.write(json.dumps(row) + "\n")
        self.results.flush()

    def fail(self, arm: str, rep: int, case: str, error: str) -> None:
        self.log(f"FAIL {arm} r{rep} {case}: {error[:400]}")
        self.record(arm, rep, case, [], ok=False, note=error)

    def diag(self, name: str, value) -> None:
        path = self.out / "diagnostics" / f"{name}.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(value, indent=1, default=str)[:200000], encoding="utf-8")

    # -- docker
    def docker(self, *argv: str, check: bool = True, timeout: float | None = 900):
        return sh(["docker", *argv], check=check, timeout=timeout)

    def security(self) -> list[str]:
        return ["--read-only", "--tmpfs", "/tmp:rw,size=64m,mode=1777", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "-u", "10001:10001"]

    def cell_env(self) -> list[str]:
        values = {
            "EXOMEM_CLOUD_CELL": "1", "EXOMEM_CLOUD_CELL_ID": CELL_ID,
            "EXOMEM_CLOUD_CELL_TOKEN": self.token, "EXOMEM_VAULT_PATH": "/data/vault",
            "TMPDIR": "/data/scratch/tmp", "EXOMEM_LOG_DIR": "/tmp/exomem-logs",
            "EXOMEM_MEDIA_ENGINES": "documents,ocr",
        }
        argv: list[str] = []
        for key, value in values.items():
            argv += ["-e", f"{key}={value}"]
        return argv

    def helper(self, volume: str, script: str, *, mounts: list[str] | None = None, user: str = "0:0",
               timeout: float = 600):
        """An unmeasured runc helper on the volume (seeding, restoring, copying inputs)."""
        argv = ["run", "--rm", "--runtime", "runc", "--network", "none", "-u", user, "-v", f"{volume}:/data"]
        for mount in mounts or []:
            argv += ["-v", mount]
        argv += ["--entrypoint", "sh", self.image, "-c", script]
        return self.docker(*argv, timeout=timeout)

    def oneshot_options(self, runtime: str, volume: str, *, mounts: list[str] | None = None,
                        env: dict[str, str] | None = None, limits: bool = False, extra: list[str] | None = None) -> list[str]:
        argv = ["--runtime", docker_runtime(runtime), "--network", "none", *self.security(), "-v", f"{volume}:/data",
                *(extra or [])]
        if limits:
            argv += ["--cpus", "2", "--memory", "3g"]
        for mount in mounts or []:
            argv += ["-v", mount]
        for key, value in {"EXOMEM_CLOUD_CELL": "1", "EXOMEM_CLOUD_CELL_ID": CELL_ID, **(env or {})}.items():
            argv += ["-e", f"{key}={value}"]
        return argv

    def oneshot(self, runtime: str, volume: str, entrypoint_args: list[str], *, entrypoint: str | None = None,
                mounts: list[str] | None = None, env: dict[str, str] | None = None, timeout: float = 600,
                limits: bool = False):
        argv = ["run", "--rm", *self.oneshot_options(runtime, volume, mounts=mounts, env=env, limits=limits)]
        if entrypoint:
            argv += ["--entrypoint", entrypoint]
        argv += [self.image, *entrypoint_args]
        start = now_ms()
        result = sh(["docker", *argv], check=False, timeout=timeout)
        return now_ms() - start, result

    def new_volume(self, name: str) -> None:
        self.docker("volume", "create", name)
        self.helper(name, "chown 10001:10001 /data && chmod 2770 /data")

    def block_volume(self, name: str) -> str:
        """A sparse ext4 image on a loop device, seeded like the Docker volumes, assigned to Kata as a block volume.

        Returns the host directory that stands for the volume in `-v DIR:/data`: the runtime finds the device
        behind it in the mount information that `kata-runtime direct-volume add` stored for that path.
        """
        base = self.work / "blk"
        image, mount, volume = base / f"{name}.img", base / f"{name}.mnt", base / name
        for path in (mount, volume):
            path.mkdir(parents=True, exist_ok=True)
        sh(["truncate", "-s", "8G", str(image)])
        sh(["mkfs.ext4", "-q", "-F", str(image)])
        sh(["sudo", "mount", "-o", "loop", str(image), str(mount)])
        try:
            seed = self.snap / "seed.tar"
            if seed.exists():
                sh(["sudo", "tar", "-C", str(mount), "-xpf", str(seed)])
            sh(["sudo", "chown", "10001:10001", str(mount)])
            sh(["sudo", "chmod", "2770", str(mount)])
        finally:
            sh(["sudo", "umount", str(mount)], check=False)
        loop = sh(["sudo", "losetup", "--find", "--show", str(image)]).stdout.strip()
        self.blocks[str(volume)] = loop
        info = json.dumps({"volume-type": "block", "device": loop, "fstype": "ext4", "options": []})
        sh(["sudo", KATA_RUNTIME_BIN, "direct-volume", "add", "--volume-path", str(volume), "--mount-info", info])
        return str(volume)

    def block_volume_remove(self, volume: str) -> None:
        loop = self.blocks.pop(volume, None)
        sh(["sudo", KATA_RUNTIME_BIN, "direct-volume", "remove", "--volume-path", volume], check=False)
        if loop:
            sh(["sudo", "losetup", "-d", loop], check=False)
        Path(volume + ".img").unlink(missing_ok=True)

    def start_cell(self, runtime: str, name: str, volume: str, *, gated: bool = False) -> tuple[str, float, float]:
        """Returns (base url, `docker run -d` milliseconds, clock at the call). A gated cell waits for the tracer."""
        command = CELL_COMMAND.replace('&& exec "$@"', f"&& {TRACE_GATE}") if gated else CELL_COMMAND
        argv = ["run", "-d", "--name", name, "--runtime", docker_runtime(runtime), "--network", self.network,
                "--cpus", "2", "--memory", "3g", *self.security(),
                "-v", f"{volume}:/data", *self.cell_env(),
                "--entrypoint", "sh", self.image, "-c", command, "cell-process", "exomem",
                "--transport", "http", "--host", "0.0.0.0", "--port", str(CELL_PORT)]
        t0 = now_ms()
        self.docker(*argv)
        created = now_ms() - t0
        info = self.docker("inspect", "--format", "{{json .NetworkSettings.Networks}}", name)
        address = json.loads(info.stdout)[self.network]["IPAddress"]
        return f"http://{address}:{CELL_PORT}", created, t0

    def wait_http(self, url: str, path: str, t0: float, timeout: float, container: str) -> float | None:
        deadline = time.monotonic() + timeout
        next_check = time.monotonic() + 5
        with httpx.Client(timeout=2.0, trust_env=False) as client:
            while time.monotonic() < deadline:
                try:
                    if client.get(url + path).status_code == 200:
                        return now_ms() - t0
                except httpx.HTTPError:
                    pass
                if time.monotonic() > next_check:
                    state = self.docker("inspect", "--format", "{{.State.Running}}", container, check=False)
                    if state.stdout.strip() != "true":
                        self.log(f"{container} is not running; no point waiting for {path}")
                        return None
                    next_check = time.monotonic() + 5
                time.sleep(0.05)
        return None

    def stop_cell(self, name: str, tag: str) -> dict:
        facts: dict = {}
        stats = self.docker("stats", "--no-stream", "--format", "{{json .}}", name, check=False, timeout=60)
        facts["stats"] = stats.stdout.strip()
        state = self.docker("inspect", "--format", "{{json .State}}", name, check=False)
        facts["state"] = state.stdout.strip()
        logs = self.docker("logs", "--tail", "400", name, check=False)
        (self.out / "logs" / f"{tag}.log").write_text((logs.stdout + logs.stderr).replace(self.token, "<token>"))
        self.docker("stop", "-t", "15", name, check=False)
        self.docker("rm", "-f", name, check=False)
        return facts

    # -- memory
    def host_memory(self, container: str) -> dict:
        """Host-side memory of one cell: the processes that carry it and the cgroups they sit in, read as root."""
        cid = "none"
        if container != "none":
            cid = self.docker("inspect", "--format", "{{.Id}}", container).stdout.strip()
        result = sh(["sudo", "python3", "-", cid], check=False, timeout=120, input_text=HOST_MEMORY_SCRIPT)
        if result.returncode != 0:
            raise HarnessError(f"host memory read failed: {result.stderr[-400:]}")
        return json.loads(result.stdout)

    def memory(self, arm: str, rep: int, container: str, moment: str) -> None:
        """Record the host-side memory of the cell and what the cell sees, in kB."""
        try:
            host = self.host_memory(container)
            self.diag(f"memory-{arm}-r{rep}-{moment}", host)
            self.record(arm, rep, f"m.host_pss_{moment}", [host["pss_kb"]], unit="kB",
                        note=json.dumps(host["by_comm"])[:500])
            self.record(arm, rep, f"m.host_rss_{moment}", [host["rss_kb"]], unit="kB")
            if host["cgroup_current_kb"] is not None:
                self.record(arm, rep, f"m.host_cgroup_{moment}", [host["cgroup_current_kb"]], unit="kB",
                            note=json.dumps(host["cgroups"])[:500])
            view = self.docker("exec", container, "python", "-c", CELL_VIEW_SCRIPT, check=False, timeout=60)
            if view.returncode == 0:
                seen = json.loads(view.stdout)
                self.diag(f"cellview-{arm}-r{rep}-{moment}", seen)
                if seen["memory_current"].isdigit():
                    self.record(arm, rep, f"m.cell_view_cgroup_{moment}", [int(seen["memory_current"]) / 1024], unit="kB",
                                note="memory.current read inside the container")
        except Exception as error:  # noqa: BLE001 - recorded, the session goes on
            self.fail(arm, rep, f"m.{moment}", f"{type(error).__name__}: {error}")

    # -- environment facts
    def environment(self) -> None:
        facts: dict = {}
        for key, argv in {
            "nproc": ["nproc"], "uname": ["uname", "-a"], "free": ["free", "-m"],
            "lscpu": ["lscpu"], "docker_version": ["docker", "version", "--format", "{{json .}}"],
            "docker_info": ["docker", "info", "--format", "{{json .}}"],
            "kata_version": ["/opt/kata/bin/kata-runtime", "--version"], "kvm": ["ls", "-l", "/dev/kvm"],
            "daemon_json": ["cat", "/etc/docker/daemon.json"],
            "image": ["docker", "image", "inspect", self.image, "--format",
                      "{{.Id}} {{json .RepoDigests}} {{.Os}}/{{.Architecture}} size={{.Size}}"],
            "os_release": ["cat", "/etc/os-release"],
        }.items():
            result = sh(argv, check=False, timeout=60)
            facts[key] = (result.stdout + result.stderr).strip()[:6000]
        self.diag("environment", facts)

    def kata_facts(self) -> None:
        """The hypervisor, its command line, the guest kernel and the Kata configuration the arm ran with."""
        facts: dict = {}
        for key, argv in {
            "kata_runtime_version": ["/opt/kata/bin/kata-runtime", "--version"],
            "kata_runtime_check": ["sudo", "/opt/kata/bin/kata-runtime", "check"],
            "kata_runtime_env": ["sudo", "/opt/kata/bin/kata-runtime", "env"],
            "qemu_version": ["/opt/kata/bin/qemu-system-x86_64", "--version"],
            "installed_files": ["sh", "-c", "ls -l /opt/kata/bin /opt/kata/share/defaults/kata-containers "
                                "/opt/kata/share/kata-containers /opt/kata/libexec 2>&1 | head -120"],
            "configuration": ["sh", "-c", "grep -vE '^[[:space:]]*(#|$)' "
                              "/opt/kata/share/defaults/kata-containers/configuration.toml | head -200"],
        }.items():
            result = sh(argv, check=False, timeout=120)
            facts[key] = (result.stdout + result.stderr).strip()[:12000]
        name = f"kv-facts-{self.run_id}"
        self.docker("run", "-d", "--name", name, "--runtime", "kata", "--cpus", "2", "--memory", "3g",
                    "--entrypoint", "sleep", self.image, "120", check=False, timeout=300)
        for key, argv in {
            "guest_uname": ["docker", "exec", name, "uname", "-a"],
            "guest_cmdline": ["docker", "exec", name, "cat", "/proc/cmdline"],
            "guest_cpus": ["docker", "exec", name, "nproc"],
            "guest_meminfo": ["docker", "exec", name, "sh", "-c", "head -8 /proc/meminfo"],
            "guest_mounts": ["docker", "exec", name, "sh", "-c", "cat /proc/self/mountinfo | cut -c1-200"],
            "host_vm_processes": ["sh", "-c", "ps -eo pid,rss,args | grep -E 'qemu|virtiofsd|kata' | grep -v grep "
                                  "| cut -c1-2500"],
        }.items():
            result = sh(argv, check=False, timeout=60)
            facts[key] = (result.stdout + result.stderr).strip()[:12000]
        self.docker("rm", "-f", name, check=False)
        self.diag("kata_facts", facts)

    # -- compatibility
    def compat(self) -> None:
        """Which kernel primitives the cell's filesystem layer needs, per runtime and root; unmeasured."""
        volume = f"kv-compat-{self.run_id}"
        self.new_volume(volume)
        block = ""
        try:
            for runtime in (*RUNTIMES, *([BLOCK_ARM] if BLOCK_ARM in self.arms else [])):
                if runtime == BLOCK_ARM:
                    block = self.block_volume(f"compat-{self.run_id}")
                wall, probed = self.oneshot(runtime, block if runtime == BLOCK_ARM else volume,
                                            ["python", "-c", PROBE_SCRIPT, "/data", "/tmp"],
                                            entrypoint="env", timeout=300)
                self.log(f"compat {runtime} rc={probed.returncode} {wall:.0f} ms {probed.stdout.strip()[:1200]}"
                         f"{probed.stderr.strip()[-300:]}")
                self.diag(f"compat-{runtime}", {"rc": probed.returncode, "stdout": probed.stdout,
                                                "stderr": probed.stderr[-2000:]})
        finally:
            self.docker("volume", "rm", "-f", volume, check=False)
            if block:
                self.block_volume_remove(block)

    def cgroup_probe(self) -> None:
        """What the cell's media brakes read from the cgroup files, in a container limited like the cell."""
        volume = f"kv-brake-{self.run_id}"
        self.new_volume(volume)
        try:
            for runtime in self.arms:
                if runtime == BLOCK_ARM:
                    continue  # the brake reads cgroup files, which the storage mode does not change
                wall, probed = self.oneshot(runtime, volume, ["python", "-c", BRAKE_SCRIPT], entrypoint="env",
                                            timeout=300, limits=True)
                self.log(f"cgroup {runtime} rc={probed.returncode} {probed.stdout.strip()[:1400]}{probed.stderr.strip()[-300:]}")
                self.diag(f"cgroup-{runtime}", {"rc": probed.returncode, "stdout": probed.stdout,
                                                "stderr": probed.stderr[-2000:]})
        finally:
            self.docker("volume", "rm", "-f", volume, check=False)

    # -- seed
    def make_fixtures(self) -> None:
        shutil.rmtree(self.fx, ignore_errors=True)
        self.stems = fixtures.write_vault_seed(self.fx / "vault", self.args.seed_notes)
        self.import_counts = fixtures.write_import_folder(self.fx / "import", IMPORT_NAME)
        samples = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "media-samples"
        media = fixtures.write_media(self.fx / "media", samples)
        self.media_files = sorted(media)
        for path in [self.fx, *self.fx.rglob("*")]:
            path.chmod(0o755 if path.is_dir() else 0o644)
        self.log(f"fixtures: notes={len(self.stems)} import={self.import_counts} media={media}")

    def seed(self) -> None:
        """Build one seeded volume under runc and keep it as a tarball; unmeasured."""
        self.snap.mkdir(parents=True, exist_ok=True)
        self.snap.chmod(0o777)
        volume = f"kv-seed-{self.run_id}"
        self.new_volume(volume)
        wall, init = self.oneshot("runc", volume, ["exomem", "cell-init", "--vault", "/data/vault", "--json"],
                                  entrypoint="env")
        self.log(f"seed cell-init rc={init.returncode} {wall:.0f} ms {init.stdout.strip()[-200:]}")
        if init.returncode != 0:
            raise HarnessError(f"seed cell-init failed: {init.stdout[-800:]}{init.stderr[-800:]}")
        self.helper(volume, "cp -a /seed/. /data/vault/ && ls /data/vault",
                    mounts=[f"{self.fx / 'vault'}:/seed:ro"], user="10001:10001")
        wall, rebuilt = self.oneshot("runc", volume, ["python", "-c", REBUILD_SCRIPT], entrypoint="env",
                                     env={"EXOMEM_VAULT_PATH": "/data/vault"}, timeout=1500)
        self.log(f"seed embeddings rc={rebuilt.returncode} {wall:.0f} ms {rebuilt.stdout.strip()[-300:]}")
        if rebuilt.returncode != 0:
            raise HarnessError(f"seed embedding build failed: {rebuilt.stdout[-800:]}{rebuilt.stderr[-1500:]}")
        # Prove the seed through the cell itself before snapshotting it.
        name = f"kv-seedcell-{self.run_id}"
        url, _, t0 = self.start_cell("runc", name, volume)
        try:
            ready = self.wait_http(url, "/health/ready", t0, 300, name)
            self.log(f"seed cell ready after {ready} ms")
            if ready is None:
                raise HarnessError("seed cell never became ready")
            mcp = Mcp(url, self.token)
            mcp.initialize()
            tools = mcp.list_tools()
            self.diag("tools_list", [{"name": t.get("name"), "schema": t.get("inputSchema")} for t in tools])
            counts = []
            for query in fixtures.SEARCH_QUERIES:
                answer = self.search_until_warm(mcp, query)
                counts.append(len(hits_of(answer.structured)) if answer.ok else -1)
            self.log(f"seed hits per query: {counts}")
            self.diag("seed_validation", {"hits": counts, "tool_spellings": mcp.names})
            if all(count <= 0 for count in counts):
                raise HarnessError("the seeded cell answered no query with a hit")
            mcp.close()
        finally:
            self.stop_cell(name, "seed-cell")
        self.helper(volume, "tar -C /data -cf /snap/seed.tar .", mounts=[f"{self.snap}:/snap"])
        self.docker("volume", "rm", "-f", volume, check=False)
        self.log(f"seed snapshot {(self.snap / 'seed.tar').stat().st_size} bytes")

    def search_until_warm(self, mcp: Mcp, query: str, limit_s: float = 240.0) -> Call:
        deadline = time.monotonic() + limit_s * self.slow
        while True:
            answer = mcp.call("search", {"query": query})
            warming = answer.code == "RETRIEVAL_INDEX_WARMING" or "embeddings" in warming_components(answer.structured)
            if not warming or time.monotonic() > deadline:
                return answer
            time.sleep(0.5)

    # -- one session
    def session(self, arm: str, rep: int) -> None:
        tag = f"{arm}-r{rep}"
        volume = f"kv-{arm}-{rep}-{self.run_id}"
        name = f"kv-cell-{arm}-{rep}-{self.run_id}"
        self.log(f"=== session {tag}")
        if arm == BLOCK_ARM:
            volume = self.block_volume(f"{arm}-{rep}-{self.run_id}")
        else:
            self.new_volume(volume)
            self.helper(volume, "tar -C /data -xpf /snap/seed.tar && chmod 2770 /data",
                        mounts=[f"{self.snap}:/snap:ro"])
        try:
            self.session_body(arm, rep, tag, volume, name)
        finally:
            self.docker("rm", "-f", name, check=False)
            if arm == BLOCK_ARM:
                self.block_volume_remove(volume)
            else:
                self.docker("volume", "rm", "-f", volume, check=False)

    def session_body(self, arm: str, rep: int, tag: str, volume: str, name: str) -> None:
        a = self.args
        # (e) the init container's path
        wall, init = self.oneshot(arm, volume, ["exomem", "cell-init", "--vault", "/data/vault", "--json"],
                                  entrypoint="env")
        if init.returncode == 0:
            self.record(arm, rep, "e.cell_init_run_to_exit", [wall])
        else:
            self.fail(arm, rep, "e.cell_init_run_to_exit", init.stdout[-300:] + init.stderr[-600:])
        # (c) part 1: the stopped-cell import Job's work, before the cell starts
        import_id = None
        if "import" in a.only:
            try:
                import_id = self.import_stage(arm, rep, volume)
            except Exception as error:  # noqa: BLE001 - recorded, the session goes on
                self.fail(arm, rep, "c.import_stage_job", f"{type(error).__name__}: {error}")
        if "fs" in a.only:
            self.fs_micro(arm, rep, volume)
        # (e) startup
        url, created, t0 = self.start_cell(arm, name, volume)
        live = self.wait_http(url, "/health", t0, 240, name)
        ready = self.wait_http(url, "/health/ready", t0, 400, name)
        self.record(arm, rep, "e.docker_run_returns", [created])
        for case, value in (("e.start_to_live", live), ("e.start_to_ready", ready)):
            if value is None:
                self.fail(arm, rep, case, "never answered 200")
            else:
                self.record(arm, rep, case, [value])
        if ready is None:
            self.stop_cell(name, f"{tag}-cell")
            return
        time.sleep(5)
        self.memory(arm, rep, name, "idle")
        mcp = Mcp(url, self.token)
        try:
            mcp.initialize()
            sections = (
                ("search", lambda: self.work_search(arm, rep, url, mcp)),
                ("write", lambda: self.work_write(arm, rep, mcp)),
                ("media", lambda: self.work_media(arm, rep, mcp, name)),
                ("import", lambda: self.work_import_cell(arm, rep, mcp, import_id)),
            )
            for key, work in sections:
                if key not in a.only:
                    continue
                try:
                    work()
                except Exception as error:  # noqa: BLE001 - recorded, the session goes on
                    self.fail(arm, rep, f"section.{key}", f"{type(error).__name__}: {error}")
            self.diag(f"spellings-{tag}", mcp.names)
            self.memory(arm, rep, name, "after")
        finally:
            mcp.close()
            facts = self.stop_cell(name, f"{tag}-cell")
            self.record(arm, rep, "z.container_facts", [], note=json.dumps(facts)[:590])
            (self.out / "logs" / f"{tag}-facts.json").write_text(json.dumps(facts, indent=1))

    # -- (a)
    def work_search(self, arm: str, rep: int, url: str, mcp: Mcp) -> None:
        queries = fixtures.SEARCH_QUERIES
        # The first answer pays the model load; keep it as its own case, with the warming it waited out.
        start = now_ms()
        first = self.search_until_warm(mcp, queries[0])
        total = now_ms() - start
        if first.ok:
            self.record(arm, rep, "a.search_first_until_warm", [total],
                        note=f"hits={len(hits_of(first.structured))} last_call_ms={first.ms:.0f}")
        else:
            self.fail(arm, rep, "a.search_first_until_warm", first.error)
            return
        values, empty = [], 0
        for _ in range(3):
            for query in queries:
                answer = mcp.call("search", {"query": query})
                if answer.ok:
                    values.append(answer.ms)
                    empty += 0 if hits_of(answer.structured) else 1
                else:
                    self.log(f"search error {answer.error[:200]}")
        self.record(arm, rep, "a.search_warm", values, ok=bool(values), note=f"queries_without_hits={empty}")
        values = []
        for _ in range(2):
            for turn in fixtures.RECALL_TURNS:
                answer = mcp.call("recall", {"turn": turn})
                if answer.ok:
                    values.append(answer.ms)
                else:
                    self.log(f"recall error {answer.error[:200]}")
        self.record(arm, rep, "a.recall_context", values, ok=bool(values))
        values = []
        for _ in range(2):
            for stem in self.stems[:10]:
                answer = mcp.call("read", {"path": f"{fixtures.NOTE_FOLDER}/{stem}.md"})
                if answer.ok:
                    values.append(answer.ms)
                else:
                    self.log(f"read error {answer.error[:200]}")
        self.record(arm, rep, "a.read_page", values, ok=bool(values))
        # Throughput: four clients, each running the twelve queries.
        def client_run() -> int:
            local = Mcp(url, self.token)
            try:
                local.initialize()
                local.names = dict(mcp.names)
                return sum(1 for query in queries if local.call("search", {"query": query}).ok)
            finally:
                local.close()
        start = now_ms()
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            done = sum(f.result() for f in [pool.submit(client_run) for _ in range(4)])
        wall = now_ms() - start
        self.record(arm, rep, "a.search_4_clients_wall", [wall], items=done, ok=done > 0)

    # -- (b)
    def work_write(self, arm: str, rep: int, mcp: Mcp) -> None:
        def write(index: int, marker: str) -> Call:
            body = (f"Benchmark note {index} for {arm} repeat {rep}. Edit anchor {marker}. "
                    + " ".join(fixtures.FILLER[index % len(fixtures.FILLER)].split()) + ".")
            deadline = time.monotonic() + 120 * self.slow
            while True:
                answer = mcp.call("write", {"content": body, "title": f"gvbench {arm} {rep} {index} {marker}",
                                            "status": "draft"})
                if answer.code != "MUTATION_WARMING" or time.monotonic() > deadline:
                    return answer
                time.sleep(0.3)
        first = write(0, "mzero")
        if first.ok:
            self.record(arm, rep, "b.first_write", [first.ms], note=json.dumps(first.structured)[:500])
        else:
            self.fail(arm, rep, "b.first_write", first.error)
            return
        paths: list[tuple[str, str]] = []
        values = []
        for index in range(1, 16):
            marker = f"mk{secrets.token_hex(4)}"
            answer = write(index, marker)
            if answer.ok:
                values.append(answer.ms)
                path = find_path(answer.structured)
                if path:
                    paths.append((path, marker))
            else:
                self.log(f"write error {answer.error[:200]}")
        self.record(arm, rep, "b.write_note", values, ok=bool(values), note=f"paths_found={len(paths)}")
        values = []
        for path, marker in paths[:10]:
            answer = mcp.call("edit", {"path": path, "why": "benchmark edit",
                                       "old_string": f"Edit anchor {marker}",
                                       "new_string": f"Edited anchor {marker}"})
            if answer.ok:
                values.append(answer.ms)
            else:
                self.log(f"edit error {answer.error[:300]}")
        self.record(arm, rep, "b.edit_note", values, ok=bool(values))
        values = []
        for index in range(3):
            marker = f"zq{secrets.token_hex(5)}"
            acked = write(100 + index, marker)
            if not acked.ok:
                continue
            start = now_ms()
            deadline = time.monotonic() + 180 * self.slow
            while time.monotonic() < deadline:
                answer = mcp.call("search", {"query": marker})
                if answer.ok and hits_of(answer.structured):
                    values.append(now_ms() - start)
                    break
                time.sleep(0.1)
        self.record(arm, rep, "b.write_to_searchable", values, ok=bool(values))

    # -- (d)
    def work_media(self, arm: str, rep: int, mcp: Mcp, container: str) -> None:
        # Files go in through the running container, so each arm stages them the same way after the cell is up.
        for filename in self.media_files:
            with (self.fx / "media" / filename).open("rb") as source:
                staged = subprocess.run(
                    ["docker", "exec", "-i", "-u", "10001:10001", container, "sh", "-c",
                     f"mkdir -p '/data/vault/{MEDIA_DIR}' && cat > '/data/vault/{MEDIA_DIR}/{filename}'"],
                    stdin=source, capture_output=True, check=False, timeout=120)
            if staged.returncode != 0:
                raise HarnessError(f"staging {filename} failed: {staged.stderr[-300:]!r}")
        for filename in self.media_files:
            path = f"{MEDIA_DIR}/{filename}"
            case = f"d.media_{filename.replace('.', '_').replace('-', '_')}"
            start = now_ms()
            acked = mcp.call("media", {"operation": "process", "paths": [path]})
            ack_ms = now_ms() - start
            if not acked.ok:
                self.fail(arm, rep, case, acked.error)
                continue
            states: list[str] = []
            final = ""
            deadline = time.monotonic() + 120 * self.slow
            while time.monotonic() < deadline:
                status = mcp.call("media", {"operation": "status", "path": path})
                state = ""
                if status.ok:
                    results = status.structured.get("results") or [{}]
                    state = str((results[0].get("processing") or {}).get("state", ""))
                if not states or states[-1] != state:
                    states.append(state)
                final = state
                if state and state not in ("pending", "running", "queued", "processing", "blocked", "unknown"):
                    break
                time.sleep(0.25)
            total = now_ms() - start
            self.record(arm, rep, case, [total], ok=final.startswith(("complete", "done")),
                        note=f"ack_ms={ack_ms:.0f} states={states} first_ack={json.dumps(acked.structured)[:200]}")

    def fs_micro(self, arm: str, rep: int, volume: str) -> None:
        """Small-file I/O on the volume itself; a microbenchmark of the storage path, not a product workload."""
        wall, result = self.oneshot(arm, volume, ["python", "-c", FS_SCRIPT, "/data/fsbench"], entrypoint="env")
        if result.returncode != 0:
            self.fail(arm, rep, "f.volume_io", result.stdout[-300:] + result.stderr[-500:])
            return
        for case, value in json.loads(result.stdout.strip().splitlines()[-1]).items():
            self.record(arm, rep, f"f.{case}", [value], items=200 if case.endswith("_200") else 1)

    # -- (c)
    def import_stage(self, arm: str, rep: int, volume: str) -> str:
        wall, result = self.oneshot(
            arm, volume, ["python", "-c", IMPORT_SCRIPT, IMPORT_NAME], entrypoint="env",
            mounts=[f"{self.fx / 'import' / IMPORT_NAME}:/input:ro"],
            env={"EXOMEM_IMPORT_MAX_MEMBERS": "500000", "EXOMEM_IMPORT_RESERVE_BYTES": "268435456"})
        if result.returncode != 0:
            raise HarnessError(result.stdout[-400:] + result.stderr[-900:])
        report = json.loads(result.stdout.strip().splitlines()[-1])
        files = self.import_counts["files"]
        self.record(arm, rep, "c.import_stage_job_run_to_exit", [wall], items=files)
        self.record(arm, rep, "c.import_stage_run_import", [report["run_import_ms"]], items=files,
                    note=f"prepare_ms={report['prepare_ms']:.0f} encrypt_ms={report['encrypt_ms']:.0f}")
        return report["import_id"]

    def work_import_cell(self, arm: str, rep: int, mcp: Mcp, import_id: str | None) -> None:
        if import_id is None:
            raise HarnessError("no staged import to adopt")
        stage = f"_Imports/{import_id}"
        steps: dict[str, float] = {}
        trace: list[dict] = []

        def step(label: str, arguments: dict) -> Call:
            answer = mcp.call("import", arguments)
            trace.append({"step": label, "ok": answer.ok, "ms": round(answer.ms), "error": answer.error[:300],
                          "keys": sorted(answer.structured)[:12] if isinstance(answer.structured, dict) else None})
            steps[label] = steps.get(label, 0.0) + answer.ms
            if not answer.ok:
                raise HarnessError(f"{label}: {answer.error[:300]}")
            return answer

        total_start = now_ms()
        try:
            started = step("start", {"action": "start", "path": stage})
            run_id = started.structured.get("run_id") or (started.structured.get("result") or {}).get("run_id")
            self.diag(f"import-start-{arm}-{rep}", started.structured)
            if not run_id:
                raise HarnessError("start returned no run_id")
            step("select", {"action": "select", "run_id": run_id, "include": [f"{stage}/{IMPORT_NAME}"]})
            planned = step("plan", {"action": "plan", "run_id": run_id})
            self.diag(f"import-plan-{arm}-{rep}", planned.structured)
            plan_id = planned.structured.get("plan_id")
            applied = step("apply", {"action": "apply", "run_id": run_id, "plan_id": plan_id})
            guard = 0
            while applied.structured.get("continuation") and guard < 200:
                applied = step("apply", {"action": "apply", "run_id": run_id, "plan_id": plan_id,
                                         "continuation": applied.structured["continuation"]})
                guard += 1
            self.diag(f"import-apply-{arm}-{rep}", applied.structured)
            step("finish", {"action": "finish", "run_id": run_id})
            ok = True
        except HarnessError as error:
            ok = False
            self.log(f"import trace {trace}")
            self.fail(arm, rep, "c.import_adopt_in_cell", str(error))
        total = now_ms() - total_start
        if ok:
            self.record(arm, rep, "c.import_adopt_in_cell", [total], items=self.import_counts["files"],
                        note=json.dumps({k: round(v) for k, v in steps.items()}))
        self.diag(f"import-trace-{arm}-{rep}", trace)

    # -- syscall recording (runc only)
    def container_pids(self, name: str) -> list[int]:
        """Host process ids in the container's cgroup, leader first."""
        leader = int(self.docker("inspect", "--format", "{{.State.Pid}}", name).stdout.strip())
        group = next(line[3:] for line in Path(f"/proc/{leader}/cgroup").read_text().splitlines() if line.startswith("0::"))
        members = [int(item) for item in Path(f"/sys/fs/cgroup{group}/cgroup.procs").read_text().split()]
        return [leader] + [pid for pid in members if pid != leader]

    def attach_and_release(self, name: str, out: Path) -> syscalls.Tracer:
        """Attach strace to a gated container's first process, then open the gate."""
        deadline = time.monotonic() + 90
        while self.docker("exec", name, "test", "-p", "/tmp/go", check=False, timeout=30).returncode != 0:
            if time.monotonic() > deadline:
                raise HarnessError(f"{name} never reached its gate")
            time.sleep(0.1)
        leader = int(self.docker("inspect", "--format", "{{.State.Pid}}", name).stdout.strip())
        tracer = syscalls.Tracer(out, [leader])
        if not tracer.wait_attached(leader):
            raise HarnessError(f"strace did not attach to {name}")
        self.docker("exec", name, "sh", "-c", ": > /tmp/go", timeout=30)
        return tracer

    def trace_phase(self, name: str, out: Path) -> syscalls.Tracer:
        pids = self.container_pids(name)
        tracer = syscalls.Tracer(out, pids)
        if not tracer.wait_attached(pids[0]):
            self.log(f"strace did not attach to {pids[0]} for {out.name}")
        return tracer

    def traced_oneshot(self, role: str, volume: str, command: list[str], out: Path, *, mounts: list[str] | None = None,
                       env: dict[str, str] | None = None, extra: list[str] | None = None) -> tuple[dict, int, str]:
        """One role as a container that strace follows from its first instruction to its exit."""
        name = f"kv-trace-{role}-{self.run_id}"
        argv = ["run", "-d", "--name", name, *self.oneshot_options("runc", volume, mounts=mounts, env=env, limits=True, extra=extra),
                "--entrypoint", "sh", self.image, "-c", TRACE_GATE, "tracee", *command]
        self.docker(*argv)
        try:
            tracer = self.attach_and_release(name, out / f"{role}.strace")
            code = int(self.docker("wait", name, timeout=1800).stdout.strip() or "-1")
            table = tracer.finish(timeout=300)
            logs = self.docker("logs", name, check=False)
            (self.out / "logs" / f"trace-{role}.log").write_text((logs.stdout + logs.stderr)[-20000:])
            return table, code, logs.stdout
        finally:
            self.docker("rm", "-f", name, check=False)

    def error_probes(self, url: str) -> None:
        """Requests the cell refuses, so that its error paths run once."""
        headers = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
        hello = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        good = {**headers, "authorization": f"Bearer {self.token}", "mcp-protocol-version": PROTOCOL}
        with httpx.Client(base_url=url, timeout=60, trust_env=False) as client:
            client.post("/mcp", headers=headers, json=hello)
            client.post("/mcp", headers={**headers, "authorization": "Bearer wrong"}, json=hello)
            client.post("/mcp", headers=good, content=b"{not json")
            client.post("/mcp", headers=good, json={"jsonrpc": "2.0", "id": 2, "method": "nonsense", "params": {}})
            client.get("/nonexistent")
        mcp = Mcp(url, self.token)
        try:
            mcp.initialize()
            mcp.raw("no_such_tool", {})
            mcp.call("read", {"path": "Knowledge Base/missing/absent.md"})
            mcp.call("search", {"query": ""})
            mcp.call("write", {"content": "", "title": ""})
            mcp.call("edit", {"path": "missing.md", "why": "probe", "old_string": "a", "new_string": "b"})
            mcp.call("media", {"operation": "status", "path": f"{MEDIA_DIR}/absent.pdf"})
        finally:
            mcp.close()

    def trace_serve(self, volume: str, import_id: str | None, out: Path) -> dict[str, dict]:
        """The serving cell under strace, one table per phase of the workload."""
        name = f"kv-trace-serve-{self.run_id}"
        arm, rep, phases = "runc-trace", 0, {}
        url, _, _ = self.start_cell("runc", name, volume, gated=True)
        mcp = None
        try:
            tracer = self.attach_and_release(name, out / "serve-startup.strace")
            ready = self.wait_http(url, "/health/ready", now_ms(), 900, name)
            if ready is None:
                phases["startup"] = tracer.stop()
                raise HarnessError("the traced cell never became ready")
            mcp = Mcp(url, self.token)
            mcp.initialize()
            phases["startup"] = tracer.stop()
            steps = (
                ("search", lambda: self.work_search(arm, rep, url, mcp)),
                ("write", lambda: self.work_write(arm, rep, mcp)),
                ("media", lambda: self.work_media(arm, rep, mcp, name)),
                ("import", lambda: self.work_import_cell(arm, rep, mcp, import_id)),
                ("errors", lambda: self.error_probes(url)),
            )
            for label, work in steps:
                tracer = self.trace_phase(name, out / f"serve-{label}.strace")
                try:
                    work()
                except Exception as error:  # noqa: BLE001 - recorded, the next phase goes on
                    self.fail(arm, rep, f"section.{label}", f"{type(error).__name__}: {error}")
                finally:
                    phases[label] = tracer.stop()
            mcp.close()
            mcp = None
            tracer = self.trace_phase(name, out / "serve-shutdown.strace")
            self.docker("stop", "-t", "30", name, check=False, timeout=180)
            phases["shutdown"] = tracer.finish(timeout=180)
        finally:
            if mcp is not None:
                mcp.close()
            self.stop_cell(name, "trace-serve")
        return phases

    def trace_session(self) -> None:
        """Record the syscalls of each process role under runc: cell-init, import Job, restic backup Job, serve."""
        out = self.out / "syscalls"
        out.mkdir(exist_ok=True)
        volume = f"kv-trace-{self.run_id}"
        self.log("=== trace session (runc, strace -f -qq -c)")
        self.new_volume(volume)
        self.helper(volume, "tar -C /data -xpf /snap/seed.tar && chmod 2770 /data", mounts=[f"{self.snap}:/snap:ro"])
        self.slow = 6
        meta = {"strace": sh(["strace", "--version"], check=False).stdout.splitlines()[:1], "image": self.image}
        documents, import_id = [], None
        try:
            roles = (
                ("cell-init", ["env", "exomem", "cell-init", "--vault", "/data/vault", "--json"], {}),
                ("import", ["env", "python", "-c", IMPORT_SCRIPT, IMPORT_NAME],
                 {"mounts": [f"{self.fx / 'import' / IMPORT_NAME}:/input:ro"],
                  "env": {"EXOMEM_IMPORT_MAX_MEMBERS": "500000", "EXOMEM_IMPORT_RESERVE_BYTES": "268435456"}}),
                ("restic", ["sh", "-c", RESTIC_SCRIPT],
                 {"extra": ["--tmpfs", "/repo:rw,size=512m,uid=10001,gid=10001,mode=0700"],
                  "env": {"RESTIC_REPOSITORY": "/repo", "RESTIC_PASSWORD": "trace-only", "RESTIC_CACHE_DIR": "/tmp/restic-cache"}}),
            )
            for role, command, options in roles:
                try:
                    table, code, stdout = self.traced_oneshot(role, volume, command, out, **options)
                except Exception as error:  # noqa: BLE001 - recorded, the next role goes on
                    self.fail("runc-trace", 0, f"trace.{role}", f"{type(error).__name__}: {error}")
                    continue
                self.log(f"trace {role}: exit={code} distinct={len(table)}")
                documents.append(syscalls.role_document(role, {"run": table}, exit_code=code, **meta))
                if role == "import" and code == 0:
                    import_id = json.loads(stdout.strip().splitlines()[-1])["import_id"]
            try:
                phases = self.trace_serve(volume, import_id, out)
                documents.append(syscalls.role_document("serve", phases, **meta))
            except Exception as error:  # noqa: BLE001 - recorded, the documents written so far stay
                self.fail("runc-trace", 0, "trace.serve", f"{type(error).__name__}: {error}")
        finally:
            for document in documents:
                (out / f"{document['role']}.json").write_text(json.dumps(document, indent=1))
            (out / "summary.json").write_text(json.dumps(syscalls.roles_summary(documents), indent=1))
            self.docker("volume", "rm", "-f", volume, check=False)

    # -- driver
    def run(self) -> None:
        self.docker("network", "create", "--driver", "bridge", "--internal", self.network)
        try:
            self.environment()
            self.kata_facts()
            self.compat()
            self.cgroup_probe()
            if self.args.compat_only:
                return
            self.make_fixtures()
            self.seed()
            if "measure" in self.args.phases:
                for rep in range(1, self.args.repeats + 1):
                    # Each arm takes each position in the order once over a full cycle of repeats.
                    shift = (rep - 1) % len(self.arms)
                    for arm in self.arms[shift:] + self.arms[:shift]:
                        self.session(arm, rep)
            if "trace" in self.args.phases:
                self.trace_session()
        finally:
            self.docker("network", "rm", self.network, check=False)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--work", required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--seed-notes", type=int, default=200)
    parser.add_argument("--only", default="search,write,media,import,fs")
    parser.add_argument("--arms", default=",".join(RUNTIMES), help="runtimes measured against each other")
    parser.add_argument("--phases", default="measure", help="measure, trace, or both: trace records runc syscalls")
    parser.add_argument("--compat-only", action="store_true", help="probe the runtimes and stop")
    args = parser.parse_args()
    args.only = {part for part in args.only.split(",") if part}
    args.phases = {part for part in args.phases.split(",") if part}
    lab = Lab(args)
    try:
        lab.run()
    except Exception as error:  # noqa: BLE001 - the artifact must still upload
        lab.log(f"RUN FAILED {type(error).__name__}: {error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
