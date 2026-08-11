"""Runner-owned direct-provider lifecycle and independently checked cleanup."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
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


_VARIANTS: dict[str, str] = {}


@dataclass(frozen=True)
class LifecycleEvidence:
    """Finalized cleanup evidence emitted before lifecycle control returns."""

    run_id: str
    requested_provider: str
    session_id: str
    namespace: str
    provider_variant: str | None
    required_surface_ids: tuple[str, ...]
    observation_path: Path
    observation_sha256: str

    def expected_instance(self, run_dir: Path) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "requested_provider": self.requested_provider,
            "session_id": self.session_id,
            "namespace": self.namespace,
            "provider_variant": self.provider_variant,
            "required_surface_ids": list(self.required_surface_ids),
            "observation_path": self.observation_path.relative_to(run_dir).as_posix(),
            "observation_sha256": self.observation_sha256,
        }

    def trace_record(self, run_dir: Path) -> dict[str, object]:
        return {
            "record": "cleanup",
            "run_id": self.run_id,
            "session_id": self.session_id,
            "namespace": self.namespace,
            "requested_provider": self.requested_provider,
            "observation_path": self.observation_path.relative_to(run_dir).as_posix(),
            "observation_sha256": self.observation_sha256,
        }


def reset_observed_variant(run_id: str) -> None:
    """Start one runner-owned run-global variant binding."""

    _VARIANTS.pop(run_id, None)


def bind_observed_variant(context: ProviderSessionContext, provider: object) -> str:
    value = getattr(provider, "variant_id")()
    if not isinstance(value, str) or not value:
        raise VariantDriftError("provider returned an empty observed variant")
    key = context.run_id
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


def _atomic_write(root_fd: int, name: str, content: bytes) -> None:
    temporary = f".{name}.tmp"
    try:
        with os.scandir(root_fd) as entries:
            if any(entry.is_symlink() for entry in entries):
                raise CleanupUnproved("cleanup evidence root contains a symlink")
        try:
            os.stat(name, dir_fd=root_fd, follow_symlinks=False)
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
            os.link(
                temporary,
                name,
                src_dir_fd=root_fd,
                dst_dir_fd=root_fd,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise CleanupUnproved("cleanup evidence publish target already exists") from exc
        except OSError as exc:
            raise CleanupUnproved(f"cleanup evidence cannot be published exclusively: {exc}") from exc
        try:
            final = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=root_fd)
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


def _verify_cleanup_observation_descriptor(descriptor: int, digest: str) -> bytes:
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise CleanupUnproved("cleanup evidence is not a regular file")
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


def _verify_cleanup_observation_under_root(root_fd: int, relative: Path, digest: str) -> bytes:
    if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise CleanupUnproved("cleanup evidence escapes evidence root")
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    descriptors: list[int] = []
    parent_fd = root_fd
    try:
        for part in relative.parts[:-1]:
            try:
                descriptor = os.open(part, flags, dir_fd=parent_fd)
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise CleanupUnproved("cleanup evidence uses a symlink") from exc
                raise CleanupUnproved(f"cleanup evidence path cannot be opened no-follow: {exc}") from exc
            descriptors.append(descriptor)
            parent_fd = descriptor
        try:
            descriptor = os.open(
                relative.parts[-1], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd
            )
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise CleanupUnproved("cleanup evidence uses a symlink") from exc
            raise CleanupUnproved(f"cleanup evidence cannot be opened no-follow: {exc}") from exc
        return _verify_cleanup_observation_descriptor(descriptor, digest)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def verify_cleanup_observation(path: Path, digest: str, *, evidence_root: Path | None = None) -> bytes:
    if evidence_root is not None:
        try:
            relative = path.relative_to(evidence_root)
        except ValueError as exc:
            raise CleanupUnproved("cleanup evidence escapes evidence root") from exc
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        try:
            root_fd = os.open(evidence_root, flags)
        except OSError as exc:
            raise CleanupUnproved(f"cleanup evidence root cannot be opened no-follow: {exc}") from exc
        try:
            return _verify_cleanup_observation_under_root(root_fd, relative, digest)
        finally:
            os.close(root_fd)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CleanupUnproved(f"cleanup evidence cannot be opened no-follow: {exc}") from exc
    return _verify_cleanup_observation_descriptor(descriptor, digest)


def _persist_observation(context: ProviderSessionContext, observation: ProviderCleanupObservation) -> tuple[Path, str]:
    payload = observation.model_dump_json(indent=2).encode() + b"\n"
    path = context.evidence_root / "provider-cleanup-observation.json"
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    try:
        root_fd = os.open(context.evidence_root, flags)
    except OSError as exc:
        raise CleanupUnproved(f"cleanup evidence root binding is unavailable: {exc}") from exc
    try:
        root_stat = os.fstat(root_fd)
        _atomic_write(root_fd, path.name, payload)
        try:
            current_root = os.lstat(context.evidence_root)
        except OSError as exc:
            raise CleanupUnproved(f"cleanup evidence root binding changed: {exc}") from exc
        if (root_stat.st_dev, root_stat.st_ino) != (current_root.st_dev, current_root.st_ino):
            raise CleanupUnproved("cleanup evidence root binding changed during persistence")
        digest = hashlib.sha256(payload).hexdigest()
        _verify_cleanup_observation_under_root(root_fd, Path(path.name), digest)
        return path, digest
    finally:
        os.close(root_fd)


def run_provider_lifecycle(
    *, provider: object, profile: object, context: ProviderSessionContext,
    binding: ProviderRuntimeBinding, requested_provider: str, operation: Callable[[object], object],
    finalized: Callable[[LifecycleEvidence], None] | None = None,
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
        if observation_path is not None and observation_digest is not None:
            try:
                if finalized is not None:
                    finalized(LifecycleEvidence(
                        run_id=context.run_id,
                        requested_provider=requested_provider,
                        session_id=context.session_id,
                        namespace=context.namespace,
                        provider_variant=observed_variant,
                        required_surface_ids=tuple(binding.required_surface_ids),
                        observation_path=observation_path,
                        observation_sha256=observation_digest,
                    ))
            except BaseException as exc:
                if primary is None and not isinstance(exc, Exception):
                    primary = exc
                else:
                    secondary.append(f"lifecycle evidence callback failed: {exc}")
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
    del requested_provider
    runner_parents = (
        (context.work_root.parent, context.evidence_root.parent)
        if (
            context.work_root.parent.name == "work"
            and context.evidence_root.parent.name == "evidence"
            and context.work_root.parent.parent == context.evidence_root.parent.parent
        )
        else ()
    )
    for root in (context.work_root, context.evidence_root):
        try:
            mode = os.lstat(root).st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISDIR(mode) and not stat.S_ISLNK(mode):
            import shutil
            shutil.rmtree(root)
        else:
            root.unlink()
        try:
            os.lstat(root)
        except FileNotFoundError:
            pass
        else:  # pragma: no cover - a concurrent reappearance is an environment fault
            raise ProviderConstructionFailure("constructor roots could not be proved absent")
    for parent in runner_parents:
        try:
            parent.rmdir()
        except FileNotFoundError:
            continue
        except OSError as exc:
            if exc.errno not in {errno.ENOTEMPTY, errno.EEXIST}:
                raise ProviderConstructionFailure(
                    f"constructor parent absence could not be proved: {exc}"
                ) from exc
        else:
            try:
                os.lstat(parent)
            except FileNotFoundError:
                pass
            else:  # pragma: no cover - a concurrent reappearance is an environment fault
                raise ProviderConstructionFailure("constructor parent could not be proved absent")
    if not isinstance(error, Exception):
        raise error
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


def _expected_index(
    expected_instances: tuple[Mapping[str, object], ...],
) -> dict[tuple[object, ...], Mapping[str, object]]:
    fields = {
        "run_id", "requested_provider", "session_id", "namespace", "provider_variant",
        "required_surface_ids", "observation_path", "observation_sha256",
    }
    expected: dict[tuple[object, ...], Mapping[str, object]] = {}
    for item in expected_instances:
        if not isinstance(item, Mapping) or set(item) != fields:
            raise LifecycleCompletenessError("expected lifecycle instance is not a strict full record")
        required = item["required_surface_ids"]
        if not isinstance(required, list) or required != sorted(set(required)) or not required:
            raise LifecycleCompletenessError("expected lifecycle required surfaces are invalid")
        string_fields = fields - {"required_surface_ids"}
        if any(not isinstance(item[name], str) or not item[name] for name in string_fields):
            raise LifecycleCompletenessError("expected lifecycle identity is incomplete")
        if not re_full_sha256(str(item["observation_sha256"])):
            raise LifecycleCompletenessError("expected lifecycle digest is invalid")
        path_value = str(item["observation_path"])
        if path_value.startswith("/") or path_value.endswith("/") or "\\" in path_value or any(part in {"", ".", ".."} for part in path_value.split("/")):
            raise LifecycleCompletenessError("expected cleanup observation path is unsafe")
        key = (
            item["run_id"], item["requested_provider"], item["session_id"], item["namespace"],
            item["provider_variant"], tuple(required), item["observation_path"], item["observation_sha256"],
        )
        if key in expected:
            raise LifecycleCompletenessError("duplicate expected lifecycle instance")
        expected[key] = item
    return expected


def validate_lifecycle_completeness(
    *, expected_instances: tuple[Mapping[str, object], ...], cleanup_records: list[dict[str, object]] | None,
    evidence_root: Path, run_dir: Path | None = None,
) -> None:
    records = _records_from_run(run_dir) if cleanup_records is None and run_dir is not None else cleanup_records
    if records is None:
        raise LifecycleCompletenessError("cleanup records are unavailable")
    seen: set[tuple[object, ...]] = set()
    referenced: set[Path] = set()
    for record in records:
        path_value = record.get("observation_path")
        digest = record.get("observation_sha256")
        trace_run_id = record.get("run_id")
        trace_requested_provider = record.get("requested_provider")
        if not all(isinstance(value, str) and value for value in (path_value, digest)):
            raise LifecycleCompletenessError("cleanup record lacks bound observation reference")
        if path_value.startswith("/") or path_value.endswith("/") or "\\" in path_value or any(part in {"", ".", ".."} for part in path_value.split("/")):
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
        key = (
            observation.run_id,
            observation.requested_provider,
            observation.session_id,
            observation.namespace,
            observation.provider_variant,
            tuple(observation.required_surface_ids),
            path_value,
            digest,
        )
        if key in seen:
            raise LifecycleCompletenessError("duplicate lifecycle cleanup record")
        seen.add(key)
        referenced.add(path.resolve())
        if (
            record.get("session_id") != observation.session_id
            or record.get("namespace") != observation.namespace
            or trace_run_id != observation.run_id
            or trace_requested_provider != observation.requested_provider
        ):
            raise LifecycleCompletenessError("cleanup observation binding disagrees with trace")
    expected = _expected_index(expected_instances)
    if seen != set(expected):
        raise LifecycleCompletenessError("missing lifecycle cleanup record")
    for candidate in evidence_root.rglob("*"):
        try:
            if not stat.S_ISREG(os.lstat(candidate).st_mode):
                continue
        except OSError:
            continue
        try:
            candidate_observation = ProviderCleanupObservation.model_validate_json(candidate.read_bytes())
        except Exception:
            continue
        if candidate_observation.artifact_type == "provider-cleanup-observation.v1" and candidate.resolve() not in referenced:
            raise LifecycleCompletenessError("orphan lifecycle cleanup observation")


def re_full_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)
