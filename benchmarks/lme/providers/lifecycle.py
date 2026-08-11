"""Runner-owned direct-provider lifecycle and independently checked cleanup."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Callable, Iterable
from pathlib import Path

from protocol.models import ProviderCleanupObservation

from .base import ProviderRuntimeBinding, ProviderSessionContext


class CleanupUnproved(RuntimeError):
    pass


class LifecycleCompletenessError(RuntimeError):
    pass


class VariantDriftError(RuntimeError):
    pass


class ProviderConstructionFailure(RuntimeError):
    pass


class LifecycleRunError(RuntimeError):
    def __init__(self, primary: BaseException, secondary_failures: list[str]) -> None:
        super().__init__(str(primary))
        self.primary = primary
        self.secondary_failures = secondary_failures
        self.terminal_status = "INVALID"


_VARIANTS: dict[tuple[str, str], str] = {}


def bind_observed_variant(context: ProviderSessionContext, provider: object) -> str:
    value = getattr(provider, "variant_id")()
    if not isinstance(value, str) or not value:
        raise VariantDriftError("provider returned an empty observed variant")
    key = (context.run_id, context.session_id)
    previous = _VARIANTS.setdefault(key, value)
    if previous != value:
        raise VariantDriftError(f"observed provider variant drift: {previous!r} -> {value!r}")
    return value


def _normalize(binding: ProviderRuntimeBinding, context: ProviderSessionContext, provider: object) -> tuple[dict[str, object], ...]:
    raw = binding.observe(context, provider)
    if not isinstance(raw, tuple):
        raw = tuple(raw)
    try:
        typed = [ProviderCleanupObservation.model_validate({
            "run_id": context.run_id, "session_id": context.session_id,
            "requested_provider": "observer", "provider_variant": None,
            "namespace": context.namespace, "cleanup_called": True,
            "required_surface_ids": [], "observations": [item],
        }).observations[0].model_dump(mode="json") for item in raw]
    except Exception as exc:
        raise CleanupUnproved(f"unobservable cleanup surface: {exc}") from exc
    keys = [(item["kind"], item.get("path", item.get("expected_namespace", ""))) for item in typed]
    if len(keys) != len(set(keys)):
        raise CleanupUnproved("duplicate cleanup surface")
    kinds = {item["kind"] for item in typed}
    required_kinds = {"provider-state" if item == "provider-state" else "path-lstat" if item in {"session-root", "work-root"} else item for item in binding.required_surface_ids}
    if not required_kinds <= kinds:
        raise CleanupUnproved("cleanup absence is unproved: missing required surface")
    return tuple(sorted(typed, key=lambda item: (str(item["kind"]), str(item.get("path", item.get("expected_namespace", ""))))))


def _absence(surfaces: Iterable[dict[str, object]]) -> bool:
    for item in surfaces:
        if item["kind"] == "namespace-membership" and item["expected_namespace"] in item["live_namespaces"]:
            return False
        if item["kind"] == "provider-state" and (item["remaining_record_ids"] or item["backend_active"]):
            return False
        if item["kind"] == "path-lstat" and not (
            item["raw_kind"] == "missing" or (item["raw_kind"] == "directory" and not item["entries"])
        ):
            return False
    return True


def observe_cleanup(
    *, context: ProviderSessionContext, requested_provider: str, observed_variant: str | None,
    binding: ProviderRuntimeBinding, provider: object, cleanup_called: bool,
) -> ProviderCleanupObservation:
    first = _normalize(binding, context, provider)
    if not _absence(first):
        raise CleanupUnproved("cleanup absence is unproved: state remains")
    second = _normalize(binding, context, provider)
    if first != second:
        raise CleanupUnproved("cleanup observation disagrees on re-observation")
    return ProviderCleanupObservation(
        run_id=context.run_id, session_id=context.session_id, requested_provider=requested_provider,
        provider_variant=observed_variant, namespace=context.namespace, cleanup_called=cleanup_called,
        required_surface_ids=list(binding.required_surface_ids), observations=list(first),
    )


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    try:
        root_fd = os.open(path.parent, flags)
    except OSError as exc:
        raise CleanupUnproved(f"cleanup evidence root is unavailable: {exc}") from exc
    temporary = f".{path.name}.tmp"
    try:
        with os.scandir(root_fd) as entries:
            if any(entry.is_symlink() for entry in entries):
                raise CleanupUnproved("cleanup evidence root contains a symlink")
        try:
            os.stat(path.name, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise CleanupUnproved("cleanup evidence target already exists")
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=root_fd,
            )
        except OSError as exc:
            raise CleanupUnproved(f"cleanup evidence temporary cannot be opened: {exc}") from exc
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.replace(temporary, path.name, src_dir_fd=root_fd, dst_dir_fd=root_fd)
            final = os.open(path.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=root_fd)
        except OSError as exc:
            raise CleanupUnproved(f"cleanup evidence final cannot be opened: {exc}") from exc
        with os.fdopen(final, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise CleanupUnproved("cleanup evidence is not a regular file")
            if handle.read() != content:
                raise CleanupUnproved("cleanup evidence changed during persistence")
        os.fsync(root_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=root_fd)
        except FileNotFoundError:
            pass
        os.close(root_fd)


def _safe_regular_path(path: Path, evidence_root: Path | None = None) -> None:
    if evidence_root is not None:
        try:
            relative = path.relative_to(evidence_root)
        except ValueError as exc:
            raise CleanupUnproved("cleanup evidence escapes evidence root") from exc
        cursor = evidence_root
        for part in relative.parts:
            cursor = cursor / part
            mode = os.lstat(cursor).st_mode
            if stat.S_ISLNK(mode):
                raise CleanupUnproved("cleanup evidence uses a symlink")
    try:
        mode = os.lstat(path).st_mode
    except OSError as exc:
        raise CleanupUnproved(f"cleanup evidence is missing: {exc}") from exc
    if not stat.S_ISREG(mode):
        raise CleanupUnproved("cleanup evidence is not a regular file")


def verify_cleanup_observation(path: Path, digest: str, *, evidence_root: Path | None = None) -> bytes:
    _safe_regular_path(path, evidence_root)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CleanupUnproved(f"cleanup evidence cannot be opened no-follow: {exc}") from exc
    with os.fdopen(descriptor, "rb") as handle:
        payload = handle.read()
    actual = hashlib.sha256(payload).hexdigest()
    if actual != digest:
        raise ValueError("cleanup observation digest differs")
    try:
        ProviderCleanupObservation.model_validate_json(payload)
    except Exception as exc:
        raise CleanupUnproved(f"cleanup observation schema is invalid: {exc}") from exc
    return payload


def _persist_observation(context: ProviderSessionContext, observation: ProviderCleanupObservation) -> tuple[Path, str]:
    payload = observation.model_dump_json(indent=2).encode() + b"\n"
    path = context.evidence_root / "provider-cleanup-observation.json"
    _atomic_write(path, payload)
    digest = hashlib.sha256(payload).hexdigest()
    verify_cleanup_observation(path, digest, evidence_root=context.evidence_root)
    return path, digest


def run_provider_lifecycle(
    *, provider: object, profile: object, context: ProviderSessionContext,
    binding: ProviderRuntimeBinding, requested_provider: str, operation: Callable[[object], object],
) -> tuple[object, Path, str, str]:
    """Setup through persistence in one cleanup-owning outer ``finally``."""
    primary: BaseException | None = None
    secondary: list[str] = []
    result: object | None = None
    observed_variant: str | None = None
    observation_path: Path | None = None
    observation_digest: str | None = None
    try:
        getattr(provider, "setup")(profile, context)
        observed_variant = bind_observed_variant(context, provider)
        result = operation(provider)
        bind_observed_variant(context, provider)
    except BaseException as exc:
        primary = exc
    finally:
        try:
            getattr(provider, "cleanup")()
            if observed_variant is not None:
                bind_observed_variant(context, provider)
        except BaseException as exc:
            if primary is None and not isinstance(exc, Exception):
                primary = exc
            else:
                secondary.append(str(exc))
        try:
            observation = observe_cleanup(
                context=context, requested_provider=requested_provider, observed_variant=observed_variant,
                binding=binding, provider=provider, cleanup_called=True,
            )
            observation_path, observation_digest = _persist_observation(context, observation)
        except BaseException as exc:
            if primary is None and not isinstance(exc, Exception):
                primary = exc
            else:
                secondary.append(str(exc))
    if primary is not None:
        if not isinstance(primary, Exception):
            for note in secondary:
                try:
                    primary.add_note(note)
                except AttributeError:
                    pass
            raise primary
        raise LifecycleRunError(primary, secondary) from primary
    if secondary:
        raise CleanupUnproved("; ".join(secondary))
    assert observation_path is not None and observation_digest is not None and observed_variant is not None
    return result, observation_path, observation_digest, observed_variant


def terminalize_constructor_failure(
    context: ProviderSessionContext, *, requested_provider: str, error: BaseException,
) -> None:
    for root in (context.work_root, context.evidence_root):
        if root.exists():
            if root.is_dir():
                import shutil
                shutil.rmtree(root)
            else:
                root.unlink()
    raise ProviderConstructionFailure(f"constructor failure: {error}") from error


def _records_from_run(run_dir: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for trace in sorted((run_dir / "traces").glob("*.jsonl")):
        for line in trace.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row.get("schema_version") != 2 or row.get("protocol_version") != "1.0.0":
                raise LifecycleCompletenessError("lifecycle trace downgraded or mixed")
            if row.get("record") == "cleanup":
                records.append(row)
    return records


def validate_lifecycle_completeness(
    *, expected_instances: tuple[tuple[str, str, str], ...], cleanup_records: list[dict[str, object]] | None,
    evidence_root: Path, run_dir: Path | None = None,
) -> None:
    records = _records_from_run(run_dir) if cleanup_records is None and run_dir is not None else cleanup_records
    if records is None:
        raise LifecycleCompletenessError("cleanup records are unavailable")
    seen: set[tuple[str, str, str]] = set()
    expected = set(expected_instances)
    referenced: set[Path] = set()
    for record in records:
        path_value = record.get("observation_path")
        digest = record.get("observation_sha256")
        trace_run_id = record.get("run_id")
        trace_requested_provider = record.get("requested_provider")
        if not all(isinstance(value, str) and value for value in (path_value, digest)):
            raise LifecycleCompletenessError("cleanup record lacks bound observation reference")
        if path_value.startswith("/") or "\\" in path_value or any(part in {"", ".", ".."} for part in path_value.split("/")):
            raise LifecycleCompletenessError("cleanup observation path is unsafe")
        path = (run_dir / path_value) if run_dir is not None else evidence_root / path_value
        try:
            payload = verify_cleanup_observation(path, digest, evidence_root=evidence_root)
            observation = ProviderCleanupObservation.model_validate_json(payload)
        except Exception as exc:
            raise LifecycleCompletenessError(str(exc)) from exc
        if not observation.cleanup_called or not _absence([item.model_dump(mode="json") for item in observation.observations]):
            raise LifecycleCompletenessError("cleanup observation does not prove absence")
        if not all(isinstance(value, str) and value for value in (trace_run_id, trace_requested_provider)):
            raise LifecycleCompletenessError("cleanup record lacks bound observation reference")
        if observation.required_surface_ids != sorted(set(observation.required_surface_ids)) or not observation.required_surface_ids:
            raise LifecycleCompletenessError("cleanup observation required surfaces are invalid")
        if trace_run_id != observation.run_id:
            raise LifecycleCompletenessError("cleanup observation run binding disagrees with trace")
        if trace_requested_provider != observation.requested_provider:
            raise LifecycleCompletenessError("cleanup observation requested-provider binding disagrees with trace")
        trace_variant = record.get("provider_variant")
        if trace_variant is not None and trace_variant != observation.provider_variant:
            raise LifecycleCompletenessError("cleanup observation binding disagrees with trace")
        key = (str(record.get("session_id")), str(record.get("namespace")), str(observation.provider_variant))
        if key not in expected:
            raise LifecycleCompletenessError("orphan lifecycle cleanup record")
        if key in seen:
            raise LifecycleCompletenessError("duplicate lifecycle cleanup record")
        seen.add(key)
        referenced.add(path.resolve())
        if (observation.session_id, observation.namespace, observation.provider_variant) != key:
            raise LifecycleCompletenessError("cleanup observation binding disagrees with trace")
    if seen != expected:
        raise LifecycleCompletenessError("missing lifecycle cleanup record")
    for candidate in evidence_root.rglob("*.json"):
        try:
            candidate_observation = ProviderCleanupObservation.model_validate_json(candidate.read_bytes())
        except Exception:
            continue
        if candidate_observation.artifact_type == "provider-cleanup-observation.v1" and candidate.resolve() not in referenced:
            raise LifecycleCompletenessError("orphan lifecycle cleanup observation")
