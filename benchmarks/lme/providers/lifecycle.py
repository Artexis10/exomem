"""Runner-owned direct-provider lifecycle and independently checked cleanup."""

from __future__ import annotations

import errno
import hashlib
import os
import stat
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from protocol.custody import (
    CustodyError,
    CustodyLimitExceeded,
    HeldDirectory,
    hold_directory,
)
from protocol.models import ProviderCleanupObservation

from .base import ProviderRuntimeBinding, ProviderSessionContext


class CleanupUnproved(RuntimeError):
    def __init__(
        self,
        message: str = "cleanup proof failed",
        *,
        fact: str = "cleanup_observation_failed",
    ) -> None:
        super().__init__(message)
        self.fact = fact


class LifecycleCompletenessError(RuntimeError):
    pass


class VariantDriftError(RuntimeError):
    pass


class ProviderConstructionFailure(RuntimeError):
    def __init__(self, primary: BaseException, secondary_facts: tuple[str, ...] = ()) -> None:
        super().__init__("direct provider construction failed")
        self.primary = primary
        self.fact = "provider_constructor_failed"
        self.secondary_facts = secondary_facts
        self.terminal_status = "INVALID"


class LifecycleRunError(RuntimeError):
    def __init__(self, primary: BaseException, fact: str, secondary_facts: tuple[str, ...]) -> None:
        super().__init__("direct provider lifecycle failed")
        self.primary = primary
        self.fact = fact
        self.secondary_facts = secondary_facts
        # Compatibility attribute retained without rendering captured objects.
        self.secondary_failures = secondary_facts
        self.terminal_status = "INVALID"


MAX_CLEANUP_OBSERVATION_BYTES = 1_048_576
RETIRE_MAX_ENTRIES = 100_000
RETIRE_MAX_DEPTH = 64


@dataclass(frozen=True)
class LifecycleCustody:
    """Runner-owned held capabilities for one direct-provider instance."""

    session: HeldDirectory
    work: HeldDirectory
    evidence: HeldDirectory

    def assert_bound(self) -> None:
        self.session.assert_bound()
        self.work.assert_bound()
        self.evidence.assert_bound()

    def close(self) -> None:
        self.evidence.close()
        self.work.close()
        self.session.close()


def _compatibility_custody(context: ProviderSessionContext) -> LifecycleCustody:
    """Retain legacy unit-test paths; the direct runner always supplies custody."""

    session_root = context.work_root.parent
    session = hold_directory(session_root, logical_ref=Path("."))
    try:
        work = hold_directory(context.work_root, logical_ref=context.work_ref)
        try:
            evidence = hold_directory(context.evidence_root, logical_ref=context.evidence_ref)
        except BaseException:
            work.close()
            raise
    except BaseException:
        session.close()
        raise
    return LifecycleCustody(session=session, work=work, evidence=evidence)


def _capture_failure(
    primary: BaseException | None,
    primary_fact: str | None,
    secondary_facts: list[str],
    failure: BaseException,
    fact: str,
) -> tuple[BaseException, str]:
    if primary is None:
        return failure, fact
    if isinstance(primary, Exception) and not isinstance(failure, Exception):
        if primary_fact is not None:
            secondary_facts.append(primary_fact)
        return failure, fact
    secondary_facts.append(fact)
    assert primary_fact is not None
    return primary, primary_fact


def _failure_fact(failure: BaseException, fallback: str) -> str:
    if isinstance(failure, VariantDriftError):
        return "provider_variant_drift"
    return fallback


_VARIANTS: dict[str, str] = {}
_PUBLISHED_OBSERVATION_IDENTITIES: dict[tuple[str, str], tuple[int, int]] = {}


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
    observation_ref: Path
    observation_sha256: str
    observation_identity: tuple[int, int]

    def expected_instance(self, run_dir: Path) -> dict[str, object]:
        _PUBLISHED_OBSERVATION_IDENTITIES[(str(run_dir), self.observation_ref.as_posix())] = self.observation_identity
        return {
            "run_id": self.run_id,
            "requested_provider": self.requested_provider,
            "session_id": self.session_id,
            "namespace": self.namespace,
            "provider_variant": self.provider_variant,
            "required_surface_ids": list(self.required_surface_ids),
            "observation_path": self.observation_ref.as_posix(),
            "observation_sha256": self.observation_sha256,
        }

    def trace_record(self, run_dir: Path) -> dict[str, object]:
        return {
            "record": "cleanup",
            "run_id": self.run_id,
            "session_id": self.session_id,
            "namespace": self.namespace,
            "requested_provider": self.requested_provider,
            "observation_path": self.observation_ref.as_posix(),
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
        raise VariantDriftError("observed provider variant drift")
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
        raise CleanupUnproved("unobservable cleanup surface") from exc
    keys = {(item["kind"], item.get("path", item.get("expected_namespace", ""))) for item in typed}
    if len(keys) != len(typed):
        raise CleanupUnproved("duplicate cleanup surface")
    required = {
        "provider-state": ("provider-state", ""),
        "namespace-membership": ("namespace-membership", context.namespace),
        "session-root": ("path-lstat", "session-root"),
        "work-root": ("path-lstat", "work"),
    }
    try:
        expected = {required[surface] for surface in binding.required_surface_ids}
    except KeyError as exc:
        raise CleanupUnproved("cleanup observation declares an unknown surface") from exc
    if keys != expected:
        raise CleanupUnproved("cleanup observation surfaces differ from the declaration")
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


def _verify_cleanup_observation_descriptor(
    descriptor: int, digest: str, *, max_bytes: int = MAX_CLEANUP_OBSERVATION_BYTES,
) -> tuple[bytes, tuple[int, int]]:
    opened = os.fstat(descriptor)
    if not stat.S_ISREG(opened.st_mode):
        os.close(descriptor)
        raise CleanupUnproved("cleanup evidence is not a regular file")
    if opened.st_size > max_bytes:
        os.close(descriptor)
        raise CleanupUnproved("cleanup evidence exceeds bounded read limit")
    chunks: list[bytes] = []
    remaining = max_bytes + 1
    try:
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(descriptor)
    payload = b"".join(chunks)
    if len(payload) > max_bytes:
        raise CleanupUnproved("cleanup evidence exceeds bounded read limit")
    actual = hashlib.sha256(payload).hexdigest()
    if actual != digest:
        raise ValueError("cleanup observation digest differs")
    try:
        ProviderCleanupObservation.model_validate_json(payload)
    except Exception as exc:
        raise CleanupUnproved("cleanup observation schema is invalid") from exc
    return payload, (opened.st_dev, opened.st_ino)


def _verify_cleanup_observation_under_root(
    root_fd: int, relative: Path, digest: str,
) -> tuple[bytes, tuple[int, int]]:
    if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise CleanupUnproved("cleanup evidence escapes evidence root")
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptors: list[int] = []
    parent_fd = root_fd
    try:
        for part in relative.parts[:-1]:
            try:
                descriptor = os.open(part, flags, dir_fd=parent_fd)
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise CleanupUnproved("cleanup evidence uses a symlink") from exc
                raise CleanupUnproved("cleanup evidence path cannot be opened no-follow") from exc
            descriptors.append(descriptor)
            parent_fd = descriptor
        try:
            descriptor = os.open(
                relative.parts[-1], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0), dir_fd=parent_fd
            )
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise CleanupUnproved("cleanup evidence uses a symlink") from exc
            raise CleanupUnproved("cleanup evidence cannot be opened no-follow") from exc
        return _verify_cleanup_observation_descriptor(descriptor, digest)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _verify_cleanup_observation_under_custody(
    evidence: HeldDirectory,
    relative: Path,
    digest: str,
) -> bytes:
    evidence.assert_bound()
    payload, identity = _verify_cleanup_observation_under_root(evidence.fd, relative, digest)
    evidence.assert_bound()
    return payload, identity


def verify_cleanup_observation(path: Path, digest: str, *, evidence_root: Path | None = None) -> bytes:
    if evidence_root is not None:
        try:
            relative = path.relative_to(evidence_root)
        except ValueError as exc:
            raise CleanupUnproved("cleanup evidence escapes evidence root") from exc
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            root_fd = os.open(evidence_root, flags)
        except OSError as exc:
            raise CleanupUnproved("cleanup evidence root cannot be opened no-follow") from exc
        try:
            return _verify_cleanup_observation_under_root(root_fd, relative, digest)[0]
        finally:
            os.close(root_fd)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CleanupUnproved("cleanup evidence cannot be opened no-follow") from exc
    return _verify_cleanup_observation_descriptor(descriptor, digest)[0]


def _persist_observation(
    context: ProviderSessionContext,
    custody: LifecycleCustody,
    observation: ProviderCleanupObservation,
) -> tuple[Path, Path, str, tuple[int, int]]:
    payload = observation.model_dump_json(indent=2).encode() + b"\n"
    path = context.evidence_root / "provider-cleanup-observation.json"
    try:
        published = custody.evidence.publish_exclusive(
            path.name, payload, max_bytes=MAX_CLEANUP_OBSERVATION_BYTES,
        )
    except (CustodyError, CustodyLimitExceeded) as exc:
        raise CleanupUnproved(
            "cleanup evidence publication failed",
            fact="cleanup_evidence_publish_failed",
        ) from exc
    if published.payload != payload or published.sha256 != hashlib.sha256(payload).hexdigest():
        raise CleanupUnproved(
            "cleanup evidence publication disagreed",
            fact="cleanup_evidence_publish_failed",
        )
    try:
        ProviderCleanupObservation.model_validate_json(published.payload)
    except Exception as exc:
        raise CleanupUnproved(
            "cleanup observation schema is invalid",
            fact="cleanup_evidence_publish_failed",
        ) from exc
    reference = context.evidence_ref / path.name
    return path, reference, published.sha256, (published.device, published.inode)


def _assert_published_observation_identity(
    evidence: HeldDirectory,
    relative: Path,
    identity: tuple[int, int],
) -> None:
    try:
        status = os.stat(relative.name, dir_fd=evidence.fd, follow_symlinks=False)
    except OSError as exc:
        raise CleanupUnproved(
            "cleanup evidence binding was lost",
            fact="cleanup_evidence_publish_failed",
        ) from exc
    if not stat.S_ISREG(status.st_mode) or identity != (status.st_dev, status.st_ino):
        raise CleanupUnproved(
            "cleanup evidence binding was lost",
            fact="cleanup_evidence_publish_failed",
        )


def run_provider_lifecycle(
    *, provider: object, profile: object, context: ProviderSessionContext,
    binding: ProviderRuntimeBinding, requested_provider: str, operation: Callable[[object], object],
    finalized: Callable[[LifecycleEvidence], None] | None = None,
    custody: LifecycleCustody | None = None,
    setup_completed: Callable[[], None] | None = None,
    variant_observed: Callable[[str], None] | None = None,
) -> tuple[object, Path, str, str]:
    """Own setup through post-publication absence authorization."""
    primary: BaseException | None = None
    primary_fact: str | None = None
    secondary_facts: list[str] = []
    result: object | None = None
    observed_variant: str | None = None
    observation_path: Path | None = None
    observation_ref: Path | None = None
    observation_digest: str | None = None
    observation_identity: tuple[int, int] | None = None
    owns_custody = custody is None
    try:
        active_custody = custody or _compatibility_custody(context)
    except CustodyError as exc:
        raise CleanupUnproved("custody root is unsafe, unavailable, or a symlink") from exc

    def capture(failure: BaseException, fact: str) -> None:
        nonlocal primary, primary_fact
        primary, primary_fact = _capture_failure(
            primary, primary_fact, secondary_facts, failure, fact,
        )

    try:
        try:
            getattr(provider, "setup")(profile, context)
            if setup_completed is not None:
                setup_completed()
            observed_variant = bind_observed_variant(context, provider)
            if variant_observed is not None:
                variant_observed(observed_variant)
        except BaseException as failure:
            capture(failure, _failure_fact(failure, "provider_setup_failed"))
        if primary is None:
            try:
                result = operation(provider)
                bind_observed_variant(context, provider)
            except BaseException as failure:
                capture(failure, _failure_fact(failure, "provider_operation_failed"))
        try:
            getattr(provider, "cleanup")()
            if observed_variant is not None:
                bind_observed_variant(context, provider)
        except BaseException as failure:
            capture(failure, _failure_fact(failure, "provider_cleanup_failed"))

        first: tuple[dict[str, object], ...] | None = None
        if observed_variant is not None:
            try:
                first = _normalize(binding, context, provider)
            except BaseException as failure:
                capture(failure, "cleanup_observation_failed")

        if first is not None:
            observation = ProviderCleanupObservation(
                run_id=context.run_id,
                session_id=context.session_id,
                requested_provider=requested_provider,
                provider_variant=observed_variant,
                namespace=context.namespace,
                cleanup_called=True,
                required_surface_ids=sorted(binding.required_surface_ids),
                observations=list(first),
            )
            try:
                observation_path, observation_ref, observation_digest, observation_identity = _persist_observation(
                    context, active_custody, observation,
                )
            except BaseException as failure:
                capture(failure, "cleanup_evidence_publish_failed")

        second: tuple[dict[str, object], ...] | None = None
        if observation_path is not None:
            try:
                second = _normalize(binding, context, provider)
                assert observation_identity is not None
                _assert_published_observation_identity(
                    active_custody.evidence, observation_path.relative_to(context.evidence_root), observation_identity,
                )
            except BaseException as failure:
                capture(failure, "cleanup_observation_failed")

        observation_valid = (
            first is not None
            and second is not None
            and first == second
            and _absence(first)
            and _absence(second)
        )
        if first is not None and not observation_valid:
            synthetic = CleanupUnproved(
                "cleanup observation did not prove stable absence: state remains or changed"
            )
            capture(synthetic, "cleanup_observation_failed")

        bindings_valid = True
        try:
            active_custody.assert_bound()
            if observation_path is not None:
                assert observation_identity is not None
                _assert_published_observation_identity(
                    active_custody.evidence, observation_path.relative_to(context.evidence_root), observation_identity,
                )
        except BaseException:
            bindings_valid = False
            capture(
                CleanupUnproved("custody binding was lost", fact="custody_binding_lost"),
                "custody_binding_lost",
            )

        authorized = (
            observation_valid
            and bindings_valid
            and observation_path is not None
            and observation_ref is not None
            and observation_digest is not None
            and observation_identity is not None
            and observed_variant is not None
        )
        if authorized and finalized is not None:
            try:
                finalized(LifecycleEvidence(
                    run_id=context.run_id,
                    requested_provider=requested_provider,
                    session_id=context.session_id,
                    namespace=context.namespace,
                    provider_variant=observed_variant,
                    required_surface_ids=tuple(sorted(binding.required_surface_ids)),
                    observation_path=observation_path,
                    observation_ref=observation_ref,
                    observation_sha256=observation_digest,
                    observation_identity=observation_identity,
                ))
            except BaseException as failure:
                capture(failure, "lifecycle_callback_failed")
            try:
                active_custody.assert_bound()
                _assert_published_observation_identity(
                    active_custody.evidence, observation_path.relative_to(context.evidence_root), observation_identity,
                )
            except BaseException as failure:
                if isinstance(failure, CleanupUnproved):
                    capture(failure, failure.fact)
                else:
                    capture(
                        CleanupUnproved("custody binding was lost", fact="custody_binding_lost"),
                        "custody_binding_lost",
                    )

        if primary is not None:
            assert primary_fact is not None
            if not isinstance(primary, Exception):
                raise primary
            if isinstance(primary, CleanupUnproved):
                primary.fact = primary_fact
                raise primary
            raise LifecycleRunError(primary, primary_fact, tuple(secondary_facts)) from primary
        assert observation_path is not None and observation_digest is not None and observed_variant is not None
        return result, observation_path, observation_digest, observed_variant
    finally:
        if owns_custody:
            active_custody.close()


def terminalize_constructor_failure(
    context: ProviderSessionContext, *, requested_provider: str, error: BaseException,
    custody: LifecycleCustody | None = None,
) -> None:
    del requested_provider
    primary = error
    primary_fact = "provider_constructor_failed"
    secondary_facts: list[str] = []
    active_custody = custody or _compatibility_custody(context)
    for root in (active_custody.work, active_custody.evidence):
        try:
            root.retire(max_entries=RETIRE_MAX_ENTRIES, max_depth=RETIRE_MAX_DEPTH)
        except BaseException as failure:
            primary, primary_fact = _capture_failure(
                primary, primary_fact, secondary_facts, failure,
                "constructor_root_retirement_failed",
            )
    try:
        active_custody.session.retire(max_entries=RETIRE_MAX_ENTRIES, max_depth=RETIRE_MAX_DEPTH)
    except BaseException as failure:
        primary, primary_fact = _capture_failure(
            primary, primary_fact, secondary_facts, failure,
            "constructor_session_retirement_failed",
        )
    active_custody.close()
    if not isinstance(primary, Exception):
        raise primary
    raise ProviderConstructionFailure(primary, tuple(secondary_facts)) from primary


def _records_from_run(run_dir: Path) -> tuple[list[dict[str, object]], tuple[str, ...]]:
    from protocol.trace import TraceError, load_trace_inventory

    try:
        inventory = load_trace_inventory(run_dir, required_schema_version=2)
    except TraceError as exc:
        raise LifecycleCompletenessError("lifecycle trace inventory is invalid") from exc
    records = [trace.cleanup.model_dump(mode="json") for trace in inventory]
    return records, tuple(trace.session_id for trace in inventory)


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


def _observed_surface_ids(observation: ProviderCleanupObservation) -> list[str]:
    surface_ids: list[str] = []
    for item in observation.observations:
        if item.kind == "provider-state":
            surface_ids.append("provider-state")
        elif item.kind == "namespace-membership":
            surface_ids.append("namespace-membership")
        elif item.kind == "path-lstat" and item.path == "session-root":
            surface_ids.append("session-root")
        elif item.kind == "path-lstat" and item.path == "work":
            surface_ids.append("work-root")
        else:
            raise LifecycleCompletenessError("cleanup observation contains an unknown surface")
    return sorted(surface_ids)


def validate_lifecycle_completeness(
    *, expected_instances: tuple[Mapping[str, object], ...], cleanup_records: list[dict[str, object]] | None,
    evidence_root: Path, run_dir: Path | None = None,
    lifecycle_attempts: tuple[Mapping[str, object], ...] | None = None,
    manifest_run_id: str | None = None,
    manifest_provider_variant: str | None = None,
    environment_provider_variant: str | None = None,
) -> None:
    trace_sessions: tuple[str, ...] | None = None
    if cleanup_records is None and run_dir is not None:
        records, trace_sessions = _records_from_run(run_dir)
    else:
        records = cleanup_records
    if records is None:
        raise LifecycleCompletenessError("cleanup records are unavailable")
    run: HeldDirectory | None = None
    evidence: HeldDirectory | None = None
    try:
        if run_dir is not None:
            try:
                run = hold_directory(run_dir, logical_ref=Path("."))
                evidence = run.open_dir("evidence", logical_ref=Path("evidence"))
            except CustodyError as exc:
                raise LifecycleCompletenessError("lifecycle evidence root is unsafe") from exc
        seen: set[tuple[object, ...]] = set()
        referenced: set[Path] = set()
        verified_identities: dict[Path, tuple[int, int]] = {}
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
                if evidence is None:
                    payload = verify_cleanup_observation(path, digest, evidence_root=evidence_root)
                    identity = None
                else:
                    relative = Path(path_value).relative_to("evidence")
                    payload, identity = _verify_cleanup_observation_under_custody(evidence, relative, digest)
                    published_identity = _PUBLISHED_OBSERVATION_IDENTITIES.get((str(run_dir), path_value))
                    if published_identity is not None and identity != published_identity:
                        raise LifecycleCompletenessError("cleanup observation binding differs from publication")
                observation = ProviderCleanupObservation.model_validate_json(payload)
            except Exception as exc:
                raise LifecycleCompletenessError("lifecycle cleanup observation is invalid") from exc
            if not observation.cleanup_called or not _absence([item.model_dump(mode="json") for item in observation.observations]):
                raise LifecycleCompletenessError("cleanup observation does not prove absence")
            if not all(isinstance(value, str) and value for value in (trace_run_id, trace_requested_provider)):
                raise LifecycleCompletenessError("cleanup record lacks bound observation reference")
            if observation.required_surface_ids != sorted(set(observation.required_surface_ids)) or not observation.required_surface_ids:
                raise LifecycleCompletenessError("cleanup observation required surfaces are invalid")
            if observation.required_surface_ids != _observed_surface_ids(observation):
                raise LifecycleCompletenessError("cleanup observation surfaces differ from the declaration")
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
            referenced.add(Path(path_value))
            if identity is not None:
                verified_identities[Path(path_value)] = identity
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
        expected_sessions = tuple(sorted(str(item["session_id"]) for item in expected.values()))
        if trace_sessions is not None and tuple(sorted(trace_sessions)) != expected_sessions:
            raise LifecycleCompletenessError("lifecycle trace topology differs from expected instances")

        if manifest_run_id is not None:
            if not isinstance(manifest_provider_variant, str) or not manifest_provider_variant:
                raise LifecycleCompletenessError("manifest lifecycle variant is unavailable")
            if environment_provider_variant != manifest_provider_variant:
                raise LifecycleCompletenessError("environment and manifest provider variants disagree")
            for item in expected.values():
                if item["run_id"] != manifest_run_id:
                    raise LifecycleCompletenessError("expected lifecycle run differs from manifest")
                if item["provider_variant"] != manifest_provider_variant:
                    raise LifecycleCompletenessError("expected lifecycle variant differs from manifest")

        if lifecycle_attempts is not None:
            factory_sessions: list[str] = []
            seen_attempts: set[str] = set()
            for attempt in lifecycle_attempts:
                if not isinstance(attempt, Mapping):
                    raise LifecycleCompletenessError("lifecycle attempt is invalid")
                session_id = attempt.get("internal_session_id")
                if not isinstance(session_id, str) or not session_id or session_id in seen_attempts:
                    raise LifecycleCompletenessError("lifecycle attempt identity is invalid or duplicated")
                seen_attempts.add(session_id)
                if attempt.get("factory_returned") is True:
                    factory_sessions.append(session_id)
                    if (
                        attempt.get("setup_completed") is not True
                        or attempt.get("provider_variant") != manifest_provider_variant
                        or attempt.get("failure_code") is not None
                    ):
                        raise LifecycleCompletenessError("factory-returned lifecycle attempt is not authorizing")
            if tuple(sorted(factory_sessions)) != expected_sessions:
                raise LifecycleCompletenessError("factory-returned attempts differ from lifecycle evidence")

        if evidence is not None:
            files = _inventory_evidence_files(evidence, verified_identities=verified_identities)
            evidence.assert_bound()
            assert run is not None
            run.assert_bound()
            if set(files) != referenced or any(
                files[path] != identity for path, identity in verified_identities.items()
            ):
                raise LifecycleCompletenessError("orphan or missing lifecycle cleanup observation")
    finally:
        if evidence is not None:
            evidence.close()
        if run is not None:
            run.close()


def _inventory_evidence_files(
    evidence: HeldDirectory,
    *,
    verified_identities: Mapping[Path, tuple[int, int]],
) -> dict[Path, tuple[int, int]]:
    files: dict[Path, tuple[int, int]] = {}
    remaining = [RETIRE_MAX_ENTRIES]

    def visit(directory: HeldDirectory, relative: Path, depth: int) -> None:
        if depth < 0:
            raise LifecycleCompletenessError("lifecycle evidence depth limit exceeded")
        try:
            with os.scandir(directory.fd) as iterator:
                names = sorted(entry.name for entry in iterator)
        except OSError as exc:
            raise LifecycleCompletenessError("lifecycle evidence cannot be inventoried") from exc
        for name in names:
            remaining[0] -= 1
            if remaining[0] < 0:
                raise LifecycleCompletenessError("lifecycle evidence entry limit exceeded")
            try:
                status = os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
            except OSError as exc:
                raise LifecycleCompletenessError("lifecycle evidence entry is unavailable") from exc
            child_ref = relative / name
            if stat.S_ISDIR(status.st_mode):
                child: HeldDirectory | None = None
                try:
                    child = directory.open_dir(name, logical_ref=child_ref)
                    visit(child, child_ref, depth - 1)
                except CustodyError as exc:
                    raise LifecycleCompletenessError("lifecycle evidence traversal is unsafe") from exc
                finally:
                    if child is not None:
                        child.close()
            elif stat.S_ISREG(status.st_mode):
                if status.st_size > MAX_CLEANUP_OBSERVATION_BYTES:
                    raise LifecycleCompletenessError("lifecycle observation exceeds bounded read limit")
                try:
                    _payload, identity = directory.read_regular_bounded(
                        name,
                        max_bytes=MAX_CLEANUP_OBSERVATION_BYTES,
                        with_identity=True,
                    )
                except CustodyError as exc:
                    raise LifecycleCompletenessError("lifecycle observation cannot be read safely") from exc
                if identity != (status.st_dev, status.st_ino) or (
                    child_ref in verified_identities
                    and identity != verified_identities[child_ref]
                ):
                    raise LifecycleCompletenessError("lifecycle observation binding changed during inventory")
                files[child_ref] = identity
            else:
                raise LifecycleCompletenessError("lifecycle evidence contains a nonregular entry")
        directory.assert_bound()

    try:
        visit(evidence, Path("evidence"), RETIRE_MAX_DEPTH)
        evidence.assert_bound()
    except CustodyError as exc:
        raise LifecycleCompletenessError("lifecycle evidence root is unsafe") from exc
    return files


def re_full_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)
