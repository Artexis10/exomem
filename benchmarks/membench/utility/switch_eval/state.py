"""Run layout, per-arm state builds and the captured server surface.

Exomem keys a vault's machine-local state by the vault's resolved path
(``state_paths.vault_state_dir``), so state built for one path is not found
under another. Every build and every session therefore runs in one fixed slot
inside the run directory: ``slot/vault`` and ``slot/state``. A build leaves its
result in ``arms/<arm>/<tree>/`` with a digest. A session moves the vault
back into the slot and copies machine state. It returns the unchanged vault
before wiping the slot. Nothing
here reads or writes the user's own state root or a running service: every
product location is pinned under the slot (see :func:`product_env`).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from epistemic.journeys.f27_replay import REPO_ROOT, refuse_unsafe_out_dir

from . import protocol
from .arms import ArmSpec, canonical_json, profile_settings, sha256_text
from .tap import HANDSHAKE_METHODS
from .tap import __file__ as TAP_SCRIPT

SERVER_NAME = "exomem"


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


def _child_env(parent: Mapping[str, str], slot: Slot, spec: ArmSpec) -> dict[str, str]:
    env = {key: value for key, value in parent.items() if not key.startswith("EXOMEM_")}
    env.update(product_env(slot, spec))
    env["PYTHONPATH"] = str(REPO_ROOT / "benchmarks")
    return env


def prepare_build_slot(layout: Layout, source_vault: Path) -> Slot:
    recover_session_vault(layout)
    slot = Slot(layout.slot)
    slot.wipe()
    for path in (slot.runtime, slot.cwd, slot.hook_home):
        path.mkdir(parents=True)
    shutil.copytree(source_vault, slot.vault, symlinks=True)
    return slot


def build_state(vault: Path) -> dict:
    """Runs in the builder child: the product's own fresh bootstrap, then the
    derived indexes the existing harness publishes, then the context catalogue.

    The same build for every arm: the arms differ in packet mode at serve time,
    not in what the vault holds.
    """

    from exomem import index_paths, recall_migration, state_migration
    from exomem.context_catalogue import ContextCatalogue
    from membench.utility.context_activation_product import publish

    started = time.monotonic()
    state_migration.require_vault_state_ready(vault)
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
    vectors = {
        **recall_migration.status(vault),
        "seconds": round(time.monotonic() - vector_started, 3),
    }
    catalogue = ContextCatalogue(vault).rebuild()
    return {
        "vectors": vectors,
        "anchors": len(publication.anchors),
        "indexed_pages": publication.indexed_pages,
        "catalogue": {
            key: value for key, value in catalogue.items() if isinstance(value, (int, str))
        },
        "seconds": round(time.monotonic() - started, 2),
    }


def mcp_server_entry(
    slot: Slot, spec: ArmSpec, *, log: Path, server_stderr: Path, skip_ready_gate: bool = False
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
            "exomem",
            "--transport",
            "stdio",
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
    slot: Slot, spec: ArmSpec, *, env: Mapping[str, str], out: Path, method: str
) -> tuple[str, list[dict]]:
    """One connection: the handshake ``method``, then ``tools/list``.

    The server serves one protocol era per connection, so each handshake
    gets its own server.
    """

    handshake_params, list_params = _HANDSHAKES[method]
    name = method.replace("/", "-")
    stderr = out / f"surface-{name}-server.stderr"
    entry = mcp_server_entry(
        slot,
        spec,
        log=out / f"surface-{name}-tap.jsonl",
        server_stderr=stderr,
        skip_ready_gate=True,
    )
    proc = subprocess.Popen(  # noqa: S603 - the harness's own tap and server
        [entry["command"], *entry["args"]],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env={**env, **entry["env"]},
        cwd=slot.cwd,
    )

    def send(message: dict) -> None:
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
        proc.stdin.flush()

    def ask(request_id: int, request: str, params: dict) -> dict:
        send({"id": request_id, "method": request, "params": params})
        for line in proc.stdout:
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
    finally:
        proc.stdin.close()
        proc.wait(timeout=60)
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


def build_arm(
    layout: Layout, spec: ArmSpec, *, tree: str, source_vault: Path, parent_env: Mapping[str, str]
) -> dict:
    """Build one arm's state for one tree once, capture its surface, record digests."""

    target = layout.arm_tree(spec.arm_id, tree)
    if (target / "state.json").exists():
        record = json.loads((target / "state.json").read_text(encoding="utf-8"))
        _require_vector_build(record)
        return record
    snapshot_digest = tree_digest(source_vault)
    slot = prepare_build_slot(layout, source_vault)
    env = _child_env(parent_env, slot, spec)
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
        env=env,
        cwd=slot.cwd,
        capture_output=True,
        text=True,
        timeout=None,
    )
    (target / "build.stdout").write_text(completed.stdout, encoding="utf-8")
    (target / "build.stderr").write_text(completed.stderr, encoding="utf-8")
    if completed.returncode != 0:
        raise StateError(
            f"arm {spec.arm_id} tree {tree}: the state build exited {completed.returncode}; see {target}"
        )
    build = json.loads(completed.stdout.strip().splitlines()[-1])
    surface = capture_surface(slot, spec, env=env, out=target)
    for name in ("vault", "state"):
        if (target / name).exists():
            shutil.rmtree(target / name)
        shutil.move(str(slot.root / name), str(target / name))
    record = {
        "arm": spec.arm_id,
        "tree": tree,
        "source_vault": str(source_vault),
        "snapshot_digest": snapshot_digest,
        "vault_digest": tree_digest(target / "vault"),
        "state_digest": tree_digest(target / "state"),
        "build": build,
        "build_seconds": round(time.monotonic() - started, 2),
        "surface": surface,
    }
    slot.wipe()
    (target / "state.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record


def load_arm_state(layout: Layout, arm: str, tree: str) -> dict | None:
    path = layout.arm_tree(arm, tree) / "state.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _require_vector_build(record: Mapping) -> None:
    # Older snapshots lack vectors and would restart the same incomplete sessions.
    if record.get("build", {}).get("vectors", {}).get("state") != "current":
        raise StateError("arm state lacks completed vector publication; build a fresh run")


def _rename_vault(source: Path, destination: Path) -> None:
    """Never fall back to copying: it invalidates the inherited catalog proof."""

    if source.stat().st_dev != destination.parent.stat().st_dev:
        raise StateError(f"vault move crosses devices: {source} -> {destination}")
    try:
        source.rename(destination)
    except OSError as error:
        raise StateError(f"vault rename failed: {source} -> {destination}: {error}") from error


def recover_session_vault(layout: Layout) -> str | None:
    """Return a checked-out vault, including one left by a killed runner.

    A changed vault stays in the slot with its custody receipt. Refusing later
    preparation prevents a new session or build from deleting that evidence.
    """

    receipt = layout.run / "slot-vault.json"
    if not receipt.exists():
        return None
    custody = json.loads(receipt.read_text(encoding="utf-8"))
    source = layout.arm_tree(custody["arm"], custody["tree"]) / "vault"
    held = Slot(layout.slot).vault
    location = held if held.exists() else source
    digest = tree_digest(location) if location.is_dir() else None
    if digest != custody["digest"]:
        raise StateError(
            f"session {custody['session']}: vault digest changed; "
            f"preserved at {location}; expected {custody['digest']}, got {digest}"
        )
    if held.exists():
        if source.exists():
            raise StateError(
                f"session {custody['session']}: both vault locations exist; preserved {held}"
            )
        _rename_vault(held, source)
    receipt.unlink()
    return digest


def prepare_session_slot(layout: Layout, arm: str, tree: str, session: str = "unstarted") -> Slot:
    """Move the vault without changing signatures; copy only writable machine state."""

    recover_session_vault(layout)
    record = load_arm_state(layout, arm, tree)
    if record is None:
        raise StateError(f"arm {arm} tree {tree} has no built state; run the state stage first")
    _require_vector_build(record)
    slot = Slot(layout.slot)
    slot.wipe()
    for path in (slot.runtime, slot.cwd, slot.hook_home):
        path.mkdir(parents=True)
    source = layout.arm_tree(arm, tree)
    for name, key in (("vault", "vault_digest"), ("state", "state_digest")):
        if tree_digest(source / name) != record[key]:
            raise StateError(f"arm {arm} tree {tree}: {name} does not match its build digest")
    # Write custody before rename so an interrupt on either side can recover it.
    (layout.run / "slot-vault.json").write_text(
        json.dumps(
            {"arm": arm, "tree": tree, "session": session, "digest": record["vault_digest"]}
        ),
        encoding="utf-8",
    )
    try:
        _rename_vault(source / "vault", slot.vault)
        shutil.copytree(source / "state", slot.state, symlinks=True)
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
