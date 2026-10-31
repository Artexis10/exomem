"""Installed-product acceptance for fast durable write acknowledgement.

The connector measures HTTP entry to acknowledgement and the public read that
immediately follows it. Its service owns startup and background convergence.
Direct mode is an in-process helper diagnosis, not maintained-runtime proof.
On a vault with a collection store it serves that store as a managed service does:
its own loopback writer-lease coordinator, then the lease and the store thread, then
managed recall, before it writes anything. It never enrols a store: a copied vault is
refused with the product's own adoption instruction, which the owner runs first.
The deterministic gate in ``semantic_write_latency.py`` proves the shape on
synthetic corpora.

Four things have to hold together, and each can fail the run on its own:

* existing default-write p50/p90 bars and the shared ordinary warm-write p95
  bar, per public operation, with cold priming calls reported separately;
* the read immediately after a write is exact, or explicitly warming -- a stale
  answer is never acceptable, because deferring derived work must not become
  losing it;
* no write covered by exact component custody mints an uncovered full-index
  receipt, which would mean the change had traded a bounded deferral for
  whole-vault debt;
* the backlog a burst creates converges within a bounded window rather than
  leaving recall warm indefinitely.

Reports contain synthetic case timings, closed outcomes, counts and ages.
Package provenance includes its hashed origin, version and source revision;
vault paths, titles, excerpts and exception text stay out of the report.

It runs against a disposable vault it is handed and nothing else. Resolving a
vault from the ambient environment is refused rather than discouraged: pointing
this at a live cell is the one way it could do harm.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.metadata
import importlib.resources
import json
import os
import secrets
import socket
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
# Source trials must opt in before importing the product. Installed runs never
# add the repository source tree, including through the shared warm-up helper.
_bootstrap = argparse.ArgumentParser(add_help=False)
_bootstrap.add_argument("--package-mode", choices=("installed", "source"), default="installed")
if _bootstrap.parse_known_args()[0].package_mode == "source":
    sys.path.insert(0, str(SRC))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import e2e_product_loop as loop  # noqa: E402
import managed_recall  # noqa: E402
import records_live_acceptance  # noqa: E402
import yaml  # noqa: E402

from exomem import (  # noqa: E402
    call_spans,
    commands,
    deferred_index,
    derived_drain,
    derived_receipts,
    due_state,
    mode,
    pending_recall,
    readiness,
    request_latency_contract,
    semantic_contract,
    vault,
    writer_lease,
)
from exomem import find as find_module  # noqa: E402
from exomem.cli_ops import OpError  # noqa: E402
from exomem.collection_store import ownership  # noqa: E402
from exomem.collection_store import runtime as store_runtime  # noqa: E402

#: The design's installed-product numbers. Not this script's to tune.
P50_MS = 3_000.0
P90_MS = 5_000.0
#: The acceptance requires at least this many samples of each operation per
#: transport. Fewer is not a faster run; it is a weaker claim.
MIN_SAMPLES_PER_OPERATION = 30
#: A burst has to drain, not merely be durable. Five minutes is the design's own
#: oldest-required-component alarm, so exceeding it here is the same breach the
#: rollout would pause on.
CONVERGENCE_BOUND_SECONDS = 300.0
# These are the public commands this acceptance protocol measures.
OPERATIONS = ("remember", "edit_memory", "record_memory", "plan_memory")
TRANSPORTS = ("direct", "connector")

#: A vault with a collection store is fenced by a writer-lease coordinator, whose database
#: holds that fence. A copied vault is enrolled in this run's coordinator by the owner's
#: offline ``exomem collections adopt-local``, so that step and this run name the same
#: coordinator database (``<state-dir>/leases.sqlite``), vault id and replica id.
LEASE_DATABASE_NAME = "leases.sqlite"
LEASE_VAULT_ID = "live-write-acceptance"
LEASE_REPLICA_ID = "live-write-acceptance-direct"
#: The store thread opens and admits the store after the service starts; a store that
#: is still not admitted after this long is reported with its refusal, not measured.
STORE_ADMISSION_SECONDS = 300.0

#: Bounded so one pathological response cannot be read into memory.
_MAX_HTTP_RESPONSE_BYTES = 1_048_576
_CONNECTOR_TIMEOUT_SECONDS = 30.0


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(
        len(ordered) - 1, max(0, int(len(ordered) * fraction + 0.999) - 1)
    )
    return ordered[index]


def _remember_content(title: str, marker: str, relation_target: str | None) -> str:
    """A remember body that qualifies through one typed relation, as a real note does.

    Without a target (an empty vault) the first remember is the vault's bootstrap
    page, which needs no relation.
    """
    content = (
        f"# {title}\n\n"
        "## Observations\n\n"
        f"- [config] {marker} #acceptance (live)\n"
    )
    if relation_target is not None:
        content += f"\n## Relations\n\n- supports [[{relation_target}]]\n"
    return content


def _load_contract(path: Path | None = None) -> tuple[request_latency_contract.LatencyContract, str]:
    if path is None:
        contract = request_latency_contract.load_contract()
        source = importlib.resources.files("exomem").joinpath("request_latency_contract.json").read_bytes()
    else:
        source = path.read_bytes()
        contract = request_latency_contract.LatencyContract.model_validate_json(source)
    return contract, hashlib.sha256(source).hexdigest()


def _package_identity(package_mode: str, *, role: str = "observer-and-direct-runtime") -> dict[str, Any]:
    from installed_package_identity import package_identity

    try:
        return package_identity(package_mode, role=role)
    except ValueError as error:
        raise SystemExit(
            "installed acceptance requires a non-editable installed exomem package"
        ) from error


def _require_disposable_environment(vault_root: Path, state_dir: Path) -> None:
    # A wrong refusal costs a disposable run; a wrong admission can mutate live
    # data. Restrict local writes to explicit temporary paths and reject conflicts.
    temporary = Path(tempfile.gettempdir()).resolve()
    if not vault_root.resolve().is_relative_to(temporary) or not state_dir.resolve().is_relative_to(temporary):
        raise SystemExit("write acceptance requires disposable vault and state paths under the temporary directory")
    ambient = os.environ.get("EXOMEM_VAULT_PATH")
    if ambient and Path(ambient).resolve() != vault_root.resolve():
        raise SystemExit("write acceptance refuses a conflicting ambient vault")
    for key in ("EXOMEM_STATE_ROOT", "EXOMEM_WRITER_LEASE_STATE_DIR"):
        value = os.environ.get(key)
        if value and not Path(value).resolve().is_relative_to(temporary):
            raise SystemExit("write acceptance refuses non-temporary ambient state")
    if not os.environ.get("EXOMEM_STATE_ROOT"):
        os.environ["EXOMEM_STATE_ROOT"] = str(state_dir / "product")
    if not os.environ.get("EXOMEM_WRITER_LEASE_STATE_DIR"):
        os.environ["EXOMEM_WRITER_LEASE_STATE_DIR"] = str(state_dir)
    if not os.environ.get("EXOMEM_CONFIG_PATH"):
        os.environ["EXOMEM_CONFIG_PATH"] = str(state_dir / "no-config.json")


def _require_disposable_connector(
    url: str, pid: int | None, host_network_namespace: str | None, vault_root: Path, state_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    # A wrong refusal costs a private run; a wrong admission risks production
    # writes. Bind loopback to an isolated process before creating fixtures.
    try:
        endpoint = urlsplit(url)
        port = endpoint.port
    except ValueError as error:
        raise SystemExit("connector requires a literal loopback MCP URL") from error
    if port is None or not 0 < port < 65536 or url != f"http://127.0.0.1:{port}/mcp":
        raise SystemExit("connector requires http://127.0.0.1:<port>/mcp without authentication, query or fragment")
    if pid is None or pid <= 0:
        raise SystemExit("connector requires an explicit --connector-pid")
    if not host_network_namespace:
        raise SystemExit("connector requires an explicit --host-network-namespace observed by the launcher")
    try:
        collector_network = os.readlink("/proc/self/ns/net")
        service_network = os.readlink(f"/proc/{pid}/ns/net")
        # A private Linux namespace has only its kernel loopback interface.
        interfaces = [name for _index, name in socket.if_nameindex()]
        environment = dict(
            field.decode().split("=", 1)
            for field in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
            if b"=" in field
        )
    except (OSError, UnicodeError, ValueError) as error:
        raise SystemExit("connector process isolation proof is unreadable") from error
    if collector_network != service_network or collector_network == host_network_namespace or interfaces != ["lo"]:
        raise SystemExit("connector and observer require the same loopback-only network namespace, distinct from the host anchor")
    # These are the fixed environment fields that bind the disposable runtime.
    expected = {
        "EXOMEM_VAULT_PATH": str(vault_root.resolve()),
        "EXOMEM_STATE_ROOT": str(Path(os.environ["EXOMEM_STATE_ROOT"]).resolve()),
        "EXOMEM_WRITER_LEASE_STATE_DIR": str(state_dir.resolve()),
    }
    if any(environment.get(key) != value for key, value in expected.items()):
        raise SystemExit("connector initial environment does not match the explicit disposable vault and state")
    instance = environment.get("EXOMEM_INSTANCE_ID", "").strip()
    if not instance:
        raise SystemExit("connector initial environment requires EXOMEM_INSTANCE_ID")
    try:
        request = Request(f"http://127.0.0.1:{port}/health/ready", method="GET")
        try:
            response = urlopen(request, timeout=_CONNECTOR_TIMEOUT_SECONDS)
        except HTTPError as error:
            # Startup may truthfully report not_ready. Its identity still binds
            # the endpoint; health alone never proves full startup readiness.
            if error.code != 503:
                raise
            response = error
        with response:
            body = response.read(_MAX_HTTP_RESPONSE_BYTES + 1)
            status = response.status
        if len(body) > _MAX_HTTP_RESPONSE_BYTES:
            raise ValueError("health response exceeds size limit")
        health = json.loads(body)
    except (OSError, ValueError) as error:
        raise SystemExit("connector readiness identity proof is unreadable") from error
    if not isinstance(health, dict) or health.get("instance_id") != instance:
        raise SystemExit("connector readiness instance does not match the explicit process")
    return {
        "connector_pid": pid,
        "collector_network_namespace": collector_network,
        "service_network_namespace": service_network,
        "host_network_namespace": host_network_namespace,
        "host_network_namespace_source": "launcher-observed",
        "network_interfaces": interfaces,
        "initial_environment_matches": {key: environment[key] == value for key, value in expected.items()},
        "instance_id": instance,
    }, {
        "source": "service-health-ready",
        "http_status": status,
        "status": health.get("status"),
        "instance_id": health["instance_id"],
        "release": health.get("release"),
        "mcp_tool_surface_sha256": health.get("mcp_tool_surface_sha256"),
        "retrieval": health.get("retrieval"),
        "runtime_readiness": "externally-unverified",
    }


def _start_lease_coordinator(
    state_dir: Path, token: str, package_mode: str
) -> tuple[subprocess.Popen, str]:
    """Start this run's own coordinator on a loopback port, its database under ``state_dir``.

    The e2e journeys' port, readiness and termination helpers do the plumbing; none of
    those scripts exports a coordinator start of its own.
    """
    (reservation,) = loop._reserve_port_reservations(1)
    port = int(reservation.getsockname()[1])
    url = f"http://127.0.0.1:{port}"
    environment = {
        key: value for key, value in os.environ.items()
        if key != "EXOMEM_LEASE_COORDINATOR_OPERATOR_TOKEN"
    }
    environment["EXOMEM_LEASE_COORDINATOR_TOKEN"] = token
    if package_mode == "source":
        environment["PYTHONPATH"] = os.pathsep.join(
            part for part in (str(SRC), environment.get("PYTHONPATH")) if part
        )
    log = state_dir / "coordinator.log"
    reservation.close()
    with log.open("w", encoding="utf-8") as handle:
        process = subprocess.Popen(
            [sys.executable, "-m", "exomem.lease_coordinator", "--host", "127.0.0.1",
             "--port", str(port), "--database", str(state_dir / LEASE_DATABASE_NAME)],
            env=environment, stdout=handle, stderr=subprocess.STDOUT, text=True,
        )
    try:
        loop._wait_http_ready(
            lambda: loop._http_json(
                f"{url}/v1/vaults/{LEASE_VAULT_ID}/lease", token=token, timeout=1.0
            )[0] == 200,
            proc=process, deadline=time.monotonic() + 60.0, log=log, label="lease coordinator",
        )
    except BaseException:
        loop._terminate(process, 10.0)
        raise
    return process, url


def _require_store_served(vault_root: Path, state_dir: Path, manager: writer_lease.LeaseManager) -> None:
    """Refuse a run whose store never reached admission, with the product's own reason.

    The product's refusal says what to run; this adds which coordinator that step must address.
    """
    deadline = time.monotonic() + STORE_ADMISSION_SECONDS
    while True:
        status = manager.status().get("collection_store", {})
        if status.get("status") == "admitted":
            return
        server = store_runtime._SERVERS.get(vault_root.resolve())
        # The refusal that `exomem collections adopt-local` is named in, as the product reads it.
        code, message = server.refusal if server is not None else ("COLLECTION_STORE_UNAVAILABLE", "no store thread")
        if code != "COLLECTION_STORE_UNAVAILABLE" or time.monotonic() >= deadline:
            raise SystemExit(
                f"the vault's collection store is not served ({code}): {message}; this run's coordinator "
                f"database is {state_dir / LEASE_DATABASE_NAME}, with vault id {LEASE_VAULT_ID} and "
                f"replica id {LEASE_REPLICA_ID}"
            )
        time.sleep(0.25)


@contextlib.contextmanager
def _served_store(
    vault_root: Path, state_dir: Path, package_mode: str
) -> Iterator[writer_lease.LeaseManager]:
    """Serve a vault that has a collection store as a managed service does.

    A service starts its writer lease and the store thread behind it before it takes
    a request; the store refuses every route until a coordinator fences it and the
    thread has admitted it. The service environment is patched for the length of the
    run only, so the process the caller owns is left as it was found.
    """
    token = secrets.token_hex(16)
    coordinator, url = _start_lease_coordinator(state_dir, token, package_mode)
    try:
        with mock.patch.dict(os.environ, {
            "EXOMEM_VAULT_PATH": str(vault_root),
            "EXOMEM_WRITER_LEASE_URL": url, "EXOMEM_WRITER_LEASE_TOPOLOGY": "single-host",
            "EXOMEM_WRITER_LEASE_VAULT_ID": LEASE_VAULT_ID,
            "EXOMEM_WRITER_LEASE_REPLICA_ID": LEASE_REPLICA_ID,
            "EXOMEM_WRITER_LEASE_TOKEN": token, "EXOMEM_WRITER_LEASE_STATE_DIR": str(state_dir),
            "EXOMEM_WRITER_LEASE_PREFERRED": "1",
        }):
            manager = writer_lease.start_server_lifecycle()
            try:
                _require_store_served(vault_root, state_dir, manager)
                yield manager
            finally:
                writer_lease.stop_server_lifecycle()
    finally:
        loop._terminate(coordinator, 10.0)


class _DirectTransport:
    """The same public command descriptors and lease boundary as the server."""

    name = "direct"

    def __init__(self, vault_root: Path, manager: writer_lease.LeaseManager) -> None:
        self.vault_root = vault_root
        self.manager = manager

    def call(self, name: str, arguments: dict[str, Any]) -> tuple[float, dict[str, Any]]:
        command = next(command for command in commands.PRODUCT_COMMANDS if command.name == name)
        token = str(uuid.uuid4())
        context = call_spans.MCP_CALL_TOKEN.set(token)
        cpu_started = time.thread_time()
        started = time.perf_counter()
        try:
            result = self.manager.invoke(
                command, (self.vault_root,), arguments,
                read_only=commands.invocation_is_read_only(command, arguments),
                mutation_request_id=str(uuid.uuid4()),
                implicit_idempotency_scope="session:live-write-acceptance",
            )
            elapsed = (time.perf_counter() - started) * 1_000.0
        finally:
            self.thread_cpu_ms = (time.thread_time() - cpu_started) * 1_000.0
            spans = call_spans.pop_call_spans(token)
            call_spans.MCP_CALL_TOKEN.reset(context)
            self.phase_attribution = [
                {"name": span["name"], "elapsed_ms": span["ms"],
                 "thread_cpu_us": span.get("fields", {}).get("thread_cpu_us")}
                for span in spans
            ]
        return elapsed, result


class _ConnectorTransport:
    """External HTTP observer; measure() proves the disposable process first."""

    name = "connector"

    def __init__(self, url: str, token: str | None) -> None:
        self.url = url
        self.token = token

    def call(self, name: str, arguments: dict[str, Any]) -> tuple[float, Any]:
        request_id = str(uuid.uuid4())
        self.request_id = request_id
        payload = json.dumps({
            "jsonrpc": "2.0", "id": request_id, "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }).encode("utf-8")
        headers = {"content-type": "application/json", "accept": "application/json, text/event-stream",
                   "x-exomem-request-id": request_id}
        if self.token:
            headers["authorization"] = f"Bearer {self.token}"
        request = Request(self.url, data=payload, headers=headers, method="POST")
        started = time.perf_counter()
        try:
            with urlopen(request, timeout=_CONNECTOR_TIMEOUT_SECONDS) as response:
                body = response.read(_MAX_HTTP_RESPONSE_BYTES + 1)
                content_type = records_live_acceptance._content_type(response)
        except (HTTPError, URLError, TimeoutError) as error:
            raise SystemExit(f"connector call failed ({type(error).__name__})") from error
        elapsed = (time.perf_counter() - started) * 1_000.0
        if len(body) > _MAX_HTTP_RESPONSE_BYTES:
            raise SystemExit("connector response exceeds size limit")
        envelope = records_live_acceptance._decode_http_response(body, content_type, request_id)
        result = envelope["result"]
        if result.get("isError"):
            raise SystemExit("connector tool returned an error")
        structured = result.get("structuredContent")
        if structured is None:
            content = result.get("content", [])
            if len(content) != 1 or content[0].get("type") != "text":
                raise SystemExit("connector response has no domain result")
            structured = json.loads(content[0]["text"])
        # FastMCP wraps union/list return schemas in this result envelope.
        if isinstance(structured, dict) and set(structured) == {"result"}:
            structured = structured["result"]
        if not isinstance(structured, (dict, list)):
            raise SystemExit("connector domain result must be an object or list")
        return elapsed, structured


def _prepare_collections(client: Any) -> tuple[dict[str, Any], str]:
    _, described = client.call("record_memory", {"action": "describe"})
    example = described["examples"]["minimal"]
    client.call("record_memory", {
        "action": "create", "manifest_path": example["manifest_path"],
        "manifest_text": example["manifest_text"], "why": "prepare disposable acceptance",
    })
    # Reuse the shipped example's collection envelope and the Planning field
    # shape. Defaults and accepted Planning values remain product-owned.
    manifest, _, _ = vault.parse_frontmatter(example["manifest_text"], strict=True)
    manifest.update({
        "semantic_profile": "planning", "title": "Acceptance plans",
        "exomem_id": str(uuid.uuid4()),
        "item_schema": {"natural_key": ["title"], "fields": {
            field: {"type": "string", **({"required": True} if field == "title" else {})}
            for field in ("title", "kind", "status", "lifecycle", "priority", "commitment", "horizon", "health", "area", "parent")
        }},
    })
    planning_path = "Knowledge Base/Planning/Acceptance/_collection.md"
    client.call("plan_memory", {
        "action": "create", "manifest_path": planning_path,
        "manifest_text": "---\n" + yaml.safe_dump(manifest, sort_keys=False) + "---\n",
        "why": "prepare disposable acceptance",
    })
    return example, planning_path


def _read_your_write(vault_root: Path, marker: str, rel_path: str) -> str:
    """One closed outcome for the read that immediately follows a write."""
    try:
        hits = find_module.find(
            vault_root, query=marker, scope="kb-only", mode="keyword", limit=5
        )
    except find_module.RetrievalIndexWarming:
        # A truthful refusal. The contract permits it; it is never a stale
        # answer, which is the outcome that matters.
        return "warming"
    except Exception:  # noqa: BLE001 - an unreadable answer is not an exact one
        return "stale"
    return "exact" if any(hit.path == rel_path for hit in hits) else "stale"


def _connector_read_your_write(client: Any, marker: str, rel_path: str) -> str:
    """Check a fresh marker through public recall and the selected page read."""
    try:
        _, result = client.call("ask_memory", {
            "query": marker, "scope": "kb-only", "mode": "keyword", "limit": 5,
            "detail": "full", "graph": False, "rerank": False,
        })
        if isinstance(result, dict):
            failure = result.get("error")
            if isinstance(failure, dict):
                if failure.get("code") == "RETRIEVAL_INDEX_WARMING":
                    # RetrievalIndexWarming declares both public availability statuses.
                    if failure.get("status") == "warming":
                        return "warming"
                    if failure.get("status") == "temporarily_unavailable":
                        return "unavailable"
                return "unknown"
            hits = result.get("hits")
            warming = bool(result.get("warming"))
        else:
            hits, warming = result, False
        if not isinstance(hits, list):
            return "unknown"
        hit = next((hit for hit in hits if hit.get("path") == rel_path), None)
        if hit is None:
            return "warming" if warming else "stale"
        _, page = client.call("read_memory", {"path": hit.get("ref") or hit["path"]})
        if not isinstance(page, dict) or page.get("error") or page.get("success") is False:
            return "unknown"
        if page.get("path") != rel_path or not isinstance(page.get("body"), str) or marker not in page["body"]:
            return "stale"
        return "exact"
    except (Exception, SystemExit):  # noqa: BLE001 - a failed public read proves no visibility
        return "unknown"


def _connector_relation_target(client: Any) -> str:
    """Use a compiled fixture anchor, never an arbitrary search hit or manifest."""
    _, recalled = client.call("ask_memory", {
        "query": "", "scope": "kb-only", "mode": "keyword", "limit": 1,
        "detail": "full", "types": ["insight"], "result_level": "page",
    })
    hits = recalled.get("hits", []) if isinstance(recalled, dict) else recalled
    if isinstance(hits, list) and hits and hits[0].get("path"):
        return str(hits[0]["path"]).removesuffix(".md")
    marker = f"acceptanceanchor{uuid.uuid4().hex}"
    _, seeded = client.call("remember", {
        "title": f"Acceptance anchor {uuid.uuid4().hex}",
        "content": _remember_content("Acceptance anchor", marker, None),
        "note_type": "insight", "suggestions": False, "response_detail": "compact",
    })
    if seeded.get("outcome") in ("held", "refused") or not seeded.get("path"):
        raise RuntimeError("the public fixture call did not create a compiled relation anchor")
    return str(seeded["path"]).removesuffix(".md")


def _readonly_receipt_connection(vault_root: Path) -> sqlite3.Connection:
    """Observe an existing supported store without product repair wrappers."""
    connection = sqlite3.connect(
        f"{deferred_index.store_path(vault_root).as_uri()}?mode=ro", uri=True, timeout=5.0,
    )
    try:
        if not derived_receipts._receipt_schema_is_current(connection):
            raise sqlite3.DatabaseError("unsupported receipt schema")
    except BaseException:
        connection.close()
        raise
    return connection


def _full_receipt_count(vault_root: Path, *, read_only: bool = False) -> int:
    try:
        if read_only:
            connection = _readonly_receipt_connection(vault_root)
            try:
                return int(connection.execute(
                    f"SELECT count(*) FROM {deferred_index._QUEUE_TABLES['full']}"
                ).fetchone()[0])
            finally:
                connection.close()
        return len(deferred_index.list_full_paths(vault_root))
    except Exception:  # noqa: BLE001 - an unreadable counter cannot prove zero
        return -1


def _reconciliation_demanded(vault_root: Path, *, read_only: bool = False) -> int:
    """Batches that fail closed into explicit reconciliation demand.

    This is deliberately NOT folded into convergence. A `reconcile_required`
    receipt is not a backlog that more draining would clear -- it is the
    store's declared, terminal statement that it could prove neither the
    complete before-state nor the intended after-state. A burst of ordinary
    writes producing one is a real acceptance failure, and reporting it as
    "still converging" would let it look like slowness.
    """
    if not read_only:
        return derived_receipts.stranded_batch_count(vault_root)
    try:
        connection = _readonly_receipt_connection(vault_root)
        try:
            return int(connection.execute(derived_receipts._STRANDED_BATCHES_SQL).fetchone()[0])
        finally:
            connection.close()
    except Exception:  # noqa: BLE001 - an unreadable counter cannot prove zero
        return -1


#: A component this many attempts deep has stopped being a transient retry.
#: `retry_component` backs off 5s * 2**(attempts-1) capped at 120s, so the
#: third attempt is the first one whose next window is a minute away; below it
#: the drain is retrying, which is ordinary, not held back.
STUCK_ATTEMPT_THRESHOLD = 3


def _drain_observation(vault_root: Path, *, read_only: bool = False) -> dict[str, int]:
    """What the store says about components a burst has not settled yet.

    Content-free by construction: counts, a mode name and ages -- no batch id,
    component name, path or failure text.

    The dueness test is the product's own, character for character
    (`derived_receipts.due_component_count`), because the two counters must not
    disagree about a row. They did: `retry_component` stamps a `failure_code`
    and bumps `attempt_count` on every transient retry and leaves the row
    `retryable`, and nothing clears that code until the component completes. A
    predicate reading "attempted, and carrying a code" therefore called the
    ordinary first retry of a lease that outlived its dispatch *stuck* -- while
    `due_component_count` counted the same row as due and
    `claim_ready_components` would have claimed it on the very next pass.

    So the counters are split by what an operator would do about them:

    * `retrying` -- due, and attempted before. The drain will pick it up. This
      is the common case on a real corpus and it is not a fault.
    * `stuck` -- NOT due (backing off into the future, or held under an
      unexpired claim) and at least `STUCK_ATTEMPT_THRESHOLD` attempts deep.
      More draining will not clear this inside any window worth waiting.
    * `failed` -- carrying a failure code while not due. Reported, but not on
      its own a reason to stop: the backoff is exactly the mechanism that
      clears it.
    * `claimed` -- held under a live lease. `drain_once` leaves one behind when
      a batch's committed proof is not `ready`, and neither `due` nor `stuck`
      can see it, so a run could otherwise report "converged" with work
      parked under a claim nobody is working.

    An unreadable store reports -1 rather than zero, because a counter that
    cannot be read has not proved anything.
    """
    due = -1
    if not read_only:
        try:
            due = int(derived_receipts.due_component_count(vault_root))
        except Exception:  # noqa: BLE001 - an unreadable counter cannot prove zero
            pass

    unreadable = {
        "pending": -1,
        "due": due,
        "claimed": -1,
        "retrying": -1,
        "stuck": -1,
        "failed": -1,
        "max_attempt_count": -1,
    }
    rows: list[tuple[Any, ...]] = []
    try:
        if read_only or deferred_index.store_path(vault_root).exists():
            connection = _readonly_receipt_connection(vault_root) if read_only else deferred_index._connect_readonly(vault_root)
            try:
                # `is_due` is `due_component_count`'s WHERE clause verbatim,
                # evaluated per row and against the same `now`.
                rows = connection.execute(
                    "SELECT c.state, c.attempt_count, c.claim_expires_at, "
                    "c.failure_code, "
                    "(CASE WHEN b.state = 'ready' AND (c.state = 'ready' OR "
                    "(c.state = 'retryable' AND c.next_attempt_at <= :now) OR "
                    "(c.state = 'claimed' AND c.claim_expires_at <= :now)) "
                    "AND NOT EXISTS (SELECT 1 FROM pending_recall_rows AS p "
                    "WHERE p.batch_id = c.batch_id AND p.state = 'prepared') "
                    "THEN 1 ELSE 0 END) AS is_due "
                    "FROM derived_batch_components AS c "
                    "JOIN derived_batches AS b ON b.batch_id = c.batch_id "
                    "WHERE b.state IN ('prepared', 'ready', 'reconcile_required') "
                    "AND c.state NOT IN ('completed', 'not_required', 'aborted', 'superseded')",
                    {"now": time.time()},
                ).fetchall()
                if read_only:
                    due = sum(int(row[-1] or 0) for row in rows)
            finally:
                connection.close()
    except Exception:  # noqa: BLE001 - same
        return unreadable

    now = time.time()
    claimed = 0
    retrying = 0
    stuck = 0
    failed = 0
    max_attempt_count = 0
    for state, attempt_count, claim_expires_at, failure_code, is_due in rows:
        attempts = int(attempt_count or 0)
        max_attempt_count = max(max_attempt_count, attempts)
        if state == "claimed" and (
            claim_expires_at is None or float(claim_expires_at) > now
        ):
            claimed += 1
        if int(is_due or 0):
            if attempts > 0:
                retrying += 1
            continue
        if attempts >= STUCK_ATTEMPT_THRESHOLD:
            stuck += 1
        if failure_code is not None:
            failed += 1
    return {
        "pending": len(rows),
        "due": due,
        "claimed": claimed,
        "retrying": retrying,
        "stuck": stuck,
        "failed": failed,
        "max_attempt_count": max_attempt_count,
    }


def _converge(
    vault_root: Path, *, bound_seconds: float = CONVERGENCE_BOUND_SECONDS
) -> tuple[float, bool, dict[str, Any]]:
    """Drain the burst in bounded passes and report what the drain did.

    The wall clock is the point: the store's retry backoff is a wall-clock
    deadline, so a burst's real convergence window is a real wait. Between
    passes it yields rather than spinning, because a hot loop competes with the
    very workers it is waiting on.

    A pass claims `derived_drain.progress_limit()` -- whatever the scheduler's
    own allowance for this cell's mode is, which is what a served drain would
    use. That is a per-machine number, not a constant: `mode.resolve_mode()`
    reads `EXOMEM_MODE`, then `~/.exomem/config.json` (or `EXOMEM_CONFIG_PATH`),
    so a box configured `performance` claims 32 and a default one claims 16.
    Both the mode and the limit are reported, because a convergence number
    measured at 32 and a convergence number measured at 16 are not comparable
    and nothing else in the report would say which was which.

    **The bound is a checkpoint between passes, not a hard stop.** There is no
    intra-pass timeout: `drain_once` dispatches its whole claim synchronously,
    so a bound of 120s is routinely overrun by a pass that takes 200 -- the
    reported `bound_overrun_seconds` says by how much. A `drain_once` that
    wedges outright is therefore not survivable here; the caller's own
    `timeout` is the only thing that ends such a run.

    What the drain saw is reported alongside whether it finished, because
    "did not converge" cannot distinguish the two outcomes an operator acts on
    differently -- a deep backlog that is draining and wants a longer window,
    and a drain that is stuck and wants a person. It stops early only on the
    second of those: an unreadable observation. Retired receipts and retry
    backoff can produce zero completions while the queue still makes progress.
    """
    dispatch = derived_drain.component_dispatcher()
    observe = derived_drain.canonical_generation_observer()
    limit = derived_drain.progress_limit()
    mode_name = mode.resolve_mode()
    started = time.monotonic()
    deadline = started + float(bound_seconds)
    passes = 0
    completed = 0
    stalled_passes = 0
    previous = _drain_observation(vault_root)
    while True:
        passes += 1
        moved = derived_drain.drain_once(
            vault_root,
            dispatch=dispatch,
            observe_current_generation=observe,
            visibility_publisher=pending_recall.publish,
            retire_visibility=derived_drain.pending_visibility_retirer(),
            limit=limit,
        )
        completed += int(moved)
        observation = _drain_observation(vault_root)
        retired = (
            previous["pending"] >= 0
            and 0 <= observation["pending"] < previous["pending"]
        )
        stalled_passes = 0 if moved or retired else stalled_passes + 1
        previous = observation
        try:
            last_pass = derived_drain.last_pass_observation(vault_root)
        except Exception:  # noqa: BLE001 - a report is worth more than a crash
            last_pass = {}
        # After the observation reads, not before: they are part of the pass a
        # rate is being computed over.
        elapsed = time.monotonic() - started
        due = observation["due"]
        drain = {
            "mode": mode_name,
            "limit": limit,
            "passes": passes,
            "completed": completed,
            "due": due,
            "pending": observation["pending"],
            "claimed": observation["claimed"],
            "retrying": observation["retrying"],
            "failed": observation["failed"],
            "stuck": observation["stuck"],
            "max_attempt_count": observation["max_attempt_count"],
            "stalled_passes": stalled_passes,
            "rate_components_per_min": (
                round(completed / elapsed * 60.0, 2) if elapsed > 0 else 0.0
            ),
            "projected_seconds_to_converge": None,
            "bound_overrun_seconds": round(
                max(0.0, elapsed - float(bound_seconds)), 3
            ),
            "oldest_due_age_seconds": last_pass.get("oldest_due_age_seconds"),
        }
        if due > 0 and completed > 0 and elapsed > 0:
            drain["projected_seconds_to_converge"] = round(
                due * elapsed / completed, 1
            )
        # Convergence is an empty queue, not merely an empty DUE queue. A final
        # pass in which every remaining component fails transiently leaves
        # `due == 0` for the length of the backoff, and a row parked under a
        # live lease is invisible to `due` entirely. Reporting "converged" over
        # either is the exact outcome these counters exist to prevent, so they
        # gate the verdict rather than only appearing in the report.
        if observation["pending"] == 0 and due == 0 and observation["claimed"] == 0:
            return round(elapsed, 3), True, drain
        # Retry backoff can resolve within the remaining window. Only an
        # unreadable observation prevents further measurement from being useful.
        if any(observation[key] < 0 for key in ("pending", "due", "stuck", "claimed")):
            return round(elapsed, 3), False, drain
        if time.monotonic() >= deadline:
            return round(elapsed, 3), False, drain
        time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))


def _observe_convergence(
    vault_root: Path, *, bound_seconds: float, started: float | None = None,
) -> tuple[float, bool, dict[str, Any]]:
    """Observe worker-owned receipts without entering the collector runtime."""
    started = time.monotonic() if started is None else started
    deadline = started + float(bound_seconds)
    observations = 0
    while True:
        observation = _drain_observation(vault_root, read_only=True)
        observations += 1
        elapsed = time.monotonic() - started
        drain = {**observation, "owner": "service", "observations": observations,
                 "bound_overrun_seconds": round(max(0.0, elapsed - bound_seconds), 3)}
        readable = all(value >= 0 for value in observation.values())
        settled = readable and observation["pending"] == observation["due"] == observation["claimed"] == 0
        if not readable or settled or time.monotonic() >= deadline:
            return round(elapsed, 3), settled and elapsed <= bound_seconds, drain
        time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))


def measure(
    vault_root: Path,
    *,
    transport: str,
    samples_per_operation: int,
    state_dir: Path,
    connector_url: str | None = None,
    connector_token: str | None = None,
    connector_pid: int | None = None,
    host_network_namespace: str | None = None,
    convergence_bound_seconds: float = CONVERGENCE_BOUND_SECONDS,
    package_mode: str = "installed",
    condition: str = "warm",
    contract_path: Path | None = None,
) -> dict[str, Any]:
    """Measure public acknowledgements, keeping cold and contended cases separate.

    Connector setup and queue progress belong to the separate service process.
    """
    root = Path(vault_root)
    _require_disposable_environment(root, state_dir)
    package = _package_identity(package_mode, role="observer-only" if transport == "connector" else "observer-and-direct-runtime")
    contract, contract_hash = _load_contract(contract_path)
    contract.bar("ordinary-write-warm")
    if samples_per_operation < 1:
        raise SystemExit("acceptance requires a positive sample count")
    if transport == "connector":
        if not connector_url:
            raise SystemExit("connector transport requires --connector-url")
        connector_environment, connector_runtime = _require_disposable_connector(connector_url, connector_pid, host_network_namespace, root, state_dir)
        client: Any = _ConnectorTransport(connector_url, connector_token)
    else:
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    if transport == "direct":
        os.environ["EXOMEM_FAST_DURABLE_ACK"] = "1"
    durations: dict[str, list[float]] = {name: [] for name in OPERATIONS}
    cases: list[dict[str, Any]] = []
    outcomes = {"exact": 0, "warming": 0, "stale": 0, "unknown": 0}
    if transport == "connector":
        outcomes["unavailable"] = 0
    relation_target: str | None = None
    last_acknowledged: float | None = None

    def skipped(name: str, index: int, error: BaseException | None = None) -> None:
        cases.append({
            "case": index, "operation": name, "condition": "cold" if index == 0 else condition,
            "elapsed_ms": None, "terminal_outcome": "skipped", "thread_cpu_ms": None,
            "thread_cpu_source": "unavailable", "phase_attribution": [],
            "error_class": type(error).__name__ if error else None,
        })

    def measured_call(name: str, arguments: dict[str, Any], index: int) -> dict[str, Any] | None:
        nonlocal last_acknowledged
        result = None
        error_class = None
        terminal = "ok"
        started = time.perf_counter()
        client.thread_cpu_ms = None
        client.phase_attribution = []
        client.request_id = None
        try:
            elapsed, result = client.call(name, arguments)
            # These are structured terminal fields, not inferred prose.
            if result.get("outcome") in ("held", "refused") or result.get("ok") is False or result.get("success") is False:
                terminal = "refused"
            else:
                last_acknowledged = time.monotonic()
        except (Exception, SystemExit) as error:  # noqa: BLE001 - retain all timed terminals without their content
            elapsed = (time.perf_counter() - started) * 1_000.0
            terminal = "refused" if isinstance(error, OpError) else "error"
            error_class = type(error).__name__
        cpu_ms = client.thread_cpu_ms
        cases.append({
            "case": index, "operation": name, "condition": "cold" if index == 0 else condition,
            "elapsed_ms": elapsed, "terminal_outcome": terminal,
            "thread_cpu_ms": cpu_ms,
            "thread_cpu_source": "public-boundary-thread" if cpu_ms is not None else "unavailable",
            "phase_attribution": client.phase_attribution,
            "request_id": client.request_id,
            "error_class": error_class,
        })
        if index > 0:
            durations[name].append(elapsed)
        return result if terminal == "ok" else None

    def collection_read(name: str, collection: str, field: str, marker: str) -> str:
        try:
            _, result = client.call(name, {
                "action": "query", "collection": collection,
                "filters": [{"column": field, "op": "eq", "value": marker}],
            })
            if result.get("status") == "warming":
                return "warming"
            if result.get("error") or result.get("success") is False:
                return "unknown"
            return "exact" if any(row.get(field) == marker for row in result["rows"]) else "stale"
        except (Exception, SystemExit):  # noqa: BLE001 - a failed read proves no visibility
            return "unknown"

    served_store = contextlib.ExitStack()
    try:
        if transport == "connector":
            relation_target = _connector_relation_target(client)
        else:
            # A service serves its store and enters managed recall before it takes a
            # write, and the collections below are writes that the store has to take.
            if ownership.file_only(root):
                manager = writer_lease.LeaseManager(writer_lease.LeaseConfig(state_dir=state_dir))
            else:
                manager = served_store.enter_context(_served_store(root, state_dir, package_mode))
            client = _DirectTransport(root, manager)
            recall_admission = str(
                managed_recall.enter_managed_recall(root, full_startup=True).get("state", "unknown")
            )
        example, planning_path = _prepare_collections(client)
        if transport == "connector":
            preparation_seconds, preparation_converged, preparation_drain = _observe_convergence(root, bound_seconds=convergence_bound_seconds)
            projection = due_state.load(root)
            # Count stored buckets, separately from the audience-filtered served view.
            try:
                due_projection_entries = None if projection is None else sum(
                    len(due_state._unbucket(bucket))
                    for pages in projection["categories"].values()
                    for bucket in pages.values()
                )
            except (AttributeError, TypeError, ValueError):
                due_projection_entries = None
            due_items = None
            retrieval = connector_runtime.get("retrieval") or {}
            recall_admission = str(retrieval.get("state", "unverified"))
        else:
            preparation_seconds, preparation_converged, preparation_drain = _converge(
                root, bound_seconds=convergence_bound_seconds
            )
            due_state.reconcile(root)
            projection = due_state.load(root)
            due_items = len(due_state.served_entries(root, recover=False))
            eligible = semantic_contract.build_corpus_context(root).eligible_governed_paths
            relation_target = min(eligible).removesuffix(".md") if eligible else None
        full_receipts_before = _full_receipt_count(root, read_only=transport == "connector")
        # The first public call of each operation is reported as cold; it primes
        # per-operation state before any sample may claim the warm population.
        for index in range(samples_per_operation + 1):
            create_marker = f"acceptanceremember{uuid.uuid4().hex}" if transport == "connector" else f"acceptanceremember{index:04d}"
            result = measured_call("remember", {
                "content": _remember_content(f"Acceptance {index:04d}", create_marker, relation_target),
                "title": f"Acceptance {index:04d}", "note_type": "insight",
                "suggestions": False, "response_detail": "compact",
            }, index)
            if result is None:
                skipped("edit_memory", index)
                outcomes["unknown"] += 2
            else:
                rel_path = str(result["path"])
                if not rel_path.endswith(".md"):
                    rel_path += ".md"
                relation_target = relation_target or rel_path.removesuffix(".md")
                if transport == "connector":
                    outcomes[_connector_read_your_write(client, create_marker, rel_path)] += 1
                else:
                    outcomes[_read_your_write(root, create_marker, rel_path)] += 1
                edit_marker = f"acceptanceedit{uuid.uuid4().hex}" if transport == "connector" else f"acceptanceedit{index:04d}"
                edited = measured_call("edit_memory", {
                    "path": rel_path, "why": "correct disposable acceptance observation",
                    "operation": {"kind": "replace_string", "old_string": create_marker, "new_string": edit_marker},
                }, index)
                if edited is None:
                    outcomes["unknown"] += 1
                elif transport == "connector":
                    outcomes[_connector_read_your_write(client, edit_marker, str(edited.get("path") or rel_path))] += 1
                else:
                    outcomes[_read_your_write(root, edit_marker, rel_path)] += 1

            record_marker = f"acceptancerecord{index:04d}"
            try:
                _, inspected = client.call("record_memory", {"action": "inspect", "collection": example["manifest_path"]})
            except (Exception, SystemExit) as error:  # noqa: BLE001 - skip only the write whose guard is unknown
                skipped("record_memory", index, error)
                outcomes["unknown"] += 1
            else:
                recorded = measured_call("record_memory", {
                    "action": "append", "collection": example["manifest_path"],
                    "item": dict(example["append_item"], label=record_marker), "item_key": str(uuid.uuid4()),
                    "expected_container_hash": inspected["snapshot"], "why": "record disposable acceptance event",
                }, index)
                outcomes[collection_read("record_memory", example["manifest_path"], "label", record_marker) if recorded is not None else "unknown"] += 1

            plan_marker = f"acceptanceplan{index:04d}"
            planned = measured_call("plan_memory", {
                "action": "add", "collection": planning_path,
                "item": {"title": plan_marker}, "why": "save disposable acceptance intent",
            }, index)
            outcomes[collection_read("plan_memory", planning_path, "title", plan_marker) if planned is not None else "unknown"] += 1
    finally:
        if transport == "direct":
            readiness.unmanage_runtime()
        served_store.close()

    if transport == "connector":
        convergence_seconds, converged, drain = _observe_convergence(root, bound_seconds=convergence_bound_seconds, started=last_acknowledged)
    else:
        convergence_seconds, converged, drain = _converge(root, bound_seconds=convergence_bound_seconds)
    full_receipts_after = _full_receipt_count(root, read_only=transport == "connector")
    uncovered = -1 if full_receipts_before < 0 or full_receipts_after < 0 else max(0, full_receipts_after - full_receipts_before)
    return {
        "transport": transport, "package": package, "condition": condition,
        "boundary": "public-write-v1",
        "contract": {"revision": contract.revision, "sha256": contract_hash},
        "connector_environment": connector_environment if transport == "connector" else None,
        "connector_runtime_provenance": connector_runtime if transport == "connector" else None,
        "measurement_scope": "external-http-observer" if transport == "connector" else "helper-in-process-diagnosis",
        "cpu_scope": "server-unmeasured" if transport == "connector" else "in-process-public-boundary-thread",
        "cold_definition": "first-public-call-per-operation-after-product-startup",
        "preparation": {
            "startup": "service-owned" if transport == "connector" else "product-warm-all",
            "runtime_readiness": "externally-unverified" if transport == "connector" else "helper-in-process-diagnosis",
            "convergence_seconds": preparation_seconds,
            "converged": preparation_converged,
            "drain": preparation_drain,
            "due_projection_present": projection is not None,
            **({"due_projection_entries": due_projection_entries} if transport == "connector" else {}),
            "due_items": due_items,
        },
        "samples_per_operation": samples_per_operation, "cases": cases,
        "fast_durable_ack": "externally-unverified" if transport == "connector" else ("active" if writer_lease.fast_durable_ack_active() else "inactive"),
        "operations": {
            name: {"samples": len(values), "p50_ms": statistics.median(values) if values else None,
                   "p90_ms": _percentile(values, 0.90) if values else None,
                   "p95_ms": _percentile(values, 0.95) if values else None,
                   "max_ms": max(values) if values else None}
            for name, values in durations.items()
        },
        "recall_admission": recall_admission, "read_your_write": outcomes,
        "uncovered_full_receipts": uncovered,
        "post_burst_convergence_seconds": convergence_seconds,
        "post_burst_convergence_bound_seconds": float(convergence_bound_seconds),
        "post_burst_converged": converged, "drain": drain,
        "reconciliation_demanded": _reconciliation_demanded(root, read_only=transport == "connector"),
    }


def _convergence_failure(report: dict[str, Any]) -> str:
    """Say which non-convergence this was, because they are not one thing.

    A deep backlog that is draining wants a longer window and a look at what a
    single dispatch costs. A drain that is stuck wants a person: a component
    that is not due and is several attempts deep will not clear by waiting. A
    counter that could not be read is a third thing and must never be rendered
    as either -- "the backlog is draining" is a claim, and an unreadable store
    has not earned it. "Did not converge" is true of all of them and
    actionable for none, and it is what the first live run said.
    """
    bound = float(
        report.get(
            "post_burst_convergence_bound_seconds", CONVERGENCE_BOUND_SECONDS
        )
    )
    headline = f"the post-burst backlog did not converge within {bound:.0f}s"
    drain = report.get("drain")
    if not isinstance(drain, dict):
        return headline
    if drain.get("owner") == "service":
        if any(int(drain.get(key, -1)) < 0 for key in ("pending", "due", "claimed")):
            return f"{headline}: service queue observation was unreadable"
        return f"{headline}: observed {drain['pending']} pending service component(s), {drain['claimed']} under live claim(s)"
    passes = int(drain.get("passes", 0))
    stuck = int(drain.get("stuck", 0))
    failed = int(drain.get("failed", 0))
    due = int(drain.get("due", 0))
    if stuck < 0 or failed < 0 or due < 0:
        return f"{headline}: the drain observation was unreadable"
    if stuck:
        held = (
            f"{stuck} component(s) held back at "
            f"{int(drain.get('max_attempt_count', 0))} attempt(s)"
        )
        if failed:
            held += f", {failed} carrying a failure code"
        return f"{headline}: drain stuck ({held}) after {passes} pass(es)"
    if int(drain.get("stalled_passes", 0)) >= 2:
        return (
            f"{headline}: drain stuck (no component completed in the last "
            f"2 of {passes} pass(es))"
        )
    rate = float(drain.get("rate_components_per_min") or 0.0)
    if rate <= 0.0:
        return (
            f"{headline}: drain stuck (no component completed in "
            f"{passes} pass(es))"
        )
    projected = drain.get("projected_seconds_to_converge")
    detail = (
        "" if projected is None else f", projected {float(projected):.0f}s to drain"
    )
    retrying = int(drain.get("retrying", 0))
    if retrying:
        # Ordinary, and worth saying: it is the visible half of a lease that
        # did not outlive its dispatch.
        detail += f", {retrying} retrying"
    return (
        f"{headline}: deep backlog draining at {rate:g} component(s)/min"
        f"{detail}"
    )


def check(
    report: dict[str, Any],
    *,
    contract: request_latency_contract.LatencyContract | None = None,
) -> None:
    """Fail the acceptance on any breach, naming it without naming content."""
    failures: list[str] = []
    preparation = report.get("preparation")
    if not isinstance(preparation, dict) or not preparation.get("converged"):
        failures.append("fixture custody did not converge before measurement")
    if not isinstance(preparation, dict) or not preparation.get("due_projection_present"):
        failures.append("due projection was not prepared before measurement")
    warm_bar = (contract or request_latency_contract.load_contract()).bar("ordinary-write-warm")
    for name in OPERATIONS:
        measured = report["operations"][name]
        if int(measured["samples"]) < MIN_SAMPLES_PER_OPERATION:
            failures.append(
                f"{name} samples={measured['samples']} < "
                f"{MIN_SAMPLES_PER_OPERATION}"
            )
        if not measured["samples"]:
            failures.append(f"{name} has no timed terminals")
            continue
        if float(measured["p50_ms"]) > P50_MS:
            failures.append(
                f"{name} p50={float(measured['p50_ms']):.1f}ms > {P50_MS:.1f}ms"
            )
        if float(measured["p90_ms"]) > P90_MS:
            failures.append(
                f"{name} p90={float(measured['p90_ms']):.1f}ms > {P90_MS:.1f}ms"
            )
        if report.get("condition") == "warm":
            if int(measured["samples"]) < warm_bar.min_samples:
                failures.append(f"{name} warm sample count below contract minimum")
            threshold = warm_bar.threshold_ms
            p95 = float(measured["p95_ms"])
            if threshold is None or (p95 >= threshold if warm_bar.comparison == "<" else p95 > threshold):
                failures.append(f"{name} warm p95={p95:.1f}ms breaches {warm_bar.id}")
    incomplete = sum(case["terminal_outcome"] != "ok" for case in report.get("cases", []))
    if incomplete:
        failures.append(f"{incomplete} failed or skipped public write(s)")
    unknown_reads = int(report["read_your_write"].get("unknown", 0))
    if unknown_reads:
        failures.append(f"{unknown_reads} read(s) have unknown visibility")
    stale = int(report["read_your_write"]["stale"])
    if stale:
        failures.append(f"{stale} stale read(s) after an acknowledged write")
    uncovered = int(report["uncovered_full_receipts"])
    if uncovered != 0:
        failures.append(
            "uncovered full receipt accounting is "
            + ("unreadable" if uncovered < 0 else f"{uncovered} above zero")
        )
    if not report["post_burst_converged"]:
        failures.append(_convergence_failure(report))
    demanded = int(report.get("reconciliation_demanded", 0))
    if demanded < 0:
        failures.append("reconciliation accounting is unreadable")
    elif demanded:
        failures.append(
            f"{demanded} batch(es) failed closed into reconciliation demand"
        )
    if failures:
        raise SystemExit("live write acceptance failed: " + "; ".join(failures))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transport", choices=TRANSPORTS, required=True)
    parser.add_argument("--package-mode", choices=("installed", "source"), default="installed")
    parser.add_argument("--condition", choices=("warm", "contended"), default="warm")
    parser.add_argument("--contract", type=Path, help="shared controller contract for matched installed versions")
    parser.add_argument(
        "--samples-per-operation", type=int, default=MIN_SAMPLES_PER_OPERATION
    )
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--vault",
        type=Path,
        help="the disposable vault to run against; never resolved from the "
        "environment, because pointing this at a live cell is the one way it "
        "could do harm",
    )
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--connector-url")
    parser.add_argument("--connector-pid", type=int, help="PID of the service in the observer's private network namespace")
    parser.add_argument("--host-network-namespace", help="network namespace readlink observed by the host launcher before isolation")
    parser.add_argument(
        "--convergence-bound",
        type=float,
        default=CONVERGENCE_BOUND_SECONDS,
        help="seconds the post-burst backlog is given to drain; the design's "
        "own oldest-required-component alarm is the default, and a shorter "
        "one narrows the window rather than the assertion -- the run still "
        "fails, and still says which non-convergence it was",
    )
    args = parser.parse_args(argv)

    if args.vault is None:
        raise SystemExit(
            "live write acceptance requires an explicit --vault naming a "
            "disposable vault"
        )
    connector_url = args.connector_url or os.environ.get("EXOMEM_CONNECTOR_URL")
    if args.transport == "connector" and not connector_url:
        raise SystemExit(
            "live write acceptance failed: connector transport requires a "
            "--connector-url or EXOMEM_CONNECTOR_URL"
        )

    report = measure(
        args.vault,
        transport=args.transport,
        samples_per_operation=args.samples_per_operation,
        state_dir=args.state_dir or (args.vault.parent / "acceptance-state"),
        connector_url=connector_url,
        connector_token=os.environ.get("EXOMEM_CONNECTOR_TOKEN"),
        connector_pid=args.connector_pid,
        host_network_namespace=args.host_network_namespace,
        convergence_bound_seconds=args.convergence_bound,
        package_mode=args.package_mode,
        condition=args.condition,
        contract_path=args.contract,
    )
    print(json.dumps(report, sort_keys=True))
    if args.check:
        check(report, contract=_load_contract(args.contract)[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
