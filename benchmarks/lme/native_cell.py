"""Per-question isolated Exomem public-MCP cell for the native LME diagnostic."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import json
import os
import shutil
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

_CALLABLE_TOOLS = frozenset(
    {
        "bootstrap",
        "ask_memory",
        "browse_memory",
        "read_memory",
        "capture_source",
        "compile_source",
        "remember",
        "observe_memory",
        "edit_memory",
        "replace_memory",
        "connect_memory",
        "review_memory",
        "review_item_context",
        "triage_memory",
        "schema_memory",
        "plan_memory",
        "record_memory",
    }
)
_REMOTE_INGEST_ARGUMENTS = frozenset(
    {"url", "files", "adoption", "delivery", "download_url", "authorization_session_credential"}
)
_PROFILES = frozenset({"fixture", "semantic"})


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _json_value(dataclasses.asdict(value))
    if hasattr(value, "model_dump"):
        return _json_value(value.model_dump(mode="json", by_alias=True))
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_value(item) for item in value]
    raise TypeError(f"MCP value is not JSON serializable: {type(value).__name__}")


def _product_payload(envelope: Mapping[str, Any]) -> Any:
    structured = envelope.get("structuredContent")
    if isinstance(structured, Mapping) and "result" in structured:
        return structured["result"]
    return structured


def _profile_settings(profile: str) -> dict[str, str]:
    if profile not in _PROFILES:
        raise ValueError(f"unsupported native cell profile: {profile!r}")
    if profile == "semantic":
        from .adapter import lme_profile

        return dict(lme_profile().settings)
    return {
        "EXOMEM_DISABLE_EMBEDDINGS": "1",
        "EXOMEM_DISABLE_RANKING": "1",
        "EXOMEM_DISABLE_CLIP": "1",
        "EXOMEM_DISABLE_MEDIA_EXTRACTION": "1",
        "EXOMEM_DISABLE_WARMUP": "1",
        "EXOMEM_DISABLE_FILE_WATCHER": "1",
        "EXOMEM_DISABLE_MODE_WATCH": "1",
        "EXOMEM_DISABLE_CORPUS_CACHE": "1",
        "EXOMEM_VEC_BACKEND": "numpy",
        "EXOMEM_LEXICAL_BACKEND": "python",
        "EXOMEM_MODE": "normal",
        "EXOMEM_DEVICE": "cpu",
        "EXOMEM_EMBED_DEVICE": "cpu",
        "EXOMEM_CLIP_DEVICE": "cpu",
        "CUDA_VISIBLE_DEVICES": "",
    }


class NativeCell:
    """Own one isolated stdio server and its persistent per-question vault."""

    def __init__(
        self,
        root: Path,
        *,
        python: Path,
        product_root: Path,
        profile: str = "fixture",
        timeout: float = 60.0,
        model_cache: Path | None = None,
        clip_model_cache: Path | None = None,
    ) -> None:
        if profile not in _PROFILES:
            raise ValueError(f"unsupported native cell profile: {profile!r}")
        if profile == "semantic" and model_cache is None:
            raise ValueError("semantic native cell requires a frozen model_cache")
        if profile == "semantic" and clip_model_cache is None:
            raise ValueError("semantic native cell requires a frozen clip_model_cache")
        self.root = Path(root).absolute()
        self.python = Path(python).absolute()
        self.product_root = Path(product_root).absolute()
        self.profile = profile
        self.timeout = timeout
        self.model_cache = Path(model_cache).absolute() if model_cache is not None else None
        self.clip_model_cache = (
            Path(clip_model_cache).absolute() if clip_model_cache is not None else None
        )
        self._vault = self.root / "vault"
        self._schemas: dict[str, dict[str, Any]] = {}
        self._runtime_receipt: dict[str, Any] = {}
        self._bootstrap: dict[str, Any] = {}
        self._client: Any | None = None
        self._transport: Any | None = None
        self._scaffold_paths: frozenset[str] = frozenset()
        self._fallback_detected: bool | None = True if profile == "fixture" else None
        self._semantic_retrieval_verified: bool | None = None
        self._readiness_path = self.root / "audit" / "readiness.json"

    @property
    def vault(self) -> Path:
        return self._vault

    @property
    def schemas(self) -> dict[str, dict[str, Any]]:
        return self._schemas

    @property
    def runtime_receipt(self) -> dict[str, Any]:
        return self._runtime_receipt

    @property
    def bootstrap(self) -> dict[str, Any]:
        return self._bootstrap

    async def __aenter__(self) -> NativeCell:
        self._prepare()
        from fastmcp import Client
        from fastmcp.client.transports import StdioTransport

        receipt_path = self.root / "audit" / "runtime.json"
        environment = self._environment()
        env_program = next(
            (
                candidate
                for candidate in (Path("/usr/bin/env"), Path("/bin/env"))
                if candidate.is_file()
            ),
            None,
        )
        if env_program is None:
            raise RuntimeError("native cell requires the POSIX env executable")
        self._transport = StdioTransport(
            command=str(env_program),
            args=[
                "-i",
                *(f"{key}={value}" for key, value in sorted(environment.items())),
                str(self.python),
                str(Path(__file__).resolve()),
                "--serve",
                str(receipt_path),
                self.profile,
            ],
            env={},
            cwd=str(self.root / "runtime"),
            keep_alive=False,
            log_file=self.root / "logs" / "stdio.log",
        )
        self._client = Client(self._transport, timeout=self.timeout, init_timeout=self.timeout)
        try:
            await self._client.__aenter__()
            tools = await self._client.list_tools()
            self._schemas = {
                tool.name: {
                    "description": tool.description,
                    "inputSchema": _json_value(tool.inputSchema),
                }
                for tool in tools
            }
            self._runtime_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            self._verify_runtime_receipt()
            bootstrap = await self.call("bootstrap", {"profile": "compact"})
            if bootstrap["isError"]:
                raise RuntimeError(f"native cell bootstrap failed: {bootstrap['content']!r}")
            payload = _product_payload(bootstrap)
            if not isinstance(payload, dict):
                raise RuntimeError("native cell bootstrap returned no structured product result")
            self._bootstrap = payload
            return self
        except BaseException:
            await self._close()
            raise

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        await self._close(exc_type, exc, traceback)

    async def _close(self, exc_type: Any = None, exc: Any = None, traceback: Any = None) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        close = asyncio.create_task(client.__aexit__(exc_type, exc, traceback))
        try:
            await asyncio.shield(close)
        except asyncio.CancelledError:
            await asyncio.shield(close)
            raise

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if self._client is None:
            raise RuntimeError("native cell is not running")
        if name not in _CALLABLE_TOOLS:
            raise PermissionError(f"native cell tool is not allowed: {name}")
        if not isinstance(arguments, dict):
            raise TypeError("native cell tool arguments must be an object")
        blocked = sorted(_blocked_arguments(arguments))
        if blocked:
            raise PermissionError(
                f"native cell remote or file ingestion arguments are not allowed: {blocked}"
            )
        result = await self._client.call_tool_mcp(name, arguments)
        envelope = {
            "content": _json_value(result.content),
            "structuredContent": _json_value(result.structuredContent),
            "isError": bool(result.isError),
        }
        if name == "ask_memory" and not envelope["isError"]:
            observed = _fallback_observation(envelope)
            if observed is True or (observed is False and self._fallback_detected is not True):
                self._fallback_detected = observed
            semantic_observed = _semantic_retrieval_observation(envelope)
            if semantic_observed is True or (
                semantic_observed is False and self._semantic_retrieval_verified is not True
            ):
                self._semantic_retrieval_verified = semantic_observed
        return envelope

    async def readiness(self) -> dict[str, Any]:
        if self._client is None:
            raise RuntimeError("native cell is not running")
        actual = await self._client.call_tool_mcp("coordination_status", {})
        actual_envelope = {
            "content": _json_value(actual.content),
            "structuredContent": _json_value(actual.structuredContent),
            "isError": bool(actual.isError),
        }
        service_readiness = json.loads(self._readiness_path.read_text(encoding="utf-8"))
        semantic_requested = self.profile == "semantic"
        semantic_verified = bool(service_readiness.get("semantic_verified"))
        ready = not actual_envelope["isError"] and (not semantic_requested or semantic_verified)
        return {
            "semantic_requested": semantic_requested,
            "semantic_verified": semantic_verified,
            "semantic_retrieval_verified": self._semantic_retrieval_verified,
            "fallback_detected": self._fallback_detected,
            "status": (
                "ready"
                if ready and semantic_requested
                else "ready_lexical_only"
                if ready
                else "not_ready"
            ),
            "evidence": {
                "semantic_probe": self._runtime_receipt.get("semantic_probe"),
                "serving_corpus": service_readiness.get("serving_corpus"),
                "retrieval_fallback": self._fallback_detected,
                "coordination_status": actual_envelope,
            },
            "actualserverreceipt": self._runtime_receipt,
        }

    def snapshot(self) -> dict[str, Any]:
        files: dict[str, str] = {}
        stored_bytes = 0
        raw_count = 0
        compiled_count = 0
        for path in _regular_files_no_follow(self.vault):
            relative = path.relative_to(self.vault).as_posix()
            data = _read_no_follow(path)
            files[relative] = hashlib.sha256(data).hexdigest()
            stored_bytes += len(data)
            parts = path.relative_to(self.vault).parts
            if (
                path.suffix.lower() != ".md"
                or relative in self._scaffold_paths
                or "_Schema" in parts
                or path.name.lower() in {"index.md", "log.md"}
            ):
                continue
            if "Sources" in parts or "Evidence" in parts:
                raw_count += 1
            else:
                compiled_count += 1
        return {
            "vault_root": str(self.vault),
            "stored_bytes": stored_bytes,
            "file_count": len(files),
            "raw_count": raw_count,
            "compiled_count": compiled_count,
            "files": dict(sorted(files.items())),
        }

    def _prepare(self) -> None:
        if not self.python.is_file():
            raise FileNotFoundError(f"native cell Python does not exist: {self.python}")
        scaffold = self.product_root / "src" / "exomem" / "_scaffold"
        if not scaffold.is_dir():
            raise FileNotFoundError(f"Exomem scaffold does not exist: {scaffold}")
        try:
            self.root.mkdir(mode=0o700, parents=False, exist_ok=False)
        except FileExistsError as error:
            raise ValueError("native cell root must be a new path") from error
        for name in ("runtime", "config", "state", "logs", "leases", "cache", "audit"):
            (self.root / name).mkdir(mode=0o700)
        (self.root / "cache" / "tmp").mkdir(mode=0o700)
        (self.root / "cache" / "xdg-state").mkdir(mode=0o700)
        (self.root / "cache" / "xdg-config").mkdir(mode=0o700)
        (self.root / "cache" / "xdg-cache").mkdir(mode=0o700)
        (self.root / "cache" / "torch").mkdir(mode=0o700)
        (self.root / "cache" / "torchinductor").mkdir(mode=0o700)
        (self.root / "cache" / "joblib").mkdir(mode=0o700)
        if self.model_cache is not None:
            copy_model_cache(
                self.model_cache,
                self.root / "cache" / "huggingface" / "hub" / "models--BAAI--bge-base-en-v1.5",
            )
        if self.clip_model_cache is not None:
            copy_model_cache(
                self.clip_model_cache,
                self.root
                / "cache"
                / "huggingface"
                / "hub"
                / "models--sentence-transformers--clip-ViT-B-32",
            )
        self.vault.mkdir()
        shutil.copytree(scaffold, self.vault / "Knowledge Base")
        self._scaffold_paths = frozenset(
            path.relative_to(self.vault).as_posix() for path in _regular_files_no_follow(self.vault)
        )

    def _environment(self) -> dict[str, str]:
        cache = self.root / "cache"
        environment = {
            "HOME": str(self.root),
            "USERPROFILE": str(self.root),
            "PATH": os.defpath,
            "LC_CTYPE": "C.UTF-8",
            "PYTHONPATH": str(self.product_root / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUTF8": "1",
            "FASTMCP_CHECK_FOR_UPDATES": "off",
            "FASTMCP_SHOW_SERVER_BANNER": "false",
            "TMPDIR": str(cache / "tmp"),
            "XDG_STATE_HOME": str(cache / "xdg-state"),
            "XDG_CONFIG_HOME": str(cache / "xdg-config"),
            "XDG_CACHE_HOME": str(cache / "xdg-cache"),
            "HF_HOME": str(cache / "huggingface"),
            "TORCH_HOME": str(cache / "torch"),
            "TORCHINDUCTOR_CACHE_DIR": str(cache / "torchinductor"),
            "JOBLIB_TEMP_FOLDER": str(cache / "joblib"),
            "KMP_DUPLICATE_LIB_OK": "True",
            "KMP_INIT_AT_FORK": "FALSE",
            "EXOMEM_VAULT_PATH": str(self.vault),
            "EXOMEM_STATE_ROOT": str(self.root / "state"),
            "EXOMEM_CONFIG_PATH": str(self.root / "config" / "config.json"),
            "EXOMEM_WRITER_LEASE_STATE_DIR": str(self.root / "leases"),
            "EXOMEM_LOG_DIR": str(self.root / "logs"),
            "EXOMEM_CALL_LEDGER_DIR": str(self.root / "logs" / "call-ledger"),
            "EXOMEM_SURFACE": "openai",
        }
        environment.update(_profile_settings(self.profile))
        return environment

    def _verify_runtime_receipt(self) -> None:
        expected = {
            "vault_root": self.vault,
            "state_root": self.root / "state",
            "config_path": self.root / "config" / "config.json",
            "log_root": self.root / "logs",
            "lease_root": self.root / "leases",
            "call_ledger_root": self.root / "logs" / "call-ledger",
            "working_directory": self.root / "runtime",
            "hf_home": self.root / "cache" / "huggingface",
            "torch_home": self.root / "cache" / "torch",
            "torchinductor_cache_root": self.root / "cache" / "torchinductor",
            "joblib_temp_root": self.root / "cache" / "joblib",
        }
        mismatches = [
            key for key, path in expected.items() if self._runtime_receipt.get(key) != str(path)
        ]
        if mismatches:
            raise RuntimeError(f"native cell runtime root attestation failed: {mismatches}")
        index_root = Path(str(self._runtime_receipt.get("index_root", "")))
        try:
            index_root.relative_to(self.root / "state")
        except ValueError as error:
            raise RuntimeError("native cell index root escaped its state root") from error
        if self._runtime_receipt.get("attestation_stage") != "after_build":
            raise RuntimeError("native cell runtime was not attested after server construction")
        if self._runtime_receipt.get("prebuild_bindings") != _runtime_binding_fields(
            self._runtime_receipt
        ):
            raise RuntimeError("native cell runtime bindings changed during server construction")
        if self._runtime_receipt.get("environment_keys") != sorted(self._environment()):
            raise RuntimeError("native cell child environment did not match the allowlist")
        if self.profile == "semantic" and not self._runtime_receipt.get("semantic_model_verified"):
            raise RuntimeError("native cell semantic profile is not ready")


def _fallback_observation(value: Any) -> bool | None:
    found_profile = False
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, Mapping):
            if current.get("degraded"):
                return True
            for key, item in current.items():
                if key == "effective_mode":
                    found_profile = True
                    mode = str(item).lower()
                    if "fallback" in mode or mode.endswith("_lexical"):
                        return True
                elif key == "fallback":
                    found_profile = True
                    if item is True or (isinstance(item, str) and "fallback" in item.lower()):
                        return True
                pending.append(item)
        elif isinstance(current, list):
            pending.extend(current)
    return False if found_profile else None


def _semantic_retrieval_observation(value: Any) -> bool | None:
    if _fallback_observation(value) is True:
        return False
    observed = False
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, Mapping):
            if current.get("warming"):
                return False
            mode = str(current.get("effective_mode", "")).lower()
            if mode in {"hybrid", "vector"}:
                observed = True
            lanes = current.get("lanes")
            if isinstance(lanes, Mapping) and isinstance(lanes.get("vector"), Mapping):
                vector = lanes["vector"]
                if "rank" in vector or "cosine" in vector:
                    observed = True
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
    return True if observed else None


def _blocked_arguments(value: Any) -> set[str]:
    blocked: set[str] = set()
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, Mapping):
            for key, item in current.items():
                if key in _REMOTE_INGEST_ARGUMENTS and item not in (None, "", []):
                    blocked.add(str(key))
                else:
                    pending.append(item)
        elif isinstance(current, list):
            pending.extend(current)
    return blocked


def _regular_files_no_follow(root: Path) -> list[Path]:
    root_mode = root.lstat().st_mode
    if not stat.S_ISDIR(root_mode):
        raise RuntimeError("native cell vault root is not a real directory")
    pending = [root]
    files: list[Path] = []
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.is_symlink():
                    raise RuntimeError(f"native cell vault contains a symlink: {entry.name}")
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    files.append(Path(entry.path))
                else:
                    raise RuntimeError(
                        f"native cell vault contains a non-regular entry: {entry.name}"
                    )
    return sorted(files, key=lambda path: path.relative_to(root).as_posix())


def copy_model_cache(source: Path, destination: Path) -> None:
    """Copy one selected model snapshot into a new, symlink-free HF cache subtree."""

    source = Path(source).absolute()
    destination = Path(destination).absolute()
    if source.is_symlink() or not source.is_dir():
        raise ValueError("model_cache must be a real directory")
    if destination.exists() or destination.is_symlink():
        raise ValueError("model cache destination must be a new path")
    source_root = source.resolve(strict=True)
    refs_root = source / "refs"
    snapshots_root = source / "snapshots"
    blob_root = source / "blobs"
    if refs_root.is_symlink() or snapshots_root.is_symlink() or blob_root.is_symlink():
        raise ValueError("model_cache metadata and blob roots must be real directories")
    if not refs_root.is_dir() or not snapshots_root.is_dir():
        raise ValueError("model_cache has no refs or snapshots directory")
    ref_path = refs_root / "main"
    try:
        revision = _read_no_follow(ref_path).decode("ascii").strip()
    except (FileNotFoundError, UnicodeDecodeError) as error:
        raise ValueError("model_cache has no valid refs/main") from error
    if len(revision) != 40 or any(character not in "0123456789abcdef" for character in revision):
        raise ValueError("model_cache refs/main is not a pinned revision")
    snapshot = snapshots_root / revision
    if snapshot.is_symlink() or not snapshot.is_dir():
        raise ValueError("model_cache selected snapshot is missing")

    (destination / "refs").mkdir(mode=0o700, parents=True)
    (destination / "blobs").mkdir(mode=0o700)
    copied_snapshot = destination / "snapshots" / revision
    copied_snapshot.mkdir(mode=0o700, parents=True)
    pending = [snapshot]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                source_path = Path(entry.path)
                relative = source_path.relative_to(snapshot)
                target = copied_snapshot / relative
                if entry.is_symlink():
                    try:
                        resolved = source_path.resolve(strict=True)
                        resolved.relative_to(source_root)
                        resolved.relative_to(blob_root.resolve(strict=True))
                    except (FileNotFoundError, RuntimeError, ValueError) as error:
                        raise ValueError(
                            f"model snapshot symlink does not resolve to a model blob: {relative}"
                        ) from error
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    _copy_file_no_follow(resolved, target)
                elif entry.is_dir(follow_symlinks=False):
                    target.mkdir(mode=0o700)
                    pending.append(source_path)
                elif entry.is_file(follow_symlinks=False):
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    _copy_file_no_follow(source_path, target)
                else:
                    raise ValueError(f"model snapshot contains a non-regular entry: {relative}")
    copied_files = list(copied_snapshot.rglob("*"))
    has_config = any(path.is_file() and path.name == "config.json" for path in copied_files)
    has_weights = any(
        path.is_file() and path.name in {"model.safetensors", "pytorch_model.bin"}
        for path in copied_files
    )
    if not has_config or not has_weights:
        raise ValueError("model_cache snapshot is missing encoder configuration or weights")
    (destination / "refs" / "main").write_text(revision, encoding="ascii")


def _copy_file_no_follow(source: Path, destination: Path) -> None:
    read_descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(read_descriptor).st_mode):
            raise ValueError(f"model cache entry is not a regular file: {source.name}")
        write_descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            with (
                os.fdopen(read_descriptor, "rb", closefd=False) as source_handle,
                os.fdopen(write_descriptor, "wb", closefd=False) as destination_handle,
            ):
                shutil.copyfileobj(source_handle, destination_handle, length=1024 * 1024)
        finally:
            os.close(write_descriptor)
    finally:
        os.close(read_descriptor)


def _read_no_follow(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise RuntimeError(f"native cell vault entry is not a regular file: {path.name}")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            return handle.read()
    finally:
        os.close(descriptor)


def _actual_runtime_receipt(profile: str) -> dict[str, Any]:
    from exomem import call_ledger, mode, state_paths
    from exomem.logging_config import resolve_log_dir
    from exomem.vault import resolve_vault
    from exomem.writer_lease import LeaseConfig

    vault = resolve_vault()
    semantic_probe: dict[str, Any]
    semantic_verified = False
    if profile == "semantic":
        try:
            from exomem import embeddings

            vector = embeddings.get_model().encode(["native cell semantic readiness"])
            semantic_probe = {
                "status": "verified",
                "model": embeddings.MODEL_NAME,
                "vector_shape": list(vector.shape),
            }
        except Exception as error:  # noqa: BLE001 - any model fault means not ready
            semantic_probe = {"status": "unavailable", "reason": type(error).__name__}
        else:
            try:
                clip_vector = embeddings.get_clip_model().encode(["native cell CLIP readiness"])
                semantic_probe["clip_probe"] = {
                    "status": "verified",
                    "model": embeddings.CLIP_MODEL_NAME,
                    "vector_shape": list(clip_vector.shape),
                }
                semantic_verified = True
            except Exception as error:  # noqa: BLE001 - any model fault means not ready
                semantic_probe["clip_probe"] = {
                    "status": "unavailable",
                    "reason": type(error).__name__,
                }
    else:
        semantic_probe = {"status": "lexical_only", "reason": "fixture_profile"}
    return {
        "child_pid": os.getpid(),
        "vault_root": str(vault),
        "state_root": str(state_paths.state_store_root()),
        "index_root": str(state_paths.vault_state_dir(vault)),
        "config_path": str(mode.config_path()),
        "log_root": str(resolve_log_dir()),
        "lease_root": str(LeaseConfig.from_env().state_dir),
        "call_ledger_root": str(call_ledger.ledger_dir()),
        "working_directory": str(Path.cwd()),
        "hf_home": os.environ["HF_HOME"],
        "torch_home": os.environ["TORCH_HOME"],
        "torchinductor_cache_root": os.environ["TORCHINDUCTOR_CACHE_DIR"],
        "joblib_temp_root": os.environ["JOBLIB_TEMP_FOLDER"],
        "kmp_duplicate_lib_ok": os.environ["KMP_DUPLICATE_LIB_OK"],
        "kmp_init_at_fork": os.environ["KMP_INIT_AT_FORK"],
        "semantic_requested": profile == "semantic",
        "semantic_model_verified": semantic_verified,
        "semantic_probe": semantic_probe,
        "environment_keys": sorted(os.environ),
    }


def _runtime_binding_fields(receipt: Mapping[str, Any]) -> dict[str, Any]:
    names = (
        "vault_root",
        "state_root",
        "index_root",
        "config_path",
        "log_root",
        "lease_root",
        "call_ledger_root",
        "working_directory",
        "hf_home",
        "torch_home",
        "torchinductor_cache_root",
        "joblib_temp_root",
        "kmp_duplicate_lib_ok",
        "kmp_init_at_fork",
    )
    return {name: receipt.get(name) for name in names}


def _bindings_match_environment(receipt: Mapping[str, Any]) -> bool:
    expected = {
        "vault_root": os.environ["EXOMEM_VAULT_PATH"],
        "state_root": os.environ["EXOMEM_STATE_ROOT"],
        "config_path": os.environ["EXOMEM_CONFIG_PATH"],
        "log_root": os.environ["EXOMEM_LOG_DIR"],
        "lease_root": os.environ["EXOMEM_WRITER_LEASE_STATE_DIR"],
        "call_ledger_root": os.environ["EXOMEM_CALL_LEDGER_DIR"],
        "working_directory": str(Path.cwd()),
        "hf_home": os.environ["HF_HOME"],
        "torch_home": os.environ["TORCH_HOME"],
        "torchinductor_cache_root": os.environ["TORCHINDUCTOR_CACHE_DIR"],
        "joblib_temp_root": os.environ["JOBLIB_TEMP_FOLDER"],
        "kmp_duplicate_lib_ok": "True",
        "kmp_init_at_fork": "FALSE",
    }
    return all(receipt.get(name) == value for name, value in expected.items())


def _committed_paths(result: Any, vault: Path) -> set[str]:
    is_error = getattr(result, "isError", None)
    if is_error is None and isinstance(result, Mapping):
        is_error = result.get("isError", result.get("is_error"))
    if is_error:
        return set()
    payload = getattr(result, "structured_content", None)
    if payload is None:
        payload = getattr(result, "structuredContent", None)
    if payload is None and isinstance(result, Mapping):
        payload = result.get("structured_content") or result.get("structuredContent")
    if not isinstance(payload, dict):
        return set()
    payload = payload.get("result", payload)
    if not isinstance(payload, dict) or payload.get("mutated") is not True:
        return set()
    paths: set[str] = set()
    pending = [payload]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            for key, value in current.items():
                if key in {"path", "new_path"} and isinstance(value, str):
                    candidate = vault / value
                    try:
                        candidate.relative_to(vault)
                    except ValueError:
                        continue
                    if candidate.is_file() and candidate.suffix.lower() == ".md":
                        paths.add(value.replace("\\", "/"))
                else:
                    pending.append(value)
        elif isinstance(current, list):
            pending.extend(current)
    return paths


def _serving_corpus_readiness(
    vault: Path, *, profile: str, paths: set[str], model_verified: bool
) -> dict[str, Any]:
    from exomem import deferred_index, lexstore

    semantic_requested = profile == "semantic"
    try:
        lexical_current: bool | None = lexstore.runtime_retrieval_catalog_current(
            vault, schedule_repair=False
        )
    except Exception:  # noqa: BLE001 - content-free readiness remains unavailable
        lexical_current = None
    if not semantic_requested:
        return {
            "semantic_requested": False,
            "semantic_verified": False,
            "status": "lexical_only",
            "tracked_path_count": len(paths),
            "lexical_current": lexical_current,
            "embedding_states": {},
            "pending_semantic_upserts": None,
        }
    states = {
        path: str(value)
        for path, value in deferred_index.inspect_embedding_freshness(vault, sorted(paths)).items()
    }
    pending = deferred_index.status(vault)
    corpus_current = all(value == "current" for value in states.values()) and pending["count"] == 0
    verified = model_verified and corpus_current
    return {
        "semantic_requested": True,
        "semantic_verified": verified,
        "status": "current" if verified else "not_ready",
        "tracked_path_count": len(paths),
        "lexical_current": lexical_current,
        "embedding_states": states,
        "pending_semantic_upserts": pending["count"],
    }


class _ReadinessRecorder:
    def __init__(self, *, vault: Path, profile: str, path: Path, model_verified: bool) -> None:
        self.vault = vault
        self.profile = profile
        self.path = path
        self.model_verified = model_verified
        self.paths: set[str] = set()

    def record(self) -> None:
        serving = _serving_corpus_readiness(
            self.vault,
            profile=self.profile,
            paths=self.paths,
            model_verified=self.model_verified,
        )
        _write_json(
            self.path,
            {
                "semantic_requested": serving["semantic_requested"],
                "semantic_verified": serving["semantic_verified"],
                "serving_corpus": serving,
            },
        )

    def mark_observation_pending(self) -> None:
        _write_json(
            self.path,
            {
                "semantic_requested": self.profile == "semantic",
                "semantic_verified": False,
                "serving_corpus": {
                    "status": "observation_pending",
                    "tracked_path_count": len(self.paths),
                },
            },
        )

    def middleware(self) -> Any:
        from fastmcp.server.middleware.middleware import Middleware

        recorder = self

        class ReadinessMiddleware(Middleware):
            async def on_call_tool(self, context: Any, call_next: Any) -> Any:
                recorder.mark_observation_pending()
                result = await call_next(context)
                try:
                    recorder.paths.update(_committed_paths(result, recorder.vault))
                    recorder.record()
                except Exception:  # noqa: BLE001 - pending marker already fails readiness closed
                    pass
                return result

        return ReadinessMiddleware()


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _serve(receipt_path: Path, profile: str) -> int:
    before = _actual_runtime_receipt(profile)
    before["attestation_stage"] = "before_build"
    _write_json(receipt_path, before)
    if not _bindings_match_environment(before):
        return 2
    if profile == "semantic" and not before["semantic_model_verified"]:
        return 2
    from exomem.logging_config import configure_logging
    from exomem.server import build_server

    configure_logging(Path(before["log_root"]), process="server")
    server = build_server(require_auth=False)
    after = _actual_runtime_receipt(profile)
    after["attestation_stage"] = "after_build"
    after["prebuild_bindings"] = _runtime_binding_fields(before)
    _write_json(receipt_path, after)
    if not _bindings_match_environment(after):
        return 2
    if _runtime_binding_fields(after) != _runtime_binding_fields(before):
        return 2
    recorder = _ReadinessRecorder(
        vault=Path(after["vault_root"]),
        profile=profile,
        path=receipt_path.parent / "readiness.json",
        model_verified=bool(after["semantic_model_verified"]),
    )
    recorder.record()
    server.add_middleware(recorder.middleware())
    server.run(transport="stdio")
    return 0


def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", type=Path, required=True)
    parser.add_argument("profile", choices=sorted(_PROFILES))
    arguments = parser.parse_args()
    return _serve(arguments.serve, arguments.profile)


if __name__ == "__main__":
    raise SystemExit(_main())
