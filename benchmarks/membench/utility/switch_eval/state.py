"""Run layout, per-arm state builds and the captured server surface.

Exomem keys a vault's machine-local state by the vault's resolved path
(``state_paths.vault_state_dir``), so state built for one path is not found
under another. Every build and every session therefore runs in one fixed slot
inside the run directory: ``slot/vault`` and ``slot/state``. Each tree keeps
one built vault and state baseline under ``built-trees/<tree>/``. Each arm holds a
copy of machine state. A session moves the original vault into the slot and
returns it unchanged before wiping the slot. Nothing here reads or writes the user's own state root or a running service: every
product location is pinned under the slot (see :func:`product_env`).
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from collections import Counter
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path

from epistemic.journeys.f27_replay import REPO_ROOT, refuse_unsafe_out_dir

from exomem import kbdir
from exomem.governance import receipts
from exomem.runtime_resources import CPU_THREADS_ENV, effective_online_cpus

from . import protocol
from .arms import ArmSpec, arm_specs, canonical_json, profile_settings, sha256_text
from .tap import HANDSHAKE_METHODS
from .tap import __file__ as TAP_SCRIPT

SERVER_NAME = "exomem"
MEDIA_CONVERGENCE_TIMEOUT_SECONDS = 1200.0


class StateError(RuntimeError):
    """A build or a slot that cannot be trusted; the run stops before a session."""


def tree_digest(root: Path) -> str:
    """Digest of every regular file and symlink under ``root``: path, kind and bytes."""

    root = Path(root)
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
        rel = path.relative_to(root).as_posix()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            digest.update(b"L\0" + rel.encode() + b"\0" + os.readlink(path).encode() + b"\0")
        elif stat.S_ISREG(info.st_mode):
            digest.update(b"F\0" + rel.encode() + b"\0")
            with open(path, "rb") as handle:
                for chunk in iter(lambda: handle.read(1 << 20), b""):
                    digest.update(chunk)
            digest.update(b"\0")
    return digest.hexdigest()


def signature_manifest(root: Path) -> dict[str, list[int]]:
    """Stat-only catalog signatures for every regular file and symlink."""

    manifest = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        if stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            manifest[path.relative_to(root).as_posix()] = [
                info.st_mtime_ns,
                info.st_ctime_ns,
                info.st_size,
            ]
    return manifest


def signature_digest(root: Path) -> str:
    return sha256_text(canonical_json(signature_manifest(root)))


def _write_json_atomically(receipt: Path, custody: Mapping) -> None:
    temporary = receipt.with_suffix(".tmp")
    try:
        temporary.write_text(json.dumps(custody), encoding="utf-8")
        os.replace(temporary, receipt)
    finally:
        temporary.unlink(missing_ok=True)


def require_default_kb_dirname() -> None:
    """Refuse an environment whose KB folder name differs from the servers' own."""

    # _child_env strips EXOMEM_*, so every server writes under the default folder name;
    # the harness would look for receipts in another folder and file none of them.
    if kbdir.kb_dirname() != kbdir._DEFAULT:
        raise StateError(
            f"EXOMEM_KB_DIRNAME names {kbdir.kb_dirname()!r} in the harness environment but "
            f"the servers run with {kbdir._DEFAULT!r}; unset it"
        )


def _events_root(path: Path) -> Path:
    """The product's receipt events root under ``path``, refused when it leaves ``path``."""

    try:
        return receipts._validated_events_root(path)
    except receipts.ReceiptError as error:
        raise StateError(f"{error}: {path}") from error


def _receipt_instances(vault: Path) -> list[str]:
    """Names of the receipt instance directories that a vault holds now."""

    root = _events_root(vault)
    if not root.is_dir():
        return []
    return sorted(entry.name for entry in root.iterdir() if entry.is_dir() and not entry.is_symlink())


def _file_new_receipts(vault: Path, known: Collection[str], evidence: Path) -> dict:
    """Move receipt instances created since checkout out of the vault into run evidence.

    An audited read appends a disclosure receipt inside the vault, and each fresh
    server creates its own instance directory. Only whole new directories move;
    a file inside an instance that existed at checkout stays, so the vault check
    still refuses an append to it. The product's chain reader then verifies every
    record under ``evidence``, which keeps the product's mirrored path layout.
    """

    root = _events_root(vault)
    mirror = _events_root(evidence)
    moved = [name for name in _receipt_instances(vault) if name not in known]
    for name in moved:
        if (mirror / name).exists():
            raise StateError(
                f"receipt instance {name} is already filed under {evidence}; preserved vault at {vault}"
            )
        mirror.mkdir(parents=True, exist_ok=True)
        shutil.move(str(root / name), str(mirror / name))
    try:
        records = receipts.event_records(evidence)
    except receipts.ReceiptError as error:
        raise StateError(
            f"filed receipts under {evidence} failed chain verification: {error}; "
            f"preserved vault at {vault}"
        ) from error
    filed = [record for record in records if record["instance_id"] in moved]
    return {
        "evidence": str(evidence),
        "instances": len(moved),
        "records": len(filed),
        "by_event_type": dict(sorted(Counter(record["event_type"] for record in filed).items())),
    }


@dataclass(frozen=True)
class Layout:
    """Every path the run writes, all under one run directory."""

    run: Path

    @classmethod
    def at(cls, run: Path) -> Layout:
        # The agent CLI discovers memory files by walking up from its cwd, which
        # is the slot; the shared refusal keeps the run outside any repository.
        return cls(refuse_unsafe_out_dir(Path(run)))

    @property
    def slot(self) -> Path:
        return self.run / "slot"

    def inside(self, path: Path) -> Path:
        """``path``, refused unless it resolves under the run directory: a name
        that comes from a key or a case file never places a write elsewhere."""

        if not path.resolve().is_relative_to(self.run.resolve()):
            raise StateError(f"{path} resolves outside the run directory {self.run}")
        return path

    def arm_tree(self, arm: str, tree: str) -> Path:
        return self.inside(self.run / "arms" / arm / tree)

    def shared_tree(self, tree: str) -> Path:
        return self.inside(self.run / "built-trees" / tree)

    def arm_dir(self, arm: str) -> Path:
        return self.inside(self.run / "arms" / arm)

    @property
    def sessions(self) -> Path:
        return self.run / "sessions"

    def session_dir(self, case_id: str, arm: str, repeat: int) -> Path:
        """A session's directory, named by a digest of its plan entry, so no
        case id reaches a path."""

        name = hashlib.sha256(json.dumps([case_id, arm, repeat]).encode()).hexdigest()[:16]
        return self.inside(self.sessions / f"session-{name}")

    def receipts_dir(self, session: str) -> Path:
        """Where a session's receipts are filed, named by a digest of the session id."""

        name = hashlib.sha256(session.encode()).hexdigest()[:16]
        return self.inside(self.run / "receipts" / f"session-{name}")

    @property
    def judge(self) -> Path:
        return self.run / "judge"

    @property
    def audit(self) -> Path:
        return self.run / "audit"

    @property
    def unblinding(self) -> Path:
        return self.run / "unblinding"

    @property
    def score(self) -> Path:
        return self.run / "score"


@dataclass(frozen=True)
class Slot:
    root: Path

    @property
    def vault(self) -> Path:
        return self.root / "vault"

    @property
    def state(self) -> Path:
        return self.root / "state"

    @property
    def runtime(self) -> Path:
        return self.root / "runtime"

    @property
    def cwd(self) -> Path:
        return self.root / "cwd"

    @property
    def hook_home(self) -> Path:
        return self.root / "hook-home"

    @property
    def turn_file(self) -> Path:
        return self.runtime / "turn"

    def wipe(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)


def product_env(slot: Slot, spec: ArmSpec) -> dict[str, str]:
    """Every Exomem location pinned under the slot, plus the arm's packet mode
    and the run's retrieval profile.

    The profiles are the existing harnesses' own (see ``profile_settings``). The
    service-credential file points at a path that never exists, so the hook's
    REST and local rungs find no key and cannot reach a running service.
    """

    rt = slot.runtime
    return {
        "EXOMEM_VAULT_PATH": str(slot.vault),
        "EXOMEM_STATE_ROOT": str(slot.state),
        "EXOMEM_CONFIG_PATH": str(rt / "exomem-config.json"),
        "EXOMEM_LOG_DIR": str(rt / "logs"),
        "EXOMEM_WRITER_LEASE_STATE_DIR": str(rt / "leases"),
        "EXOMEM_CALL_LEDGER_DIR": str(rt / "call-ledger"),
        "EXOMEM_LEASE_COORDINATOR_DB": str(rt / "lease-coordinator.sqlite"),
        "EXOMEM_SERVICE_ENV": str(rt / "no-service.env"),
        "EXOMEM_HOOK_HOME": str(slot.hook_home),
        protocol.PACKET_SETTING: spec.packet_mode,
        **profile_settings(spec.profile),
    }


def python_executable() -> str:
    """The build's interpreter: the server, the tap and the builder all run on it."""

    return sys.executable


#: The product's settings, and the legacy ``KB_*`` names it promotes to them at
#: import: an inherited one would change what a child runs with.
PRODUCT_ENV_PREFIXES = ("EXOMEM_", "KB_")


def _child_env(parent: Mapping[str, str], slot: Slot, spec: ArmSpec) -> dict[str, str]:
    env = {key: value for key, value in parent.items() if not key.startswith(PRODUCT_ENV_PREFIXES)}
    env.update(product_env(slot, spec))
    env["PYTHONPATH"] = str(REPO_ROOT / "benchmarks")
    return env


def prepare_build_slot(
    layout: Layout, source_vault: Path, *, move_vault: bool = False, custody: Mapping | None = None
) -> Slot:
    recover_session_vault(layout)
    slot = Slot(layout.slot)
    slot.wipe()
    for path in (slot.runtime, slot.cwd, slot.hook_home):
        path.mkdir(parents=True)
    if custody is not None:
        _write_json_atomically(layout.run / "slot-vault.json", custody)
    if move_vault:
        _rename_vault(source_vault, slot.vault)
    else:
        shutil.copytree(source_vault, slot.vault, symlinks=True)
    return slot


def converge_media(vault: Path) -> dict:
    """Run complete product discovery passes and drain admitted media work to a fixed point."""

    from exomem import media_processing, server_runtime

    passes = []
    written: set[str] = set()
    deadline = time.monotonic() + MEDIA_CONVERGENCE_TIMEOUT_SECONDS
    worker = None
    try:
        while True:
            # Extraction can add media. Cover every current candidate on each pass.
            limit = max(1, sum(1 for path in vault.rglob("*") if path.is_file()))
            before = signature_manifest(vault)
            if not passes:
                worker = server_runtime._start_media_worker(vault)
            attempted = media_processing.reconcile_all_media(
                vault, limit=limit, propagate_transient_errors=True
            )
            if worker is not None:
                worker.join(timeout=max(0.01, deadline - time.monotonic()))
            after = signature_manifest(vault)
            changed = {
                name for name in before.keys() | after.keys() if before.get(name) != after.get(name)
            }
            written.update(changed)
            passes.append({"attempted": attempted, "files_written": len(changed)})
            if not changed:
                break
            if time.monotonic() >= deadline:
                raise StateError("media did not converge before the build deadline")
    finally:
        if worker is not None:
            worker.stop()
    return {"passes": passes, "files_written": len(written)}


def build_state(vault: Path) -> dict:
    """Runs in the builder child: the product's own fresh bootstrap, then the
    derived indexes the existing harness publishes, then the context catalogue.

    The same build for every arm: the arms differ in packet mode at serve time,
    not in what the vault holds.
    """

    from exomem import (
        embeddings,
        freshness,
        index_paths,
        recall_migration,
        recall_policy,
        state_migration,
    )
    from exomem.context_catalogue import ContextCatalogue
    from membench.utility.context_activation_product import publish

    started = time.monotonic()
    state_migration.require_vault_state_ready(vault)
    convergence = converge_media(vault)
    final_content = tree_digest(vault)
    publication = publish(vault)
    vector_started = time.monotonic()
    vector_state = recall_migration.run(vault, threading.Event())
    sidecar = index_paths.active_sidecar_name(vault)
    if (
        vector_state != "current"
        or sidecar is None
        or not index_paths.sidecar_path(vault).is_file()
    ):
        raise StateError(f"vector publication did not complete: {vector_state}, sidecar={sidecar}")
    vector_status = recall_migration.status(vault)
    # The product counter includes copied vectors as well as new encodes.
    processed = vector_status.pop("chunks_encoded", None)
    # Census and pruning precede proofs: mtime slack alone cannot certify current text.
    incremental = embeddings.index_incremental(vault)
    parents = recall_policy.partition_markdown_paths(
        vault, (path for path in index_paths.iter_index_markdown(vault)
                if index_paths.is_embeddable_path(path))
    )
    if parents.invalid_paths or parents.missing_paths:
        raise StateError("final vector census contains unavailable parents")
    proofs = []
    freshly_published = 0
    for parent in parents.admitted_paths:
        proof = embeddings.reconstruct_publication(vault, parent.path)
        if proof is None:
            status = embeddings.upsert_after_write_status(vault, [parent.path], defer_during_warm=False)
            if status.status != "completed" or status.eligible_count != 1:
                raise StateError(f"final vector publication did not complete: {status.code}")
            # Completed writes publish both projections; local writers need no volatile proof.
            parent.guard.recheck(vault)
            if freshness.stat_signature(parent.path) != parent.signature:
                raise StateError("final vector source changed during publication")
            freshly_published += 1
        elif not proof.current(vault):
            raise StateError("final parent lacks exact vector coverage")
        else:
            proofs.append(proof)
    vectors = {
        **vector_status,
        "chunks_processed": processed,
        "incremental": incremental,
        "coverage": {
            "final_parents": len(parents.admitted_paths),
            "reconstructed_parents": len(proofs),
            "freshly_published_parents": freshly_published,
        },
        "seconds": round(time.monotonic() - vector_started, 3),
    }
    context_catalogue = ContextCatalogue(vault)
    catalogue = context_catalogue.rebuild()
    freshness = stamp_serving_freshness(vault, context_catalogue)
    result = {
        "vault_digest": final_content,
        "product_source": product_source_identity(),
        "convergence": convergence,
        "vectors": vectors,
        "anchors": len(publication.anchors),
        "indexed_pages": publication.indexed_pages,
        "catalogue": {
            key: value for key, value in catalogue.items() if isinstance(value, (int, str))
        },
        "owner_coverage": context_catalogue.owner_coverage(),
        "serving_freshness": freshness,
    }
    if (
        tree_digest(vault) != final_content
        or not parents.revalidate(vault)
        or any(not proof.current(vault) for proof in proofs)
    ):
        raise StateError("vault or vector publication changed during final coverage proof")
    result["seconds"] = round(time.monotonic() - started, 2)
    return result


def stamp_serving_freshness(vault: Path, context_catalogue) -> dict:
    """Stamp the activation index and the catalogue with the key a session's server will ask for.

    Every session serves a cold copy of this state. An unstamped sidecar reads as stale on
    every call and schedules a catch-up walk that the copy then discards, so the map arm
    would always serve a stale, turn-independent map.
    """
    from exomem import find, working_set_index, working_set_runtime

    # The same key and serialisation that op_activate_context passes to the sidecars.
    key = working_set_runtime._key_text(find.FreshnessSnapshot(vault).projection_key("kb"))
    if not key:
        raise StateError("no serving freshness key for the built vault")
    index = working_set_index.WorkingSetIndex(vault)
    try:
        # The index first: the catalogue mirrors the index token that it reads when it updates.
        index.update(freshness_stamp=key)
        index_stamp, index_token = index.freshness_stamp(), json.dumps(list(index.token()))
    finally:
        index.close()
    context_catalogue.update(freshness_stamp=key)
    report = {
        "key_sha256": sha256_text(key),
        "index_stamped": index_stamp == key,
        "catalogue_stamped": context_catalogue.freshness_stamp() == key,
        "token_mirrored": context_catalogue.mirrored_activation_token() == index_token,
        "catalogue_pending": len(context_catalogue.pending()),
    }
    stamped = report["index_stamped"] and report["catalogue_stamped"] and report["token_mirrored"]
    if not stamped or report["catalogue_pending"]:
        raise StateError(f"serving freshness was not stamped: {report}")
    return report


def product_source_identity() -> dict:
    """Installed origin and Git revision, separate from the harness content pin."""
    from exomem.install_info import package_identity

    return package_identity("source", role="switch-eval-state-builder")


def mcp_server_entry(
    slot: Slot,
    spec: ArmSpec,
    *,
    log: Path,
    server_stderr: Path,
    skip_ready_gate: bool = False,
    startup_marker: Path | None = None,
) -> dict:
    """The ``--mcp-config`` server entry: the tap in front of the stdio server."""

    return {
        "command": python_executable(),
        "args": [
            TAP_SCRIPT,
            "--log",
            str(log),
            "--turn-file",
            str(slot.turn_file),
            "--budget",
            str(protocol.PULL_BUDGET_PER_TURN),
            "--allow",
            ",".join(spec.tools),
            "--server-stderr",
            str(server_stderr),
            *(["--skip-ready-gate"] if skip_ready_gate else []),
            "--",
            python_executable(),
            "-m",
            *(
                [
                    "membench.utility.switch_eval",
                    "startup-proof-server",
                    "--marker",
                    str(startup_marker),
                ]
                if startup_marker is not None
                else ["exomem", "--transport", "stdio"]
            ),
        ],
        "env": product_env(slot, spec),
    }


_CLIENT_INFO = {"name": "switch-eval", "version": "1"}
_ENVELOPE = {
    "_meta": {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientInfo": _CLIENT_INFO,
        "io.modelcontextprotocol/clientCapabilities": {},
    }
}
#: Per handshake in the tap's HANDSHAKE_METHODS: its params, then the params of
#: the ``tools/list`` that follows it. The protocol versions are the MCP
#: revisions that each handshake belongs to; the stateless one sends its
#: envelope on every request.
_HANDSHAKES: dict[str, tuple[dict, dict]] = {
    "initialize": (
        {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": _CLIENT_INFO},
        {},
    ),
    "server/discover": (_ENVELOPE, _ENVELOPE),
}


def _ask_surface(
    slot: Slot,
    spec: ArmSpec,
    *,
    env: Mapping[str, str],
    out: Path,
    method: str,
    startup_proof: bool = False,
) -> tuple[str, list[dict]]:
    """One connection: the handshake ``method``, then ``tools/list``.

    The server serves one protocol era per connection, so each handshake
    gets its own server.
    """

    handshake_params, list_params = _HANDSHAKES[method]
    name = method.replace("/", "-")
    stderr = out / f"surface-{name}-server.stderr"
    marker = out / "startup-complete.json"
    if startup_proof:
        marker.unlink(missing_ok=True)
    entry = mcp_server_entry(
        slot,
        spec,
        log=out / f"surface-{name}-tap.jsonl",
        server_stderr=stderr,
        skip_ready_gate=not startup_proof,
        startup_marker=marker if startup_proof else None,
    )
    proc = subprocess.Popen(  # noqa: S603 - the harness's own tap and server
        [entry["command"], *entry["args"]],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env={**env, **entry["env"]},
        cwd=slot.cwd,
        start_new_session=True,
    )
    responses: queue.Queue[str | None] = queue.Queue()

    def pump() -> None:
        try:
            for line in proc.stdout:
                responses.put(line)
        finally:
            responses.put(None)

    threading.Thread(target=pump, daemon=True).start()
    from .tap import READY_TIMEOUT_SECONDS

    deadline = time.monotonic() + READY_TIMEOUT_SECONDS + 60

    def send(message: dict) -> None:
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
        proc.stdin.flush()

    def ask(request_id: int, request: str, params: dict) -> dict:
        send({"id": request_id, "method": request, "params": params})
        while True:
            try:
                line = responses.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty as error:
                raise StateError(f"server setup timed out; see {stderr}") from error
            if line is None:
                break
            message = json.loads(line)
            if message.get("id") == request_id:
                if "error" in message:
                    raise StateError(
                        f"{request} failed while capturing the surface: {message['error']}"
                    )
                return message["result"]
        raise StateError(f"the server closed before answering {request} (see {stderr})")

    try:
        answered = ask(1, method, handshake_params)
        if method == "initialize":
            send({"method": "notifications/initialized"})
        tools = ask(2, "tools/list", list_params)["tools"]
        if startup_proof:
            while not marker.exists():
                if proc.poll() is not None or time.monotonic() >= deadline:
                    raise StateError(f"server startup did not finish; see {stderr}")
                time.sleep(0.05)
            observed = json.loads(marker.read_text(encoding="utf-8"))
            if observed.get("outcome") != "complete":
                raise StateError(f"server startup failed: {observed}; see {stderr}")
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            raise StateError(f"server shutdown timed out; see {stderr}") from None
    if proc.returncode != 0:
        raise StateError(f"server exited {proc.returncode}; see {stderr}")
    return answered.get("instructions") or "", tools


def capture_surface(slot: Slot, spec: ArmSpec, *, env: Mapping[str, str], out: Path) -> dict:
    """Ask the server, through the tap, what the arm's actor will be shown.

    Each handshake the tap knows, then ``tools/list``, with no model in the
    loop. The hashes cover each handshake's instructions and filtered tools
    exactly as relayed, so a session is compared on the handshake its client
    sent.
    """

    out.mkdir(parents=True, exist_ok=True)
    slot.turn_file.write_text("-1", encoding="utf-8")
    surface: dict[str, dict] = {"server_instructions": {}, "tool_descriptions": {}, "per_tool": {}}
    shown: dict[str, dict] = {}
    for method in sorted(HANDSHAKE_METHODS):
        instructions, tools = _ask_surface(slot, spec, env=env, out=out, method=method)
        by_name = {tool["name"]: tool for tool in tools}
        if sorted(by_name) != sorted(spec.tools):
            raise StateError(
                f"arm {spec.arm_id}: after {method} the server offers {sorted(by_name)}, "
                f"the arm needs {sorted(spec.tools)}"
            )
        listed = [by_name[name] for name in sorted(by_name)]
        surface["server_instructions"][method] = sha256_text(instructions)
        surface["tool_descriptions"][method] = sha256_text(canonical_json(listed))
        surface["per_tool"][method] = {
            name: sha256_text(canonical_json(by_name[name])) for name in sorted(by_name)
        }
        shown[method] = {"instructions": instructions, "tools": listed}
    (out / "surface.json").write_text(
        json.dumps({"hashes": surface, "shown": shown}, indent=2), encoding="utf-8"
    )
    return surface


def startup_proof_server(marker: Path) -> int:
    """Observe exact product activation and its admitted media queue in the probe only."""

    from exomem import server_runtime
    from exomem.__main__ import main

    from .tap import READY_TIMEOUT_SECONDS

    activate = server_runtime.LocalRuntimeActivation._activate

    def observed(owner) -> None:
        try:
            activate(owner)
            if owner._shutdown.is_set():
                raise RuntimeError("startup stopped before completion")
            if owner.media_worker is not None:
                owner.media_worker.join(timeout=READY_TIMEOUT_SECONDS)
            _write_json_atomically(marker, {"outcome": "complete"})
        except BaseException as error:
            _write_json_atomically(marker, {"outcome": "failed", "error": type(error).__name__})
            raise

    # No product completion signal exists; this observer changes no activation work.
    server_runtime.LocalRuntimeActivation._activate = observed
    return main(["--transport", "stdio"])


def prove_quiescence(
    slot: Slot, spec: ArmSpec, *, env: Mapping[str, str], out: Path, before: Mapping
) -> dict:
    """Admit a fresh server, finish startup and media work, then check the stopped vault."""

    started = time.monotonic()
    known = _receipt_instances(slot.vault)
    _ask_surface(slot, spec, env=env, out=out, method="server/discover", startup_proof=True)
    # The tap's readiness probe is an audited read, so a governed vault gains a receipt instance.
    filed = _file_new_receipts(slot.vault, known, out / "receipts")
    after = signature_manifest(slot.vault)
    prior = before["vault_signatures"]
    counts: dict[str, int] = {}
    for name in prior.keys() | after.keys():
        if prior.get(name) != after.get(name):
            extension = Path(name).suffix.lower() or "(none)"
            counts[extension] = counts.get(extension, 0) + 1
    report = {
        "content_unchanged": tree_digest(slot.vault) == before["vault_digest"],
        "signatures_unchanged": after == prior,
        "changed_files_by_extension": dict(sorted(counts.items())),
        "receipts_filed": filed,
        "seconds": round(time.monotonic() - started, 3),
    }
    (out / "quiescence.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    if not report["content_unchanged"] or not report["signatures_unchanged"]:
        raise StateError(
            f"fresh server changed the vault; changed files by extension: {report['changed_files_by_extension']}; preserved {slot.vault}"
        )
    return report


def validate_build_cpu_threads(cpu_threads: int | None) -> None:
    available = effective_online_cpus()
    if cpu_threads is not None and not 1 <= cpu_threads <= available:
        raise StateError(f"--cpu-threads must be between 1 and {available}")


def require_checkpoint_selection(layout: Layout, checkpoint_from: Path | None) -> dict | None:
    """Refuse an implicit destructive retry before ordinary custody recovery."""
    path = layout.run / "checkpoint-import.json"
    if not path.exists():
        if checkpoint_from is not None:
            raise StateError("prepare --import-checkpoint-from before importing checkpoint state")
        return None
    try:
        intent = json.loads(path.read_bytes())
        # Version 1 fixes this import protocol, rather than a vault vocabulary.
        valid = intent["version"] == 1 and intent["successor_run"] == str(layout.run.resolve())
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise StateError("checkpoint import intent is unreadable; preserved the successor") from error
    if not valid:
        raise StateError("checkpoint import intent does not match the successor")
    if checkpoint_from is None:
        raise StateError("checkpoint import requires --checkpoint-from before retry")
    if str(Path(checkpoint_from).resolve()) != intent["archive_run"]:
        raise StateError("checkpoint selection differs from the recorded import")
    return intent


def prepare_checkpoint_import(layout: Layout, archive_run: Path, *, harness_digest: str) -> dict:
    """Pin an unproven archive before copying its opaque experimental inputs."""
    from exomem import state_migration, state_paths

    archive = Layout.at(archive_run)
    successor, predecessor = layout.run.resolve(), archive.run.resolve()
    if predecessor.is_relative_to(successor) or successor.is_relative_to(predecessor):
        raise StateError("checkpoint archive and successor run roots must be disjoint")
    inputs_bytes = (archive.run / "inputs.json").read_bytes()
    inputs = json.loads(inputs_bytes)
    try:
        imported_protocol, discovery_projection = protocol.rebind_discovery(inputs.get("protocol"))
    except ValueError as error:
        raise StateError("checkpoint recorded protocol cannot bind the current command registry") from error
    if inputs.get("design") != protocol.DESIGN or imported_protocol != protocol.snapshot():
        raise StateError("checkpoint recorded protocol differs from the current implementation")
    try:
        retrieval = inputs["retrieval_profile"]
        specs = arm_specs(hook_mode=inputs["hook_mode"], profile=retrieval["name"])
        current_retrieval = {"name": retrieval["name"], "settings": profile_settings(retrieval["name"])}
    except (KeyError, TypeError, ValueError) as error:
        raise StateError("checkpoint retrieval settings or hook mode are unsupported") from error
    if retrieval != current_retrieval:
        raise StateError("checkpoint retrieval settings differ from the current implementation")
    selected = inputs.get("arms")
    if (
        not isinstance(selected, list) or not selected
        or any(not isinstance(arm, str) or arm not in specs for arm in selected)
        or len(set(selected)) != len(selected)
    ):
        raise StateError("checkpoint arms differ from the current protocol")
    intent_path = layout.run / "checkpoint-import.json"
    if not intent_path.exists() and layout.run.exists() and any(layout.run.iterdir()):
        raise StateError("checkpoint import requires an empty successor run")
    trees = {}
    for tree, source in inputs["trees"].items():
        frozen = Path(source["path"]).resolve()
        if frozen.is_relative_to(successor) or frozen.is_relative_to(predecessor):
            raise StateError("the frozen source must be outside the archive and successor roots")
        if tree_digest(frozen) != source["digest"]:
            raise StateError(f"tree {tree}: frozen source differs from predecessor inputs")
        baseline = archive.shared_tree(tree)
        if (baseline / "state.json").exists():
            raise StateError(f"tree {tree}: completed donors require --seed-build-from")
        retained = baseline / "state"
        if not retained.is_dir():
            raise StateError(f"tree {tree}: checkpoint has no retained baseline machine state")
        vault = Slot(layout.slot).vault
        identity = state_paths.vault_state_key(vault)
        try:
            manifest = state_migration._load_manifest(retained / identity, vault_root=vault)
        except state_migration.StateMigrationManifestError as error:
            raise StateError(f"tree {tree}: checkpoint does not match the original slot") from error
        if manifest is None or manifest["state"] != "complete":
            raise StateError(f"tree {tree}: checkpoint requires the original slot migration manifest")
        evidence = {}
        # These filenames belong to the fixed preparation evidence protocol.
        for name in ("build.stdout", "build.stderr", "quiescence.json"):
            path = baseline / name
            if path.exists():
                evidence[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        trees[tree] = {
            "source_vault": str(frozen), "snapshot_digest": source["digest"],
            "state_digest": tree_digest(retained), "vault_state_key": identity,
            "evidence": evidence,
        }
    opaque = {name: (archive.run / name).read_bytes() for name in ("cases.json", "plan.json")}
    intent = {
        "version": 1, "archive_run": str(predecessor), "successor_run": str(successor),
        "predecessor_inputs_digest": hashlib.sha256(inputs_bytes).hexdigest(),
        "predecessor_harness_digest": inputs["harness_digest"],
        "opaque_inputs": {name: hashlib.sha256(data).hexdigest() for name, data in opaque.items()},
        "harness_digest": harness_digest, "product_source": product_source_identity(),
        "discovery_projection": discovery_projection,
        "trees": trees,
    }
    if "product_source" in inputs:
        intent["predecessor_product_source"] = inputs["product_source"]
    if "checkpoint_predecessor" in inputs:
        intent["predecessor_checkpoint"] = inputs["checkpoint_predecessor"]
    if intent_path.exists() and json.loads(intent_path.read_bytes()) != intent:
        raise StateError("checkpoint import differs from its recorded intent")
    layout.run.mkdir(parents=True, exist_ok=True)
    # An interruption can restart these copies only after validating the immutable archive.
    _write_json_atomically(intent_path, intent)
    for name, data in opaque.items():
        (layout.run / name).write_bytes(data)
    successor_inputs = {
        **inputs, "harness_digest": harness_digest, "product_source": intent["product_source"],
        "protocol": imported_protocol,
        "checkpoint_predecessor": intent,
    }
    _write_json_atomically(layout.run / "inputs.json", successor_inputs)
    return successor_inputs


def _prepare_checkpoint_slot(layout: Layout, *, tree: str, source_vault: Path, intent: dict) -> Slot:
    """Restart from retained state and the frozen source, never a dirty successor."""
    checkpoint = intent["trees"][tree]
    source = Path(source_vault).resolve()
    archive = Layout.at(Path(intent["archive_run"]))
    retained = archive.shared_tree(tree) / "state"
    if str(source) != checkpoint["source_vault"] or tree_digest(source) != checkpoint["snapshot_digest"]:
        raise StateError(f"tree {tree}: checkpoint frozen source changed")
    if tree_digest(retained) != checkpoint["state_digest"]:
        raise StateError(f"tree {tree}: retained checkpoint state changed")
    # The intent authorizes replacing only this import's partial successor copies.
    (layout.run / "slot-vault.json").unlink(missing_ok=True)
    target = layout.shared_tree(tree)
    for name in ("state", "vault"):
        path = target / name
        if path.exists():
            shutil.rmtree(path)
    slot = prepare_build_slot(layout, source)
    shutil.copytree(retained, slot.state, symlinks=True)
    if tree_digest(slot.state) != checkpoint["state_digest"]:
        raise StateError(f"tree {tree}: checkpoint copy differs from the retained state")
    return slot


def _prepare_build_seed(
    layout: Layout, spec: ArmSpec, *, tree: str, snapshot_digest: str, donor_run: Path
) -> tuple[dict, dict, Path]:
    """Validate retained donor evidence before moving its original vault."""

    from exomem import state_migration, state_paths

    donor = Layout.at(donor_run)
    # Preparation wipes successor paths; neither run may contain the donor's evidence.
    if donor.run.resolve().is_relative_to(layout.run.resolve()) or layout.run.resolve().is_relative_to(donor.run.resolve()):
        raise StateError("the build donor and successor run roots must be disjoint")
    # Operational sequencing excludes live donors; receipt absence cannot prove that.
    if (donor.run / "slot-vault.json").exists():
        raise StateError("recover the donor's pending custody before archiving its build")
    baseline, target = donor.shared_tree(tree), layout.shared_tree(tree)
    try:
        metadata = (baseline / "state.json").read_bytes()
        record = json.loads(metadata)
    except (OSError, ValueError) as error:
        raise StateError(f"tree {tree}: donor build metadata is missing or unreadable") from error
    _require_build_proof(record)
    if record["snapshot_digest"] != snapshot_digest or record["profile"] != profile_settings(spec.profile):
        raise StateError(f"tree {tree}: seed snapshot or retrieval profile does not match")
    if tree_digest(baseline / "state") != record["state_digest"]:
        raise StateError(f"tree {tree}: donor state does not match its build digest")
    vault = Slot(layout.slot).vault
    identity = state_paths.vault_state_key(vault)
    try:
        manifest = state_migration._load_manifest(baseline / "state" / identity, vault_root=vault)
    except state_migration.StateMigrationManifestError as error:
        raise StateError(f"tree {tree}: seed manifest does not match the successor slot") from error
    if manifest is None or manifest["state"] != "complete":
        raise StateError(f"tree {tree}: seed requires a complete migration manifest for the successor slot")
    intent = {
        "donor_run": str(donor.run),
        "donor_metadata_digest": hashlib.sha256(metadata).hexdigest(),
        "donor_state_digest": record["state_digest"],
        "vault_state_key": identity,
    }
    pending = target / "seed-build.json"
    if pending.exists() and json.loads(pending.read_text(encoding="utf-8")) != intent:
        raise StateError(f"tree {tree}: pending seed differs from the retained donor evidence")
    original, returned = baseline / "vault", target / "vault"
    if original.exists() == returned.exists() or (returned.exists() and not pending.exists()):
        raise StateError(f"tree {tree}: seed requires exactly one original vault at its recorded location")
    location = original if original.exists() else returned
    if tree_digest(location) != record["vault_digest"] or signature_digest(location) != record["vault_signature_digest"]:
        raise StateError(f"tree {tree}: original seed vault differs from its build content or signatures")
    target.mkdir(parents=True, exist_ok=True)
    # Persist provenance before the first rename, including an interrupt before slot custody exists.
    _write_json_atomically(pending, intent)
    if original.exists():
        _rename_vault(original, returned)
    return record, intent, baseline / "state"


def build_tree(
    layout: Layout,
    spec: ArmSpec,
    *,
    tree: str,
    source_vault: Path,
    parent_env: Mapping[str, str],
    cpu_threads: int | None = None,
    seed_build_from: Path | None = None,
    checkpoint_from: Path | None = None,
) -> dict:
    """Publish one tree baseline; packet mode does not affect index construction."""

    validate_build_cpu_threads(cpu_threads)
    checkpoint = require_checkpoint_selection(layout, checkpoint_from)
    if checkpoint is not None and seed_build_from is not None:
        raise StateError("checkpoint import and proven donor seeding are distinct modes")
    if checkpoint is None:
        recover_session_vault(layout)
    target = layout.shared_tree(tree)
    pending_seed = target / "seed-build.json"
    if pending_seed.exists() and seed_build_from is None:
        raise StateError(f"tree {tree}: pending seed requires --seed-build-from before retry")
    snapshot_digest = tree_digest(source_vault)
    profile = profile_settings(spec.profile)
    if (target / "state.json").exists():
        record = json.loads((target / "state.json").read_text(encoding="utf-8"))
        _require_build_proof(record)
        if record["snapshot_digest"] != snapshot_digest or record["profile"] != profile:
            raise StateError(
                f"tree {tree}: snapshot or retrieval profile changed; build a fresh run"
            )
        if pending_seed.exists():
            _prepare_build_seed(
                layout, spec, tree=tree, snapshot_digest=snapshot_digest,
                donor_run=seed_build_from,
            )
            pending_seed.unlink()
        return record
    resumed = target.exists() or layout.slot.exists()
    seed = None
    if seed_build_from is not None:
        seed = _prepare_build_seed(
            layout, spec, tree=tree, snapshot_digest=snapshot_digest, donor_run=seed_build_from
        )
    if checkpoint is None and (target / "state").exists():
        shutil.rmtree(target / "state")
    # An interrupted proof may have returned the original here. Reuse it, never copy it.
    returned = target / "vault"
    slot = Slot(layout.slot)
    custody = None if seed is None else {
        "arm": spec.arm_id, "tree": tree, "session": f"seed-{tree}",
        "digest": seed[0]["vault_digest"], "signatures": seed[0]["vault_signature_digest"],
        "shared": True,
    }
    try:
        if checkpoint is not None:
            slot = _prepare_checkpoint_slot(layout, tree=tree, source_vault=source_vault, intent=checkpoint)
        else:
            slot = prepare_build_slot(
                layout, returned if returned.exists() else source_vault,
                move_vault=returned.exists(), custody=custody,
            )
        if seed is not None:
            shutil.copytree(seed[2], slot.state, symlinks=True)
            if tree_digest(slot.state) != seed[0]["state_digest"]:
                raise StateError(f"tree {tree}: seeded state copy differs from the retained donor")
        env = _child_env(parent_env, slot, spec)
        # Only publication gets the speed override; all server proofs keep serving defaults.
        build_env = dict(env)
        if cpu_threads is not None:
            build_env[CPU_THREADS_ENV] = str(cpu_threads)
        target.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        completed = subprocess.run(  # noqa: S603 - the harness's own builder entry point
            [
                python_executable(),
                "-m",
                "membench.utility.switch_eval",
                "build-state",
                "--vault",
                str(slot.vault),
            ],
            env=build_env,
            cwd=slot.cwd,
            capture_output=True,
            text=True,
            timeout=None,
        )
        (target / "build.stdout").write_text(completed.stdout, encoding="utf-8")
        (target / "build.stderr").write_text(completed.stderr, encoding="utf-8")
        if completed.returncode != 0:
            raise StateError(f"tree {tree}: state build exited {completed.returncode}; see {target}")
        build = json.loads(completed.stdout.strip().splitlines()[-1])
        # The first arm's surface is captured before freezing the vault, as the protocol requires.
        published_content = build.get("vault_digest")
        if not isinstance(published_content, str):
            raise StateError(f"tree {tree}: state builder lacks final vault coverage identity")
        if tree_digest(slot.vault) != published_content:
            raise StateError(f"tree {tree}: vault changed after final vector coverage; preserved {slot.vault}")
        observed_source = build.get("product_source")
        if checkpoint is not None and (
            not isinstance(observed_source, dict) or observed_source != checkpoint["product_source"]
        ):
            raise StateError(f"tree {tree}: builder product source is missing or differs from checkpoint intent")
        surface_out = layout.arm_tree(spec.arm_id, tree)
        surface = capture_surface(slot, spec, env=env, out=surface_out)
        if tree_digest(slot.vault) != published_content:
            raise StateError(f"tree {tree}: surface capture changed the published vault; preserved {slot.vault}")
        if seed is not None and (
            tree_digest(slot.vault) != seed[0]["vault_digest"]
            or signature_digest(slot.vault) != seed[0]["vault_signature_digest"]
        ):
            raise StateError(f"tree {tree}: seeded vault changed during preparation; preserved {slot.vault}")
        record = {
            "tree": tree,
            "profile": profile,
            "source_vault": str(source_vault),
            "snapshot_digest": snapshot_digest,
            "vault_digest": published_content,
            "vault_signatures": signature_manifest(slot.vault),
            "vault_signature_digest": signature_digest(slot.vault),
            "build": build,
        }
        if isinstance(observed_source, dict):
            record["product_source"] = observed_source
        # The build-record schema fixes these fields; absent donor fields stay unknown.
        build_settings = {"cpu_threads": cpu_threads, "resumed": resumed}
        if seed is not None:
            donor, provenance, _state = seed
            build_settings = {key: donor[key] for key in build_settings if key in donor}
            record.update(
                seed_build=provenance,
                vector_build=donor.get("vector_build", {
                    "run": provenance["donor_run"],
                    "metadata_digest": provenance["donor_metadata_digest"],
                    **build_settings,
                }),
                preparation={"cpu_threads": cpu_threads, "resumed": resumed},
            )
        if checkpoint is not None:
            # An unproven checkpoint has no certified build settings to inherit.
            build_settings = {}
            record.update(
                checkpoint={
                    "archive_run": checkpoint["archive_run"], **checkpoint["trees"][tree],
                    **({"product_source": checkpoint["predecessor_product_source"]}
                       if "predecessor_product_source" in checkpoint else {}),
                },
                preparation={"cpu_threads": cpu_threads, "resumed": resumed},
            )
        record.update(build_settings)
        # Freeze a baseline before the proof server can add process-local activity.
        shutil.move(str(slot.state), str(target / "state"))
        shutil.copytree(target / "state", slot.state, symlinks=True)
        _write_json_atomically(
            layout.run / "slot-vault.json",
            {
                "arm": spec.arm_id,
                "tree": tree,
                "session": f"quiescence-{tree}",
                "digest": record["vault_digest"],
                "signatures": record["vault_signature_digest"],
                "shared": True,
                "receipt_instances": _receipt_instances(slot.vault),
            },
        )
        record["quiescence"] = prove_quiescence(slot, spec, env=env, out=target, before=record)
        recover_session_vault(layout)
        record.update(
            state_digest=tree_digest(target / "state"),
            build_seconds=round(time.monotonic() - started, 2),
        )
        slot.wipe()
        _write_json_atomically(target / "state.json", record)
        pending_seed.unlink(missing_ok=True)
        # Reuse this capture only for the arm that supplied the tree's first environment.
        record["first_surface"] = surface
        return record
    finally:
        if seed is not None:
            # A changed vault refuses recovery and keeps both its receipt and evidence.
            recover_session_vault(layout)
            slot.wipe()


def build_arm(
    layout: Layout,
    spec: ArmSpec,
    *,
    tree: str,
    source_vault: Path,
    parent_env: Mapping[str, str],
    cpu_threads: int | None = None,
    seed_build_from: Path | None = None,
    checkpoint_from: Path | None = None,
) -> dict:
    """Copy the tree baseline for this arm and capture its own product surface."""

    checkpoint = require_checkpoint_selection(layout, checkpoint_from)
    if checkpoint is None:
        recover_session_vault(layout)
    if (layout.shared_tree(tree) / "seed-build.json").exists() and seed_build_from is None:
        raise StateError(f"tree {tree}: pending seed requires --seed-build-from before retry")
    target = layout.arm_tree(spec.arm_id, tree)
    if (target / "state.json").exists():
        record = json.loads((target / "state.json").read_text(encoding="utf-8"))
        _require_build_proof(record)
        return record
    shared = build_tree(
        layout,
        spec,
        tree=tree,
        source_vault=source_vault,
        parent_env=parent_env,
        cpu_threads=cpu_threads,
        seed_build_from=seed_build_from,
        checkpoint_from=checkpoint_from,
    )
    target.mkdir(parents=True, exist_ok=True)
    if (target / "state").exists():
        shutil.rmtree(target / "state")
    shutil.copytree(layout.shared_tree(tree) / "state", target / "state", symlinks=True)
    surface = shared.get("first_surface")
    surface_receipts = None
    if surface is None:
        slot = _prepare_session_slot(layout, spec.arm_id, tree, f"surface-{spec.arm_id}", shared)
        try:
            surface = capture_surface(
                slot, spec, env=_child_env(parent_env, slot, spec), out=target
            )
        finally:
            surface_receipts = recover_session_custody(layout).receipts_filed
            slot.wipe()
    record = {**shared, "arm": spec.arm_id, "surface": surface}
    record.pop("first_surface", None)
    if surface_receipts is not None:
        record["surface_receipts_filed"] = surface_receipts
    (target / "state.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record


def load_arm_state(layout: Layout, arm: str, tree: str) -> dict | None:
    path = layout.arm_tree(arm, tree) / "state.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _require_build_proof(record: Mapping) -> None:
    # Old snapshots lack the startup proofs required before any arm can use them.
    if record.get("build", {}).get("vectors", {}).get("state") != "current":
        raise StateError("arm state lacks completed vector publication; build a fresh run")
    if (
        not record.get("quiescence", {}).get("content_unchanged")
        or not record.get("quiescence", {}).get("signatures_unchanged")
        or not record.get("vault_signature_digest")
    ):
        raise StateError("state lacks startup quiescence and signature proof; build a fresh run")


def _rename_vault(source: Path, destination: Path) -> None:
    """Never fall back to copying: it invalidates the inherited catalog proof."""

    if source.stat().st_dev != destination.parent.stat().st_dev:
        raise StateError(f"vault move crosses devices: {source} -> {destination}")
    try:
        source.rename(destination)
    except OSError as error:
        raise StateError(f"vault rename failed: {source} -> {destination}: {error}") from error


@dataclass(frozen=True)
class Recovery:
    """A recovered checkout: the vault digest and what was filed out of the vault.

    ``digest`` is None when no vault was checked out. ``receipts_filed`` is None
    when the custody predates receipt filing.
    """

    digest: str | None
    receipts_filed: dict | None


def recover_session_vault(layout: Layout) -> str | None:
    """Return a checked-out vault, including one left by a killed runner.

    A changed vault stays in the slot with its custody receipt. Refusing later
    preparation prevents a new session or build from deleting that evidence.
    """

    return recover_session_custody(layout).digest


def recover_session_custody(layout: Layout) -> Recovery:
    """:func:`recover_session_vault`, with the receipts that it filed.

    Custody that lists the receipt instances present at checkout has each newer
    instance filed into run evidence before the vault is compared. Custody
    without the list is compared as it stands.
    """

    receipt = layout.run / "slot-vault.json"
    if not receipt.exists():
        return Recovery(None, None)
    custody = json.loads(receipt.read_text(encoding="utf-8"))
    # Old custody predates shared trees; return it before refusing the old build format.
    source = (
        layout.shared_tree(custody["tree"])
        if custody.get("shared")
        else layout.arm_tree(custody["arm"], custody["tree"])
    ) / "vault"
    held = Slot(layout.slot).vault
    location = held if held.exists() else source
    advice = (
        "Inspect the vault and any filed receipts, then remove the failed run at "
        f"{layout.run} and rebuild state from the frozen snapshot in a new run."
    )
    evidence = filed = None
    if "receipt_instances" in custody and location.is_dir():
        evidence = layout.receipts_dir(custody["session"])
        try:
            filed = _file_new_receipts(location, custody["receipt_instances"], evidence)
        except StateError as error:
            raise StateError(f"session {custody['session']}: {error}. {advice}") from error
    digest = tree_digest(location) if location.is_dir() else None
    signatures = signature_digest(location) if location.is_dir() else None
    if digest != custody["digest"] or (
        "signatures" in custody and signatures != custody["signatures"]
    ):
        # The evidence directory exists only once an instance has been filed into it.
        filing = f" Filed receipts are at {evidence}." if evidence is not None and evidence.exists() else ""
        raise StateError(
            f"session {custody['session']}: vault digest changed (content or signatures); "
            f"preserved at {location}.{filing} {advice}"
        )
    if held.exists():
        if source.exists():
            raise StateError(
                f"session {custody['session']}: both vault locations exist; preserved {held}"
            )
        _rename_vault(held, source)
    receipt.unlink()
    return Recovery(digest, filed)


def prepare_session_slot(layout: Layout, arm: str, tree: str, session: str = "unstarted") -> Slot:
    """Move the vault without changing signatures; copy only writable machine state."""

    recover_session_vault(layout)
    record = load_arm_state(layout, arm, tree)
    if record is None:
        raise StateError(f"arm {arm} tree {tree} has no built state; run the state stage first")
    _require_build_proof(record)
    return _prepare_session_slot(layout, arm, tree, session, record)


def _prepare_session_slot(
    layout: Layout, arm: str, tree: str, session: str, record: Mapping
) -> Slot:
    slot = Slot(layout.slot)
    slot.wipe()
    for path in (slot.runtime, slot.cwd, slot.hook_home):
        path.mkdir(parents=True)
    source = layout.shared_tree(tree) / "vault"
    machine_state = layout.arm_tree(arm, tree) / "state"
    for name, path, key in (
        ("vault", source, "vault_digest"),
        ("state", machine_state, "state_digest"),
    ):
        if tree_digest(path) != record[key]:
            raise StateError(f"arm {arm} tree {tree}: {name} does not match its build digest")
    signatures = signature_digest(source)
    if signatures != record["vault_signature_digest"]:
        raise StateError(
            f"arm {arm} tree {tree}: vault signatures differ from the build; rebuild state"
        )
    # Publish custody atomically before rename so either side of an interrupt can recover it.
    _write_json_atomically(
        layout.run / "slot-vault.json",
        {
            "arm": arm,
            "tree": tree,
            "session": session,
            "digest": record["vault_digest"],
            "signatures": signatures,
            "shared": True,
            "receipt_instances": _receipt_instances(source),
        },
    )
    try:
        _rename_vault(source, slot.vault)
        shutil.copytree(machine_state, slot.state, symlinks=True)
        if tree_digest(slot.state) != record["state_digest"]:
            raise StateError(
                f"arm {arm} tree {tree}: the session state copy differs from its build"
            )
        slot.turn_file.write_text("-1", encoding="utf-8")
    except BaseException:
        recover_session_vault(layout)
        slot.wipe()
        raise
    return slot
