#!/usr/bin/env python3
"""Measure the Exomem Cloud cell image under runc and under runsc (gVisor).

Diagnostic harness for the `diag/gvisor-overhead` branch; it ships nothing. It
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

CELL_ID = "abcdefghijklmnop"
CELL_PORT = 8765
PROTOCOL = "2025-11-25"
IMPORT_NAME = "Project Archive"
RUNTIMES = ("runc", "runsc", "runsc-nodirectfs", "runsc-ptrace", "runsc-debug", "runsc-overlay")
# The runtime that runs the long-lived cell in each arm. Plain runsc cannot start the cell on a
# volume: the sandbox refuses renameat2(RENAME_NOREPLACE) on its gofer-backed mounts, and the
# cell's filesystem probe then fails closed. `runsc-overlay` keeps the volume as a read-only
# lower layer and holds every write in sandbox memory, so the cell runs but its writes do not persist.
CELL_RUNTIME = {"runc": "runc", "runsc": "runsc-overlay"}
MEDIA_DIR = "Knowledge Base/Sources/Media"
CELL_COMMAND = (
    'ulimit -c 0 && mkdir -p /data/scratch/tmp /data/scratch/cache /data/scratch/export && exec "$@"'
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


def now_ms() -> float:
    return time.perf_counter() * 1000.0


def sh(argv: list[str], *, check: bool = True, timeout: float | None = 900, input_text: str | None = None):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, input=input_text, check=False)
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
                       "clientInfo": {"name": "gvisor-overhead", "version": "1"}}})
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


report = {"kernel": os.uname().release, "roots": {}}
for root in sys.argv[1:]:
    entry = {}
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    for label, resolve in (("none", 0), ("no_xdev", 1), ("no_magiclinks", 2), ("no_symlinks", 4),
                           ("beneath", 8), ("all_four", 15)):
        entry["openat2_" + label] = openat2(fd, resolve)
    entry["renameat2_noreplace"] = renameat2(fd)
    os.close(fd)
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
        self.network = f"gv-net-{self.run_id}"
        self.token = secrets.token_urlsafe(32)
        self.fx = self.work / "fx"
        self.snap = self.work / "snap"
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / "logs").mkdir(exist_ok=True)
        self.results = (self.out / "results.jsonl").open("a", encoding="utf-8")
        self.events = (self.out / "events.log").open("a", encoding="utf-8")
        self.stems: list[str] = []
        self.import_counts: dict = {}
        self.media_files: list[str] = []

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

    def oneshot(self, runtime: str, volume: str, entrypoint_args: list[str], *, entrypoint: str | None = None,
                mounts: list[str] | None = None, env: dict[str, str] | None = None, timeout: float = 600):
        argv = ["run", "--rm", "--runtime", runtime, "--network", "none", *self.security(), "-v", f"{volume}:/data"]
        for mount in mounts or []:
            argv += ["-v", mount]
        for key, value in {"EXOMEM_CLOUD_CELL": "1", "EXOMEM_CLOUD_CELL_ID": CELL_ID, **(env or {})}.items():
            argv += ["-e", f"{key}={value}"]
        if entrypoint:
            argv += ["--entrypoint", entrypoint]
        argv += [self.image, *entrypoint_args]
        start = now_ms()
        result = sh(["docker", *argv], check=False, timeout=timeout)
        return now_ms() - start, result

    def new_volume(self, name: str) -> None:
        self.docker("volume", "create", name)
        self.helper(name, "chown 10001:10001 /data && chmod 2770 /data")

    def start_cell(self, runtime: str, name: str, volume: str) -> tuple[str, float, float]:
        """Returns (base url, `docker run -d` milliseconds, clock at the call)."""
        argv = ["run", "-d", "--name", name, "--runtime", runtime, "--network", self.network,
                "--cpus", "2", "--memory", "3g", *self.security(), "-v", f"{volume}:/data", *self.cell_env(),
                "--entrypoint", "sh", self.image, "-c", CELL_COMMAND, "cell-process", "exomem",
                "--transport", "http", "--host", "0.0.0.0", "--port", str(CELL_PORT)]
        t0 = now_ms()
        self.docker(*argv)
        created = now_ms() - t0
        info = self.docker("inspect", "--format", "{{json .NetworkSettings.Networks}}", name)
        address = json.loads(info.stdout)[self.network]["IPAddress"]
        return f"http://{address}:{CELL_PORT}", created, t0

    def wait_http(self, url: str, path: str, t0: float, timeout: float) -> float | None:
        deadline = time.monotonic() + timeout
        with httpx.Client(timeout=2.0, trust_env=False) as client:
            while time.monotonic() < deadline:
                try:
                    if client.get(url + path).status_code == 200:
                        return now_ms() - t0
                except httpx.HTTPError:
                    pass
                time.sleep(0.05)
        return None

    def stop_cell(self, name: str, tag: str) -> dict:
        facts: dict = {}
        stats = self.docker("stats", "--no-stream", "--format", "{{json .}}", name, check=False)
        facts["stats"] = stats.stdout.strip()
        state = self.docker("inspect", "--format", "{{json .State}}", name, check=False)
        facts["state"] = state.stdout.strip()
        logs = self.docker("logs", "--tail", "400", name, check=False)
        (self.out / "logs" / f"{tag}.log").write_text((logs.stdout + logs.stderr).replace(self.token, "<token>"))
        self.docker("stop", "-t", "15", name, check=False)
        self.docker("rm", "-f", name, check=False)
        return facts

    # -- environment facts
    def environment(self) -> None:
        facts: dict = {}
        for key, argv in {
            "nproc": ["nproc"], "uname": ["uname", "-a"], "free": ["free", "-m"],
            "lscpu": ["lscpu"], "docker_version": ["docker", "version", "--format", "{{json .}}"],
            "docker_info": ["docker", "info", "--format", "{{json .}}"],
            "runsc_version": ["runsc", "--version"], "kvm": ["ls", "-l", "/dev/kvm"],
            "daemon_json": ["cat", "/etc/docker/daemon.json"],
            "image": ["docker", "image", "inspect", self.image, "--format",
                      "{{.Id}} {{json .RepoDigests}} {{.Os}}/{{.Architecture}} size={{.Size}}"],
            "os_release": ["cat", "/etc/os-release"],
        }.items():
            result = sh(argv, check=False, timeout=60)
            facts[key] = (result.stdout + result.stderr).strip()[:6000]
        self.diag("environment", facts)

    def runsc_facts(self) -> None:
        """The sandbox's own view: kernel string, and the runsc command line with its platform and flags."""
        name = f"gv-facts-{self.run_id}"
        self.docker("run", "-d", "--name", name, "--runtime", "runsc", "--entrypoint", "sleep", self.image, "120")
        facts: dict = {}
        for key, argv in {
            "uname": ["docker", "exec", name, "uname", "-a"],
            "proc_version": ["docker", "exec", name, "cat", "/proc/version"],
            "dmesg": ["docker", "exec", "-u", "0", name, "dmesg"],
            "host_runsc_processes": ["sh", "-c", "ps -eo args | grep -E 'runsc|boot' | grep -v grep | cut -c1-1500"],
        }.items():
            result = sh(argv, check=False, timeout=60)
            facts[key] = (result.stdout + result.stderr).strip()[:6000]
        self.docker("rm", "-f", name, check=False)
        self.diag("runsc_facts", facts)

    # -- compatibility
    def compat(self) -> None:
        """Which kernel primitives the cell's filesystem layer needs, per runtime and root; unmeasured."""
        volume = f"gv-compat-{self.run_id}"
        self.new_volume(volume)
        try:
            for runtime in RUNTIMES:
                wall, probed = self.oneshot(runtime, volume, ["python", "-c", PROBE_SCRIPT, "/data", "/tmp"],
                                            entrypoint="env", timeout=300)
                self.log(f"compat {runtime} rc={probed.returncode} {wall:.0f} ms {probed.stdout.strip()[:1200]}"
                         f"{probed.stderr.strip()[-300:]}")
                self.diag(f"compat-{runtime}", {"rc": probed.returncode, "stdout": probed.stdout,
                                                "stderr": probed.stderr[-2000:]})
        finally:
            self.docker("volume", "rm", "-f", volume, check=False)
        # The sandbox's own log names every syscall it refused; the debug runtime wrote it to the host.
        pattern = "unsupported syscall|ENOSYS|not implemented|not supported"
        listing = sh(["sudo", "sh", "-c", f"ls -l /tmp/runsc-debug | head -20; "
                      f"grep -rhiE '{pattern}' /tmp/runsc-debug | cut -c1-300 | sort | uniq -c | sort -rn | head -60"],
                     check=False, timeout=120)
        self.diag("runsc_debug_unsupported", listing.stdout + listing.stderr)

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
        volume = f"gv-seed-{self.run_id}"
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
        name = f"gv-seedcell-{self.run_id}"
        url, _, t0 = self.start_cell("runc", name, volume)
        try:
            ready = self.wait_http(url, "/health/ready", t0, 300)
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
        deadline = time.monotonic() + limit_s
        while True:
            answer = mcp.call("search", {"query": query})
            warming = answer.code == "RETRIEVAL_INDEX_WARMING" or "embeddings" in warming_components(answer.structured)
            if not warming or time.monotonic() > deadline:
                return answer
            time.sleep(0.5)

    # -- one session
    def session(self, arm: str, rep: int) -> None:
        tag = f"{arm}-r{rep}"
        volume = f"gv-{arm}-{rep}-{self.run_id}"
        name = f"gv-cell-{arm}-{rep}-{self.run_id}"
        self.log(f"=== session {tag}")
        self.new_volume(volume)
        self.helper(volume, "tar -C /data -xpf /snap/seed.tar && chmod 2770 /data",
                    mounts=[f"{self.snap}:/snap:ro"])
        if "media" in self.args.only:
            # Staged before the cell starts: an overlay over the volume would not see a later host-side copy.
            self.helper(volume, f"mkdir -p '/data/vault/{MEDIA_DIR}' && cp /media/* '/data/vault/{MEDIA_DIR}/'",
                        mounts=[f"{self.fx / 'media'}:/media:ro"], user="10001:10001")
        try:
            self.session_body(arm, rep, tag, volume, name)
        finally:
            self.docker("rm", "-f", name, check=False)
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
        # (e) startup
        url, created, t0 = self.start_cell(CELL_RUNTIME[arm], name, volume)
        live = self.wait_http(url, "/health", t0, 240)
        ready = self.wait_http(url, "/health/ready", t0, 400)
        self.record(arm, rep, "e.docker_run_returns", [created])
        for case, value in (("e.start_to_live", live), ("e.start_to_ready", ready)):
            if value is None:
                self.fail(arm, rep, case, "never answered 200")
            else:
                self.record(arm, rep, case, [value])
        if ready is None:
            self.stop_cell(name, f"{tag}-cell")
            return
        mcp = Mcp(url, self.token)
        try:
            mcp.initialize()
            sections = (
                ("search", lambda: self.work_search(arm, rep, url, mcp)),
                ("write", lambda: self.work_write(arm, rep, mcp)),
                ("media", lambda: self.work_media(arm, rep, mcp, volume)),
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
            deadline = time.monotonic() + 120
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
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                answer = mcp.call("search", {"query": marker})
                if answer.ok and hits_of(answer.structured):
                    values.append(now_ms() - start)
                    break
                time.sleep(0.1)
        self.record(arm, rep, "b.write_to_searchable", values, ok=bool(values))

    # -- (d)
    def work_media(self, arm: str, rep: int, mcp: Mcp, volume: str) -> None:
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
            deadline = time.monotonic() + 300
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

    # -- driver
    def run(self) -> None:
        self.docker("network", "create", "--driver", "bridge", "--internal", self.network)
        try:
            self.environment()
            self.runsc_facts()
            self.compat()
            if self.args.compat_only:
                return
            self.make_fixtures()
            self.seed()
            for rep in range(1, self.args.repeats + 1):
                order = ("runc", "runsc") if rep % 2 else ("runsc", "runc")
                for arm in order:
                    self.session(arm, rep)
        finally:
            self.docker("network", "rm", self.network, check=False)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--work", required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--seed-notes", type=int, default=200)
    parser.add_argument("--only", default="search,write,media,import")
    parser.add_argument("--compat-only", action="store_true", help="probe the runtimes and stop")
    args = parser.parse_args()
    args.only = {part for part in args.only.split(",") if part}
    lab = Lab(args)
    try:
        lab.run()
    except Exception as error:  # noqa: BLE001 - the artifact must still upload
        lab.log(f"RUN FAILED {type(error).__name__}: {error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
