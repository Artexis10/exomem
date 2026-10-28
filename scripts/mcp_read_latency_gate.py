"""Paired MCP read latency gate: this checkout against the latest release.

`paired` times one shard of the generated public fixture on both arms and writes a
fragment. `aggregate` joins the fragments into per-group verdicts. `private` times
a prepared private vault the same way and also reports each store group against its
p95 target. Both arms run the real managed worker and every call is timed by
`recall_canary.measure_call`; nothing in this file times a call.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import secrets
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from importlib.resources import files
from pathlib import Path
from typing import Any

from release_arm import MeasurementRefused, extract_release, latest_release_tag, prepare_tag_runtime

from exomem import latency_watch
from exomem.request_latency_contract import load_contract

ROOT = Path(__file__).resolve().parents[1]
BENCHMARK = ROOT / "benchmarks/mcp-read-latency"
SCHEMA_VERSION = 1

CONTRACT = load_contract()
REFERENCE_ID = "mcp-read-paired"
REFERENCE = CONTRACT.references[REFERENCE_ID]
BOUNDARY = CONTRACT.bar("mcp-read-paired-pending").boundary.id
#: The chosen verdict for a group whose interval has no allowance to meet.
NO_ALLOWANCE = "allowance-not-set"


# --- Statistics and verdicts -------------------------------------------------


def percentile(values: list[float], q: float) -> float | None:
    # The live watcher's nearest-rank definition, so "meets target" here means what its p95 bar means.
    return latency_watch.nearest_rank(sorted(values), q) if values else None


def group_interval(medians: list[float], *, confidence: float, family: int) -> dict[str, Any]:
    """Student-t interval of the mean case difference, widened for the family of groups.

    Each case contributes one median of its (head - base) differences, so the cases are
    the independent units. `family` is the number of gating groups the contract declares.
    """
    from scipy.stats import t

    count = len(medians)
    mean = statistics.fmean(medians)
    half = float(t.ppf(1 - (1 - confidence) / (2 * family), count - 1)) * (
        statistics.stdev(medians) / math.sqrt(count)
    )
    return {"cases": count, "mean_ms": mean, "lower_ms": mean - half, "upper_ms": mean + half}


def judge(interval: dict[str, Any], allowance_ms: float | None) -> str:
    """Fail on the lower bound, demonstrate on the upper bound, otherwise stay inconclusive."""
    if allowance_ms is None:
        return NO_ALLOWANCE
    if interval["lower_ms"] > allowance_ms:
        return "failed"
    if interval["upper_ms"] <= allowance_ms:
        return "demonstrated"
    return "inconclusive"


def cpu_pressure(path: str = "/proc/pressure/cpu") -> float | None:
    """The kernel's 60-second share of time some task waited for a CPU; None when unreadable."""
    try:
        line = next(row for row in Path(path).read_text().splitlines() if row.startswith("some "))
        return float(dict(field.split("=") for field in line.split()[1:])["avg60"])
    except (OSError, StopIteration, KeyError, ValueError):
        return None


# --- Refusals ----------------------------------------------------------------


def state_keys(state_root: Path) -> frozenset[str]:
    """The per-vault state directories under an arm's state root."""
    if not state_root.is_dir():
        return frozenset()
    return frozenset(entry.name for entry in state_root.iterdir() if entry.is_dir())


def require_warm_state(before: frozenset[str], after: frozenset[str]) -> None:
    """Refuse an arm whose worker created vault state; a cold start times index builds.

    State is keyed by the vault path, so a copied vault, or any state the worker made
    for itself, appears as a directory that was not there before the worker started.
    """
    if not before or after - before:
        raise MeasurementRefused("arm-state-created-during-run", phase="state-check")


def require_derived_ready(state_root: Path) -> None:
    """Refuse an arm with a derived collection still rebuilding.

    The store retries a rebuilding collection every 2 s under the writer lease, which
    puts a tail on unrelated calls. The derived file allows only 'ready' and
    'rebuilding', so "not ready" is the product's own `rebuilds_pending`; its argument
    is a store path and resolves to the derived file beside it, which is the file itself.
    """
    from exomem.collection_store import derived_rows

    for derived in sorted(state_root.glob(f"*/{derived_rows.FILENAME}")):
        if derived_rows.rebuilds_pending(derived):
            raise MeasurementRefused("derived-collection-not-ready", phase="state-check")


# --- Fragments and the family verdict -----------------------------------------

#: Fields every fragment of one run must agree on.
CONSISTENT = ("contract_revision", "contract_sha256", "profile_sha256", "cases_sha256", "boundary",
              "tag", "base_commit", "shards", "case_total", "reduced")


def contract_sha256() -> str:
    return hashlib.sha256(files("exomem").joinpath("request_latency_contract.json").read_bytes()).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pair_samples(samples: list[dict], assigned: list[dict], *, repeats: int):
    """Pair each assigned case's base and head samples by repeat.

    Returns the per-group case medians of (head - base), the per-group arm times and
    the broken-instrument problems. A case with a failed or absent sample is a problem
    and contributes nothing, so a failure can never shift a group's interval.
    """
    by_key = {(sample["case"], sample["arm"], sample["repeat"]): sample for sample in samples}
    medians: dict[str, list[float]] = defaultdict(list)
    arm_ms: dict[str, dict[str, list[float]]] = defaultdict(lambda: {"base": [], "head": []})
    problems = []
    for case in assigned:
        pairs = [(by_key.get((case["id"], "base", repeat)), by_key.get((case["id"], "head", repeat)))
                 for repeat in range(repeats)]
        if any(base is None or head is None for base, head in pairs):
            problems.append({"code": "pair-missing", "group": case["group"], "case": case["id"]})
        elif any(base["failure"] or head["failure"] for base, head in pairs):
            problems.append({"code": "case-failed", "group": case["group"], "case": case["id"]})
        else:
            medians[case["group"]].append(statistics.median(h["elapsed_ms"] - b["elapsed_ms"] for b, h in pairs))
            for base, head in pairs:
                arm_ms[case["group"]]["base"].append(base["elapsed_ms"])
                arm_ms[case["group"]]["head"].append(head["elapsed_ms"])
    return medians, arm_ms, problems


def fragment_problems(fragments: list[dict], *, head_contract_sha256: str,
                      head_profile_sha256: str | None) -> list[dict]:
    """Check that the fragments are one run: same contract, fixture, cases, release and shard set."""
    first = fragments[0]
    problems = [{"code": "fragment-differs", "field": key} for key in CONSISTENT
                if any(fragment[key] != first[key] for fragment in fragments)]
    if first["contract_sha256"] != head_contract_sha256:
        problems.append({"code": "fragment-differs", "field": "contract_sha256"})
    if head_profile_sha256 is not None and first["profile_sha256"] != head_profile_sha256:
        problems.append({"code": "fragment-differs", "field": "profile_sha256"})
    if sorted(fragment["shard"] for fragment in fragments) != list(range(first["shards"])):
        problems.append({"code": "shard-missing"})
    ids = [case["id"] for fragment in fragments for case in fragment["assigned"]]
    if len(ids) != len(set(ids)) or len(ids) != first["case_total"]:
        problems.append({"code": "case-assignment-inconsistent"})
    return problems


def group_row(group: str, setting: dict, *, gating: bool, medians: list[float], arm_ms: dict,
              reference: dict, reduced: bool, private: bool) -> dict[str, Any]:
    row: dict[str, Any] = {"group": group, "gating": gating, "cases": len(medians)}
    if len(medians) >= 2:
        row.update(group_interval(medians, confidence=reference["confidence"],
                                  family=len(reference["gating_groups"])))
    for arm in ("base", "head"):
        row[f"{arm}_p50_ms"], row[f"{arm}_p95_ms"] = (percentile(arm_ms[arm], 50), percentile(arm_ms[arm], 95))
    if not gating:
        row["verdict"] = "reported"
    elif len(medians) < 2 or (len(medians) < reference["minimum_cases"] and not reduced):
        row["verdict"] = "insufficient-cases"
    else:
        row["allowance_ms"] = setting["allowance_ms"]
        row["verdict"] = judge(row, setting["allowance_ms"])
    if private and "p95_target_ms" in setting and row["head_p95_ms"] is not None:
        row["target"] = {"p95_target_ms": setting["p95_target_ms"],
                         "result": "meets target" if row["head_p95_ms"] <= setting["p95_target_ms"]
                         else "misses target"}
    return row


def summarize(fragments: list[dict], *, mode: str, reference: dict = REFERENCE,
              head_contract_sha256: str | None = None,
              head_profile_sha256: str | None = None) -> dict[str, Any]:
    """Join the fragments of one run into per-group verdicts and the run's state."""
    refused = [fragment for fragment in fragments if fragment["state"] != "measured"]
    if refused:
        return {"schema_version": SCHEMA_VERSION, "mode": mode, "state": "refused",
                "failures": [{"code": f["reason"], "phase": f["phase"]} for f in refused]}
    first = fragments[0]
    summary: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION, "mode": mode, "contract_revision": first["contract_revision"],
        "contract_sha256": first["contract_sha256"], "profile_sha256": first["profile_sha256"],
        "cases_sha256": first["cases_sha256"], "boundary": first["boundary"], "tag": first["tag"],
        "base_commit": first["base_commit"], "reduced": first["reduced"],
        "family": {"groups": len(reference["gating_groups"]), "confidence": reference["confidence"],
                   "multiplicity": reference["multiplicity"]},
        "environment": [{"shard": f["shard"], **f["environment"]} for f in fragments],
        "groups": [], "warnings": [], "failures": []}
    failures = fragment_problems(
        fragments, head_contract_sha256=head_contract_sha256 or contract_sha256(),
        head_profile_sha256=head_profile_sha256)
    samples = [sample for fragment in fragments for sample in fragment["samples"]]
    assigned = [case for fragment in fragments for case in fragment["assigned"]]
    medians, arm_ms, problems = pair_samples(samples, assigned, repeats=reference["repeats"])
    failures += problems
    settings = {**{name: (setting, True) for name, setting in reference["gating_groups"].items()},
                **{name: (setting, False) for name, setting in reference["non_gating_groups"].items()}}
    failures += [{"code": "group-undeclared", "group": g} for g in sorted({c["group"] for c in assigned} - settings.keys())]
    for name, (setting, gating) in settings.items():
        if not any(case["group"] == name for case in assigned):
            if gating:
                failures.append({"code": "group-missing", "group": name})
            continue
        summary["groups"].append(group_row(
            name, setting, gating=gating, medians=medians[name], arm_ms=arm_ms[name], reference=reference,
            reduced=first["reduced"], private=mode == "private"))
    for row in summary["groups"]:
        if row["verdict"] == "failed":
            failures.append({"code": "slower-than-allowance", "group": row["group"]})
        elif row["verdict"] == "insufficient-cases":
            failures.append({"code": "cases-below-floor", "group": row["group"]})
        elif row["verdict"] in ("inconclusive", NO_ALLOWANCE):
            summary["warnings"].append({"code": row["verdict"], "group": row["group"]})
    if first["reduced"]:
        summary["warnings"].append({"code": "reduced-run"})
    summary["failures"] = failures
    summary["state"] = "failed" if failures else "reduced" if first["reduced"] else "passed"
    return summary


def validate_document(document: dict) -> None:
    """Validate a stored plan, fragment or summary against the schema's definition for its mode."""
    from jsonschema import Draft202012Validator, FormatChecker

    shape = json.loads((BENCHMARK / "summary.schema.json").read_text())
    Draft202012Validator({"$schema": shape["$schema"], "$defs": shape["$defs"],
                          "$ref": f"#/$defs/{document['mode']}"}, format_checker=FormatChecker()).validate(document)


# --- Arms: one release, its interpreter and the real managed worker ------------

#: Wraps the managed worker so it can reach only its own coordinator and listener.
#: AF_UNIX addresses are strings, so the worker's private socket is never refused.
WORKER_GUARD = """
import os, runpy, sys
allowed = {int(port) for port in os.environ.pop("GATE_ALLOWED_PORTS").split(",") if port}
def audit(event, args):
    if event == "socket.connect" and isinstance(args[1], tuple) and (
            args[1][0] not in ("127.0.0.1", "::1") or args[1][1] not in allowed):
        raise PermissionError("the latency gate refuses external or unrelated connections")
sys.addaudithook(audit)
sys.argv = ["exomem.service_manager", *sys.argv[1:]]
runpy.run_module("exomem.service_manager", run_name="__main__", alter_sys=True)
"""

#: Issues the session the local ingress accepts, as the installed service does for its own clients.
SESSION_ISSUE = """
import asyncio
from exomem import local_ingress, server_auth
from exomem.auth_sessions import SessionIdentity
async def main():
    authority = server_auth.build_local_session_authority()
    secret, _ = await authority.issue(client_id="latency-gate", scopes=list(local_ingress.LOCAL_SCOPES),
                                      identity=SessionIdentity(github_user_id=42, github_login="gate-owner"))
    print(secret)
asyncio.run(main())
"""

INIT_VAULT = ("import sys; from pathlib import Path; from exomem import init; "
              "init.init_vault(Path(sys.argv[1]), force=sys.argv[2:] == ['overlay'])")
INIT_STATE = ("import sys; from pathlib import Path; from exomem import state_migration as m; "
              "Path(sys.argv[1]).mkdir(parents=True, exist_ok=True); m.migrate_vault_state_offline(Path(sys.argv[1]), "
              "authority=m.assert_offline_migration_authority(source='latency gate arm before seeding'))")

#: The runner's own settings never reach an arm: its keys, tokens and service settings stay out.
_HOST_PREFIXES = ("EXOMEM_", "KB_MCP_", "FASTMCP_", "GITHUB_")
_HOST_SUFFIXES = ("_API_KEY", "_ACCESS_TOKEN", "_TOKEN", "_SECRET")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Arm:
    """One release: its interpreter, a private root and the managed worker serving that root."""

    def __init__(self, name: str, python: Path | str, root: Path, socket_dir: Path, *,
                 worker_settings: dict[str, str]):
        self.name, self.python, self.root = name, str(python), root
        self.vault, self.state, self.lease, self.logs = root / "vault", root / "state", root / "lease", root / "logs"
        self.socket = socket_dir / f"{name}.sock"
        if len(str(self.socket)) > 100:
            raise MeasurementRefused("worker-socket-path-too-long", phase="arm-setup")
        for directory in (self.state, self.lease, self.logs, root / "tmp", root / "xdg", root / "config",
                          root / "data", root / "auth-home"):
            directory.mkdir(parents=True, exist_ok=True)
        self.public_port, self.coordinator_port = free_port(), free_port()
        self.coordinator_token, self.operator_token = secrets.token_hex(32), secrets.token_hex(32)
        self.ingress_key = secrets.token_urlsafe(32)
        self.worker_settings = worker_settings
        # One environment per arm: its tokens, ports and signing key must not change between steps.
        self.env = self.environment()
        self.worker: subprocess.Popen | None = None
        self.coordinator: subprocess.Popen | None = None
        self.client: Any = None
        self.state_before: frozenset[str] = frozenset()

    def environment(self) -> dict[str, str]:
        """Mirror the installed service's settings; every path is under this arm's root."""
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(_HOST_PREFIXES) and not key.endswith(_HOST_SUFFIXES)}
        root = self.root
        environment.update({
            "EXOMEM_VAULT_PATH": str(self.vault), "EXOMEM_STATE_ROOT": str(self.state),
            "EXOMEM_WRITER_LEASE_STATE_DIR": str(self.lease), "EXOMEM_LOG_DIR": str(self.logs),
            "EXOMEM_MODE": "quiet", "EXOMEM_AUTO_QUIET": "0", "EXOMEM_FAST_DURABLE_ACK": "1",
            "EXOMEM_DISABLE_WORKING_SET": "0", "EXOMEM_SEMANTIC_SEGMENTS": "1", "CUDA_VISIBLE_DEVICES": "",
            "EXOMEM_DISABLE_MEDIA_EXTRACTION": "1", "EXOMEM_DISABLE_FILE_WATCHER": "1",
            "EXOMEM_WRITER_LEASE_URL": f"http://127.0.0.1:{self.coordinator_port}",
            "EXOMEM_WRITER_LEASE_VAULT_ID": "latency-gate", "EXOMEM_WRITER_LEASE_REPLICA_ID": "gate-service",
            "EXOMEM_WRITER_LEASE_TOKEN": self.coordinator_token, "EXOMEM_WRITER_LEASE_TOPOLOGY": "single-host",
            "EXOMEM_LEASE_COORDINATOR_TOKEN": self.coordinator_token,
            "EXOMEM_LEASE_COORDINATOR_OPERATOR_TOKEN": self.operator_token,
            "FASTMCP_HOME": str(root / "auth-home"), "EXOMEM_BASE_URL": f"http://127.0.0.1:{self.public_port}",
            "GITHUB_CLIENT_ID": "gate-client", "GITHUB_CLIENT_SECRET": secrets.token_hex(32),
            "EXOMEM_GITHUB_USERNAME": "gate-owner", "EXOMEM_GITHUB_USER_ID": "42",
            "EXOMEM_JWT_SIGNING_KEY": secrets.token_hex(32), "TMPDIR": str(root / "tmp"),
            "XDG_STATE_HOME": str(root / "xdg"), "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_DATA_HOME": str(root / "data"), "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
            **self.worker_settings})
        return environment

    def run_python(self, code: str, *arguments: str) -> str:
        """Run a snippet in this arm's own interpreter and environment; raw output stays here."""
        try:
            done = subprocess.run([self.python, "-I", "-c", code, *arguments], env=self.env,
                                  cwd=self.root, capture_output=True, text=True, check=True, timeout=120)
        except (subprocess.SubprocessError, OSError) as error:
            raise MeasurementRefused("arm-command-failed", phase="arm-setup") from error
        return done.stdout.strip()

    def init_vault(self, *, overlay: bool = False) -> None:
        """Lay down the starter scaffold, with the state manifest only on an empty vault.

        `overlay` lays it over pages already written; a page at a scaffold path is replaced.
        """
        self.run_python(INIT_VAULT, str(self.vault), *(("overlay",) if overlay else ()))

    def init_state(self) -> None:
        """Write the empty vault's external state manifest, as `init_vault` does for a new vault."""
        self.run_python(INIT_STATE, str(self.vault))

    async def start(self, *, quiescence_limit: float) -> dict[str, float]:
        """Start the coordinator and the worker, join the worker as a local client and wait for quiet."""
        import httpx2 as httpx
        from fastmcp import Client
        from fastmcp.client.transports import StreamableHttpTransport

        environment = self.env
        self.state_before = state_keys(self.state)
        coordinator_log = (self.logs / "coordinator.log").open("ab")
        self.coordinator = subprocess.Popen(
            [self.python, "-I", "-m", "exomem.lease_coordinator", "--host", "127.0.0.1",
             "--port", str(self.coordinator_port), "--database", str(self.root / "coordinator.sqlite")],
            env=environment, stdout=coordinator_log, stderr=subprocess.STDOUT, cwd=self.root)
        await self._wait("coordinator-start", 30, lambda: self._port_open(self.coordinator_port, self.coordinator))
        worker_log = (self.logs / "worker.log").open("ab")
        self.worker = subprocess.Popen(
            [self.python, "-I", "-c", WORKER_GUARD, "worker", "--socket", str(self.socket),
             "--host", "127.0.0.1", "--port", str(self.public_port)],
            env={**environment, "EXOMEM_INTERNAL_INGRESS_KEY": self.ingress_key,
                 "GATE_ALLOWED_PORTS": f"{self.coordinator_port},{self.public_port}"},
            stdout=worker_log, stderr=subprocess.STDOUT, cwd=self.root)
        started = time.perf_counter()

        async def healthy() -> bool:
            if self.worker.poll() is not None:
                raise MeasurementRefused("worker-exited", phase="worker-start")
            if not self.socket.exists():
                return False
            try:
                async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=str(self.socket)),
                                             base_url="http://worker") as probe:
                    return (await probe.get("/health")).status_code < 500
            except httpx.HTTPError:
                return False

        await self._wait("worker-start", 600, healthy)
        ready = time.perf_counter() - started
        secret = self.run_python(SESSION_ISSUE).splitlines()[-1]
        headers = {"Authorization": f"Bearer {secret}", "x-exomem-internal-ingress": "local",
                   "x-exomem-internal-ingress-proof": self.ingress_key}
        socket_path = str(self.socket)

        def factory(headers=None, timeout=None, auth=None, **_):
            return httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=socket_path), headers=headers,
                                     timeout=timeout, auth=auth)

        self.client = Client(StreamableHttpTransport("http://127.0.0.1/mcp", headers=headers,
                                                     httpx_client_factory=factory), timeout=120)
        await self.client.__aenter__()
        progress(f"arm-ready arm={self.name} ready_s={ready:.0f}")
        quiescent = await self.quiesce(limit=quiescence_limit)
        progress(f"arm-quiet arm={self.name} quiescent_s={quiescent:.0f}")
        return {"ready_s": ready, "quiescent_s": quiescent}

    @staticmethod
    async def _port_open(port: int, process: subprocess.Popen) -> bool:
        if process.poll() is not None:
            raise MeasurementRefused("coordinator-exited", phase="coordinator-start")
        with socket.socket() as probe:
            return probe.connect_ex(("127.0.0.1", port)) == 0

    @staticmethod
    async def _wait(phase: str, limit: float, check) -> None:
        deadline = time.perf_counter() + limit
        while not await check():
            if time.perf_counter() > deadline:
                raise MeasurementRefused("startup-timeout", phase=phase)
            await asyncio.sleep(0.25)

    async def quiesce(self, *, limit: float, window: float = 10.0, share: float = 0.2) -> float:
        """Wait until the worker used under `share` of one CPU for `window` seconds.

        A worker that re-embeds or rebuilds after startup stays busy and is refused here.
        """
        def cpu_seconds() -> float:
            fields = Path(f"/proc/{self.worker.pid}/stat").read_text().rsplit(")", 1)[1].split()
            return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")

        started = time.perf_counter()
        history = [(started, cpu_seconds())]
        while time.perf_counter() - started < limit:
            await asyncio.sleep(1)
            now = time.perf_counter()
            history.append((now, cpu_seconds()))
            history = [entry for entry in history if now - entry[0] <= window + 0.5]
            span = history[-1][0] - history[0][0]
            if span >= window and (history[-1][1] - history[0][1]) / span < share:
                return now - started
        raise MeasurementRefused("arm-not-quiescent", phase="quiescence")

    def require_timeable(self) -> None:
        """The two refusals that make a started arm timeable."""
        require_warm_state(self.state_before, state_keys(self.state))
        require_derived_ready(self.state)

    def pause(self) -> None:
        self.worker.send_signal(signal.SIGSTOP)

    def resume(self) -> None:
        self.worker.send_signal(signal.SIGCONT)

    async def stop(self) -> None:
        if self.worker is not None and self.worker.poll() is None:
            self.resume()
        if self.client is not None:
            try:
                await self.client.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001 - the worker is being stopped anyway
                pass
            self.client = None
        for process in (self.worker, self.coordinator):
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()

    async def tool(self, name: str, arguments: dict) -> dict:
        """One unmeasured call through this arm's own tools; a refusal stops the run."""
        result = await self.client.call_tool(name, arguments, raise_on_error=False, timeout=120)
        data = result.structured_content
        if result.is_error or not isinstance(data, dict) or data.get("success") is False:
            # The product's error code goes to the log only; the summary keeps the runner's closed code.
            error = data.get("error", {}) if isinstance(data, dict) else {}
            print(f"{self.name} arm: {name} {arguments['operation']['kind']} failed with "
                  f"{error.get('code')}: {error.get('message')}", file=sys.stderr)
            raise MeasurementRefused("collection-load-failed", phase="fixture-load")
        return data

    async def load_collections(self, fixture: dict) -> None:
        """Create every collection and load its rows through this arm's own write tools.

        Records rows go in as chained bulk upserts, because one guarded append per row costs
        about a second of worker time. Planning has no bulk action, so it adds row by row.
        Each collection is read back and must hold exactly the rows the generator authored.
        """
        from exomem.records import BULK_UPSERT_MAX_ROWS
        from exomem.structured_collections import plan_ref

        why = "latency gate fixture"
        for kind, read_tool, write_tool in (("records", "read_records", "change_records"),
                                            ("planning", "read_plans", "change_plans")):
            for collection in fixture[kind]:
                path = collection["manifest_path"]
                created = await self.tool(write_tool, {"operation": {
                    "kind": "create", "manifest_path": path, "manifest_text": collection["manifest_text"], "why": why}})
                if kind == "records":
                    guard = created["after_container_hash"]
                    for start in range(0, len(collection["items"]), BULK_UPSERT_MAX_ROWS):
                        rows = [{"item": item} for item in collection["items"][start:start + BULK_UPSERT_MAX_ROWS]]
                        await self.tool(write_tool, {"operation": {
                            "kind": "bulk_upsert", "collection": path, "rows": rows, "expected_container_hash": guard,
                            "source": fixture["source_page"]["path"], "why": why}})
                        guard = (await self.tool(read_tool, {"operation": {"kind": "inspect", "collection": path}}))["snapshot"]
                else:
                    # The product assigns each item key, so a child's `parent` reference is built from its
                    # parent's add response; the generator orders parents before children.
                    keys: list[str] = []
                    for item, parent in zip(collection["items"], collection["parents"], strict=True):
                        if parent is not None:
                            item = {**item, "parent": plan_ref(collection["id"], keys[parent])}
                        added = await self.tool(write_tool, {"operation": {
                            "kind": "add", "collection": path, "item": item, "why": why}})
                        keys.append(added["plan_id"])
                held = await self.tool(read_tool, {"operation": {"kind": "query", "collection": path, "limit": 1}})
                if held["total_matched"] != len(collection["items"]):
                    raise MeasurementRefused("collection-load-incomplete", phase="fixture-load")


async def time_cases(arms: dict[str, Arm], cases: list, *, repeats: int, timeout: float = 120.0) -> list[dict]:
    """Time each case on both arms in adjacent pairs; the inactive worker stays stopped.

    Repeat 0 is an unmeasured warm-up. The arm that goes first alternates across cases and repeats.
    """
    from exomem.recall_canary import measure_call

    samples = []
    for index, case in enumerate(cases):
        for repeat in range(repeats + 1):
            first = "base" if (index + repeat) % 2 == 0 else "head"
            for name in (first, "head" if first == "base" else "base"):
                arms[name].resume()
                try:
                    measurement = await measure_call(arms[name].client, case, timeout)
                finally:
                    arms[name].pause()
                if repeat:
                    samples.append({"case": case.id, "group": case.group, "arm": name, "repeat": repeat - 1,
                                    "first": first, "elapsed_ms": measurement.elapsed_ms,
                                    "outcome": measurement.outcome, "failure": measurement.failure})
    return samples


async def stop_arms(arms: dict[str, Arm]) -> None:
    """Stop every arm and remove the short socket directory that `build_arms` created."""
    await asyncio.gather(*(arm.stop() for arm in arms.values()))
    for directory in {arm.socket.parent for arm in arms.values()}:
        shutil.rmtree(directory, ignore_errors=True)


def progress(line: str) -> None:
    """One stderr line per preparation step: a run that overruns its step budget still shows where the time went."""
    print(line, file=sys.stderr, flush=True)


async def load_and_quiesce(arm: Arm, fixture: dict) -> None:
    started = time.monotonic()
    await arm.load_collections(fixture)
    loaded = time.monotonic()
    await arm.quiesce(limit=1800)
    # Fixture preparation is not part of the summary; one stderr line keeps its cost visible to the operator.
    progress(f"fixture-prep arm={arm.name} load_s={loaded - started:.0f} quiesce_s={time.monotonic() - loaded:.0f}")


async def measure(arms: dict[str, Arm], cases: list, fixture: dict | None, *, repeats: int) -> dict[str, Any]:
    """Prepare quiet arms and time the cases; returns samples and initial startup timings.

    Fixture workers stay running after loading and quiescence. Private workers start on their
    prepared state. Both modes require existing state and ready derived collections before timing.
    """
    try:
        if fixture is not None:
            started = await asyncio.gather(*(arm.start(quiescence_limit=1800) for arm in arms.values()))
            await asyncio.gather(*(load_and_quiesce(arm, fixture) for arm in arms.values()))
        else:
            started = await asyncio.gather(*(arm.start(quiescence_limit=900) for arm in arms.values()))
        timings = dict(zip(arms, started, strict=True))
        for arm in arms.values():
            arm.require_timeable()
        for arm in arms.values():
            arm.pause()
        pressure_start = cpu_pressure()
        samples = await time_cases(arms, cases, repeats=repeats)
        return {"samples": samples, "timings": timings,
                "environment": {"cpu_pressure_start": pressure_start, "cpu_pressure_end": cpu_pressure()}}
    finally:
        await stop_arms(arms)


# --- Public fixture runs and the private sandbox ------------------------------

RECALL_BENCHMARK = ROOT / "benchmarks/recall-latency"


def case_digest(cases: list[dict]) -> str:
    return hashlib.sha256(json.dumps(cases, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def select_cases(cases: list[dict], *, per_group: int | None, shard: int, shards: int) -> tuple[list[dict], int]:
    """Keep the first `per_group` cases of each group, then this shard's slice; also the kept total."""
    if per_group:
        seen: dict[str, int] = defaultdict(int)
        kept = []
        for case in cases:
            seen[case["group"]] += 1
            if seen[case["group"]] <= per_group:
                kept.append(case)
        cases = kept
    return cases[shard::shards], len(cases)


def resolve_commit(tag: str) -> str:
    return subprocess.check_output(["git", "rev-parse", f"{tag}^{{commit}}"], cwd=ROOT, text=True).strip()


def prepare_release(tag: str, scratch: Path) -> tuple[Path, Path]:
    """Extract the tagged tree and install its locked runtime; the archive is dropped once extracted."""
    base_src = extract_release(ROOT, tag, scratch)
    (scratch / "release.tar").unlink()
    return base_src, prepare_tag_runtime(base_src, scratch)


def worker_settings(*, embeddings: bool, passthrough: list[str]) -> dict[str, str]:
    """The retrieval settings of an arm's worker; the recall gate's reference fixes the real-encoder ones."""
    if not embeddings:
        return {"EXOMEM_DISABLE_EMBEDDINGS": "1"}
    settings = {key: os.environ[key] for key in passthrough if key in os.environ}
    settings.update(EXOMEM_EMBED_BACKEND="onnx", EXOMEM_DISABLE_RANKING="1", EXOMEM_DISABLE_CLIP="1",
                    EXOMEM_PRELOAD_MODELS="1")
    return settings


def build_arms(arm_roots: dict[str, Path], pythons: dict[str, Path | str], settings: dict[str, str]) -> dict[str, Arm]:
    # AF_UNIX paths are limited to about 100 bytes, so the sockets live in a short directory.
    sockets = Path(tempfile.mkdtemp(prefix="mrg-", dir="/tmp"))
    return {name: Arm(name, pythons[name], arm_roots[name], sockets, worker_settings=settings) for name in ("base", "head")}


def seed_reference_notes(arm: Arm, source: Path, python: Path | None, recall: Any, profile: dict) -> dict:
    """Prepare one arm's notes state with the recall gate's seeded-vector startup; returns its inputs."""
    runtime = ({"python": python, "runtime_prefix": python.parent.parent, "runtime_policy": "frozen-tag-runtime"}
               if python is not None else {})
    cpus = sorted(os.sched_getaffinity(0))[: recall.REFERENCE["reference_cpus"]]
    worker = recall._Worker(source, arm.vault, arm.state, cpus, profile, **runtime)
    try:
        return worker.environment
    finally:
        worker.close()


def copy_notes(rels: list[str], source: Path, destination: Path) -> None:
    """Give the base arm the head arm's pages byte for byte, with the governance fixture beside them."""
    for rel in rels:
        (destination / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / rel, destination / rel)
    shutil.copytree(source / "Knowledge Base/_Governance", destination / "Knowledge Base/_Governance",
                    dirs_exist_ok=True)
    if (source / "reference-corpus.json").exists():
        shutil.copy2(source / "reference-corpus.json", destination / "reference-corpus.json")


def prepare_notes(arms: dict[str, Arm], base_src: Path, base_python: Path, *, notes_pages: int | None) -> list[str]:
    """Create the notes of both arms: the reference corpus, or a small corpus for a reduced run."""
    from synth_vault import gen_reference_vault, reference_source

    head, base = arms["head"], arms["base"]
    recall_profile = json.loads((RECALL_BENCHMARK / "profile.json").read_text())
    if notes_pages is None:
        import recall_latency_gate as recall

        if len(os.sched_getaffinity(0)) < recall.REFERENCE["reference_cpus"]:
            raise MeasurementRefused("two-cpu-reference-unavailable", phase="reference-preparation")
        # The recall worker checks the stored corpus against its reference profile before it serves, and
        # the scaffold's navigation pages carry semantic units that no vector covers. So each arm is seeded
        # before its scaffold is laid. Its state manifest comes first: the managed worker refuses state
        # without one, and a later `init_vault` writes one only for an empty vault.
        head.init_state()
        base.init_state()
        head_inputs = seed_reference_notes(head, ROOT, None, recall, recall_profile)
        rels = json.loads((head.vault / "reference-corpus.json").read_text())
        copy_notes(rels, head.vault, base.vault)
        base_inputs = seed_reference_notes(base, base_src, base_python, recall, recall_profile)
        if any(head_inputs[key] != base_inputs[key] for key in ("corpus", "encoder_digest", "encoder_identity")):
            raise MeasurementRefused("paired-reference-inputs-differ", phase="reference-preparation")
        head.init_vault(overlay=True)
        base.init_vault(overlay=True)
        return rels
    head.init_vault()
    recall_profile["pages"] = notes_pages
    words, _digest = reference_source(recall_profile, RECALL_BENCHMARK)
    rels = gen_reference_vault(head.vault, recall_profile, words)
    base.init_vault()
    copy_notes(rels, head.vault, base.vault)
    return rels


def write_fixture_files(arm: Arm, fixture: dict) -> None:
    """Write the legacy trackers and the preserved source page, which are files rather than collections."""
    for page in (*fixture["legacy_trackers"], fixture["source_page"]):
        (arm.vault / page["path"]).parent.mkdir(parents=True, exist_ok=True)
        (arm.vault / page["path"]).write_text(page["text"], encoding="utf-8")


def fragment_of(result: dict, *, cases: list[dict], total: int, shard: int, shards: int, header: dict) -> dict:
    return {"schema_version": SCHEMA_VERSION, "mode": "fragment", "state": "measured", "shard": shard,
            "shards": shards, "contract_revision": CONTRACT.revision, "contract_sha256": contract_sha256(),
            "boundary": BOUNDARY, "case_total": total,
            "assigned": [{"id": case["id"], "group": case["group"]} for case in cases],
            "samples": result["samples"],
            "environment": {**result["environment"], "python": sys.version.split()[0],
                            "cpus": len(os.sched_getaffinity(0)), "arms": result["timings"]},
            **header}


def refused_fragment(error: Exception, *, shard: int, phase: str) -> dict:
    # Closed local codes only; raw subprocess or product output never enters evidence.
    return {"schema_version": SCHEMA_VERSION, "mode": "fragment", "state": "refused", "shard": shard,
            "reason": error.code if isinstance(error, MeasurementRefused) else type(error).__name__,
            "phase": (error.phase or phase) if isinstance(error, MeasurementRefused) else phase}


def scratch_directory(path: Path | None):
    if path is None:
        return tempfile.TemporaryDirectory(prefix="mcp-read-gate-")
    path.mkdir(parents=True)
    from contextlib import nullcontext

    return nullcontext(str(path))


def run_paired(args: argparse.Namespace) -> dict:
    from synth_vault import gen_collection_cases, gen_collection_fixture, gen_note_cases

    from exomem.recall_canary import load_cases

    profile_path = BENCHMARK / "profile.json"
    profile = json.loads(profile_path.read_text())
    digest = file_sha256(profile_path)
    if REFERENCE["fixture_profile_sha256"] not in (None, digest):
        raise MeasurementRefused("fixture-profile-digest-mismatch", phase="fixture")
    if not 0 <= args.shard < profile["shards"]:
        raise MeasurementRefused("shard-outside-profile", phase="fixture")
    tag = args.tag or latest_release_tag(ROOT)
    base_commit = resolve_commit(tag)
    if args.base_commit not in (None, base_commit):
        raise MeasurementRefused("release-tag-moved", phase="release-archive")
    reduced = bool(args.max_cases or args.notes_pages)
    with scratch_directory(args.scratch) as temporary:
        scratch = Path(temporary)
        began = time.monotonic()
        base_src, base_python = prepare_release(tag, scratch)
        progress(f"release-prepared elapsed_s={time.monotonic() - began:.0f}")
        recall_profile = json.loads((RECALL_BENCHMARK / "profile.json").read_text())
        arms = build_arms({"head": scratch / "arms/head", "base": scratch / "arms/base"},
                          {"head": sys.executable, "base": base_python},
                          worker_settings(embeddings=args.notes_pages is None,
                                          passthrough=recall_profile["worker_environment"]))
        rels = prepare_notes(arms, base_src, base_python, notes_pages=args.notes_pages)
        progress(f"notes-prepared elapsed_s={time.monotonic() - began:.0f}")
        fixture = gen_collection_fixture(profile)
        everything = gen_collection_cases(fixture, profile) + gen_note_cases(arms["head"].vault, rels, profile)
        cases, total = select_cases(everything, per_group=args.max_cases, shard=args.shard, shards=profile["shards"])
        loaded = load_cases(json.dumps(cases).encode())
        for arm in arms.values():
            write_fixture_files(arm, fixture)
        result = asyncio.run(measure(arms, loaded, fixture, repeats=REFERENCE["repeats"]))
    return fragment_of(result, cases=cases, total=total, shard=args.shard, shards=profile["shards"], header={
        "profile_sha256": digest, "cases_sha256": case_digest(everything), "tag": tag,
        "base_commit": base_commit, "reduced": reduced})


def run_private(args: argparse.Namespace) -> dict:
    """Time a private prepared vault on both arms; its inputs never leave the operator's host."""
    from exomem.recall_canary import load_cases

    roots = {"base": args.arm_roots[0], "head": args.arm_roots[1]}
    raw = args.cases.read_bytes()
    loaded = load_cases(raw)
    if any(case.group is None for case in loaded):
        raise MeasurementRefused("case-group-missing", phase="cases")
    tag = args.tag or latest_release_tag(ROOT)
    base_commit = resolve_commit(tag)
    with scratch_directory(args.scratch) as temporary:
        scratch = Path(temporary)
        base_src, base_python = prepare_release(tag, scratch)
        if args.prepare:
            for root in roots.values():
                if root.exists():
                    raise MeasurementRefused("arm-root-exists", phase="arm-preparation")
                root.mkdir(parents=True)
                for part in ("vault", "state", "lease"):
                    shutil.copytree(args.golden / part, root / part, symlinks=True)
                shutil.copy2(args.golden / "coordinator.sqlite", root / "coordinator.sqlite")
        elif not all((root / "prepared.json").is_file() for root in roots.values()):
            raise MeasurementRefused("arm-not-prepared", phase="arm-preparation")
        arms = build_arms(roots, {"head": sys.executable, "base": base_python},
                          worker_settings(embeddings=args.embeddings,
                                          passthrough=json.loads((RECALL_BENCHMARK / "profile.json").read_text())
                                          ["worker_environment"]))
        if args.prepare:
            asyncio.run(warm(arms))
            for root in roots.values():
                (root / "prepared.json").write_text(json.dumps({"prepared": True}))
            return {"state": "prepared"}
        result = asyncio.run(measure(arms, loaded, None, repeats=REFERENCE["repeats"]))
    cases = [{"id": case.id, "group": case.group} for case in loaded]
    return fragment_of(result, cases=cases, total=len(cases), shard=0, shards=1, header={
        "profile_sha256": None, "cases_sha256": hashlib.sha256(raw).hexdigest(), "tag": tag,
        "base_commit": base_commit, "reduced": False})


async def warm(arms: dict[str, Arm]) -> None:
    """Start each arm once on its copied state, so a later timed start finds that state existing."""
    try:
        await asyncio.gather(*(arm.start(quiescence_limit=3600) for arm in arms.values()))
    finally:
        await stop_arms(arms)


# --- Commands -------------------------------------------------------------------


def render(summary: dict) -> str:
    if summary["state"] == "refused":
        return "refused: " + ", ".join(f"{f['code']} ({f['phase']})" for f in summary["failures"])
    lines = [f"{'group':16} {'cases':>5} {'mean ms':>9} {'lower':>9} {'upper':>9} {'allowance':>9} "
             f"{'base p95':>9} {'head p95':>9}  verdict"]
    def cell(group: dict, key: str) -> str:
        return f"{group[key]:9.1f}" if group.get(key) is not None else f"{'-':>9}"

    for group in summary["groups"]:
        target = f"  {group['target']['result']}" if "target" in group else ""
        lines.append(f"{group['group']:16} {group['cases']:5} {cell(group, 'mean_ms')} {cell(group, 'lower_ms')} "
                     f"{cell(group, 'upper_ms')} {cell(group, 'allowance_ms')} {cell(group, 'base_p95_ms')} "
                     f"{cell(group, 'head_p95_ms')}  {group['verdict']}{target}")
    lines += [f"warning: {w['code']}" + (f" {w['group']}" if "group" in w else "") for w in summary["warnings"]]
    lines += [f"failure: {f['code']}" + "".join(f" {k}={v}" for k, v in f.items() if k != "code") for f in summary["failures"]]
    lines.append(f"state: {summary['state']}")
    return "\n".join(lines)


def write_document(path: Path, document: dict) -> None:
    validate_document(document)
    path.write_text(json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n")


def measured_cli(runner, args: argparse.Namespace) -> int:
    phase = "preparation"
    try:
        fragment = runner(args)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        fragment = refused_fragment(error, shard=getattr(args, "shard", 0), phase=phase)
    if fragment["state"] == "prepared":
        print(json.dumps({"state": "prepared"}))
        return 0
    if args.mode == "private":
        summary = summarize([fragment], mode="private")
        write_document(args.output, summary)
        print(render(summary))
        host = fragment.get("environment", {})
        print(f"host cpu pressure avg60: start {host.get('cpu_pressure_start')} end {host.get('cpu_pressure_end')}")
        return int(summary["state"] not in ("passed", "reduced"))
    write_document(args.output, fragment)
    print(json.dumps({"state": fragment["state"], "artifact": args.output.name}))
    return int(fragment["state"] != "measured")


def plan_cli(args: argparse.Namespace) -> int:
    profile = json.loads((BENCHMARK / "profile.json").read_text())
    tag = args.tag or latest_release_tag(ROOT)
    plan = {"schema_version": SCHEMA_VERSION, "mode": "plan", "tag": tag, "base_commit": resolve_commit(tag),
            "shards": list(range(profile["shards"]))}
    write_document(args.output, plan)
    print(json.dumps(plan))
    return 0


def aggregate_cli(args: argparse.Namespace) -> int:
    fragments = [json.loads(path.read_text()) for path in args.fragments]
    for fragment in fragments:
        validate_document(fragment)
    summary = summarize(fragments, mode="aggregate", head_profile_sha256=file_sha256(BENCHMARK / "profile.json"))
    write_document(args.output, summary)
    print(render(summary))
    return int(summary["state"] not in ("passed", "reduced"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare MCP read latency with the latest release.")
    commands = parser.add_subparsers(dest="mode", required=True)
    plan = commands.add_parser("plan", help="pin the release and list the shards")
    plan.add_argument("--tag")
    plan.add_argument("--output", type=Path, default=Path("mcp-read-latency-plan.json"))
    plan.set_defaults(run=plan_cli)
    paired = commands.add_parser("paired", help="time one shard of the generated fixture on both arms")
    paired.add_argument("--shard", type=int, default=0)
    paired.add_argument("--tag")
    paired.add_argument("--base-commit")
    paired.add_argument("--max-cases", type=int, help="reduced run: keep this many cases per group")
    paired.add_argument("--notes-pages", type=int, help="reduced run: this many pages, embeddings off")
    paired.add_argument("--scratch", type=Path, help="new directory for the run; kept after it")
    paired.add_argument("--output", type=Path, default=Path("mcp-read-latency-fragment.json"))
    paired.set_defaults(run=lambda a: measured_cli(run_paired, a))
    private = commands.add_parser("private", help="time a private prepared vault; inputs stay outside the repository")
    private.add_argument("--golden", type=Path, required=True)
    private.add_argument("--cases", type=Path, required=True)
    private.add_argument("--arm-roots", required=True, help="BASE,HEAD fixed directories outside the repository")
    private.add_argument("--embeddings", action="store_true")
    private.add_argument("--tag")
    private.add_argument("--scratch", type=Path)
    private.add_argument("--output", type=Path, default=Path("mcp-read-latency-private.json"))
    choice = private.add_mutually_exclusive_group(required=True)
    choice.add_argument("--prepare", action="store_true", help="copy the golden into both roots and warm them")
    choice.add_argument("--reuse", action="store_true", help="time the roots as prepared; never rebuild them")
    private.set_defaults(run=lambda a: measured_cli(run_private, a))
    aggregate = commands.add_parser("aggregate", help="join fragments into per-group verdicts")
    aggregate.add_argument("fragments", type=Path, nargs="+")
    aggregate.add_argument("--output", type=Path, default=Path("mcp-read-latency-summary.json"))
    aggregate.set_defaults(run=aggregate_cli)
    check = commands.add_parser("validate-summary", help="validate a stored plan, fragment or summary")
    check.add_argument("document", type=Path)
    check.set_defaults(run=lambda a: validate_document(json.loads(a.document.read_text())) or 0)
    args = parser.parse_args(argv)
    if args.mode == "private":
        args.arm_roots = [Path(part) for part in args.arm_roots.split(",")]
        if len(args.arm_roots) != 2:
            parser.error("--arm-roots takes BASE,HEAD")
    return args.run(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
