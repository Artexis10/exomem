#!/usr/bin/env python3
"""Record and verify digest-authoritative hosted OCI image candidates."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NoReturn, cast

MAX_BUNDLE_BYTES = 16 * 1024 * 1024
MAX_CANDIDATE_BYTES = 128 * 1024
VERIFY_TIMEOUT_SECONDS = 120
SCHEMA_VERSION = 1
RECORDS_COMPATIBILITY_SCHEMA_VERSION = 2
SOURCE_REPOSITORY = "Artexis10/exomem"
SLSA_PREDICATE = "https://slsa.dev/provenance/v1"
RUNTIME_SIGNER_WORKFLOW = f"{SOURCE_REPOSITORY}/.github/workflows/release-please.yml"
PROVISIONER_SIGNER_WORKFLOW = f"{SOURCE_REPOSITORY}/.github/workflows/publish-hosted-provisioner.yml"
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:([0-9a-f]{64})$")
_SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$")
_TAG_REF = re.compile(r"^refs/tags/(v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?)$")
_RUN_NUMBER = re.compile(r"^[1-9][0-9]*$")
_RFC3339_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_RECORDS_COMPATIBILITY_MAX_TTL = timedelta(hours=24)
_RUNTIME_TARGET_FIELDS = {
    "releaseVersion", "protocolVersion", "agentProfile", "gatewayContractDigest",
    "commandFingerprint", "schemaDigest",
}


class CandidateError(ValueError):
    """Raised when hosted image evidence does not meet the admission policy."""


def _error(message: str) -> NoReturn:
    raise CandidateError(message)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _error(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_regular(path: Path, *, label: str, maximum: int) -> bytes:
    try:
        information = path.lstat()
    except OSError as exc:
        _error(f"cannot inspect {label}: {exc}")
    if stat.S_ISLNK(information.st_mode):
        _error(f"{label} must not be a symlink")
    if not stat.S_ISREG(information.st_mode):
        _error(f"{label} must be a regular file")
    if information.st_size > maximum:
        _error(f"{label} exceeds maximum size of {maximum} bytes")
    try:
        data = path.read_bytes()
    except OSError as exc:
        _error(f"cannot read {label}: {exc}")
    if len(data) > maximum:
        _error(f"{label} exceeds maximum size of {maximum} bytes")
    return data


def _json_object(path: Path, *, label: str, maximum: int) -> dict[str, object]:
    try:
        raw = _read_regular(path, label=label, maximum=maximum)
        decoded = raw.decode("utf-8")
        value = json.loads(decoded, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _error(f"invalid {label} JSON: {exc}")
    if not isinstance(value, dict):
        _error(f"{label} must be a JSON object")
    return cast(dict[str, object], value)


def _mapping(value: object, *, label: str, fields: set[str]) -> dict[str, object]:
    if not isinstance(value, dict):
        _error(f"{label} must be an object")
    actual = set(value)
    unknown = actual - fields
    missing = fields - actual
    if unknown:
        _error(f"{label} contains unknown field(s): {', '.join(sorted(unknown))}")
    if missing:
        _error(f"{label} is missing field(s): {', '.join(sorted(missing))}")
    return cast(dict[str, object], value)


def _string(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        _error(f"{label} must be a non-empty string")
    return value


def _commit(value: object, *, label: str) -> str:
    result = _string(value, label=label)
    if not _COMMIT.fullmatch(result):
        _error(f"{label} must be a lowercase 40-character commit")
    return result


def _digest(value: object, *, label: str) -> str:
    result = _string(value, label=label)
    if not _DIGEST.fullmatch(result):
        _error(f"{label} must be a sha256 digest")
    return result


def _run_number(value: object, *, label: str) -> str:
    result = _string(value, label=label)
    if not _RUN_NUMBER.fullmatch(result):
        _error(f"{label} must be a positive decimal string")
    return result


def _rfc3339_utc(value: object, *, label: str) -> datetime:
    raw = _string(value, label=label)
    if not _RFC3339_UTC.fullmatch(raw):
        _error(f"{label} must be canonical RFC3339 UTC")
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise CandidateError(f"{label} must be canonical RFC3339 UTC") from exc


def _runtime_target(value: object, *, label: str) -> dict[str, object]:
    target = _mapping(value, label=label, fields=_RUNTIME_TARGET_FIELDS)
    for field in ("releaseVersion", "protocolVersion", "agentProfile"):
        _string(target[field], label=f"{label}.{field}")
    for field in ("gatewayContractDigest", "commandFingerprint", "schemaDigest"):
        digest = _string(target[field], label=f"{label}.{field}")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            _error(f"{label}.{field} must be a sha256 hex hash")
    return target


def validate_records_compatibility_claim(
    candidate: dict[str, object], *, require_fresh: bool = False, now: datetime | None = None
) -> dict[str, object]:
    """Validate the signed v2 reader-status claim bound to its candidate workflow."""

    workflow = _mapping(
        candidate.get("workflow"),
        label="workflow",
        fields={
            "producerRepository", "signerWorkflow", "signerWorkflowDigest", "oidcSourceRef",
            "oidcSourceCommit", "event", "runId", "runAttempt",
        },
    )
    records = _mapping(
        candidate.get("recordsCompatibility"),
        label="recordsCompatibility",
        fields={
            "profile", "recordsReaderVersion", "lifecycleActionsEnabled", "issuedAt", "expiresAt",
            "signerWorkflow", "signerWorkflowDigest", "runtimeTarget",
        },
    )
    target = _runtime_target(records["runtimeTarget"], label="recordsCompatibility.runtimeTarget")
    release = _mapping(candidate.get("release"), label="release", fields={"tag", "version"})
    issued_at = _rfc3339_utc(records["issuedAt"], label="recordsCompatibility.issuedAt")
    expires_at = _rfc3339_utc(records["expiresAt"], label="recordsCompatibility.expiresAt")
    if (
        records["profile"] != "hosted-alpha-agent-v1"
        or records["recordsReaderVersion"] != 2
        or records["lifecycleActionsEnabled"] is not False
        or records["signerWorkflow"] != workflow["signerWorkflow"]
        or records["signerWorkflowDigest"] != workflow["signerWorkflowDigest"]
        or expires_at <= issued_at
        or expires_at - issued_at > _RECORDS_COMPATIBILITY_MAX_TTL
        or target["releaseVersion"] != release["version"]
        or target["agentProfile"] != records["profile"]
    ):
        _error("runtime Records compatibility is invalid")
    if require_fresh:
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            _error("Records compatibility freshness time is invalid")
        current = current.astimezone(UTC)
        if not issued_at <= current <= expires_at:
            _error("runtime Records compatibility is stale")
    return records


def _image_reference(value: object) -> tuple[str, str]:
    reference = _string(value, label="image.reference")
    repository, separator, digest = reference.partition("@")
    if separator != "@" or not repository or "@" in digest:
        _error("image.reference must be an immutable repository-at-digest reference")
    return repository, _digest(digest, label="image.digest")


def _candidate_from_flags(args: argparse.Namespace) -> dict[str, object]:
    repository, digest = _image_reference(args.image)
    source_ref = args.source_ref
    component = args.component
    release: object = None
    if component == "runtime":
        if args.release is None:
            _error("runtime candidates require --release")
        release = {"tag": f"v{args.release}", "version": args.release}
    elif args.release is not None:
        _error("provisioner candidates must not supply --release")
    candidate: dict[str, object] = {
        "schemaVersion": SCHEMA_VERSION,
        "kind": component,
        "image": {
            "repository": repository,
            "digest": digest,
            "reference": args.image,
            "discoveryTag": args.discovery_tag,
        },
        "source": {
            "repository": args.source_repository,
            "checkoutRef": source_ref,
            "commit": args.source_commit,
        },
        "release": release,
        "workflow": {
            "producerRepository": args.producer_repository,
            "signerWorkflow": args.producer_workflow,
            "signerWorkflowDigest": args.producer_workflow_commit,
            "oidcSourceRef": args.producer_oidc_source_ref,
            "oidcSourceCommit": args.producer_oidc_source_commit,
            "event": args.producer_event,
            "runId": args.run_id,
            "runAttempt": args.run_attempt,
        },
        "attestation": {
            "predicateType": SLSA_PREDICATE,
            "subjectName": repository,
            "subjectDigest": digest,
            "bundleSha256": "",
        },
        "storage": {
            "kind": args.storage_kind,
            "subject": args.image,
            "uri": args.storage_uri,
        },
    }
    records_values = (
        args.records_profile,
        args.records_reader_version,
        args.lifecycle_actions_enabled,
        args.records_issued_at,
        args.records_expires_at,
        args.records_runtime_target_json,
    )
    if any(value is not None for value in records_values):
        if any(value is None for value in records_values):
            _error("Records compatibility flags must be supplied together")
        if component != "runtime":
            _error("Records compatibility flags are valid only for runtime candidates")
        candidate["schemaVersion"] = RECORDS_COMPATIBILITY_SCHEMA_VERSION
        candidate["recordsCompatibility"] = {
            "profile": args.records_profile,
            "recordsReaderVersion": args.records_reader_version,
            "lifecycleActionsEnabled": args.lifecycle_actions_enabled == "true",
            "issuedAt": args.records_issued_at,
            "expiresAt": args.records_expires_at,
            "signerWorkflow": args.producer_workflow,
            "signerWorkflowDigest": args.producer_workflow_commit,
            "runtimeTarget": _json_object_from_string(
                args.records_runtime_target_json, label="Records runtime target"
            ),
        }
        validate_records_compatibility_claim(candidate, require_fresh=True)
    return candidate


def _json_object_from_string(value: str, *, label: str) -> dict[str, object]:
    try:
        parsed = json.loads(value, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        _error(f"{label} must be JSON: {exc}")
    if not isinstance(parsed, dict):
        _error(f"{label} must be a JSON object")
    return cast(dict[str, object], parsed)


def validate_candidate(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        _error("candidate must be an object")
    schema_version = value.get("schemaVersion")
    fields = {
        "schemaVersion", "kind", "image", "source", "release", "workflow", "attestation", "storage"
    }
    if schema_version == RECORDS_COMPATIBILITY_SCHEMA_VERSION:
        fields.add("recordsCompatibility")
    candidate = _mapping(
        value,
        label="candidate",
        fields=fields,
    )
    if candidate["schemaVersion"] not in {SCHEMA_VERSION, RECORDS_COMPATIBILITY_SCHEMA_VERSION}:
        _error("candidate.schemaVersion is unsupported")
    kind = _string(candidate["kind"], label="candidate.kind")
    if kind not in {"runtime", "provisioner"}:
        _error("candidate.kind must be runtime or provisioner")

    image = _mapping(
        candidate["image"],
        label="image",
        fields={"repository", "digest", "reference", "discoveryTag"},
    )
    repository = _string(image["repository"], label="image.repository")
    digest = _digest(image["digest"], label="image.digest")
    reference = _string(image["reference"], label="image.reference")
    if reference != f"{repository}@{digest}":
        _error("image.reference must be the exact immutable repository-at-digest reference")
    discovery_tag = _string(image["discoveryTag"], label="image.discoveryTag")

    source = _mapping(
        candidate["source"],
        label="source",
        fields={"repository", "checkoutRef", "commit"},
    )
    if source["repository"] != SOURCE_REPOSITORY:
        _error("source.repository is not approved")
    checkout_ref = _string(source["checkoutRef"], label="source.checkoutRef")
    source_commit = _commit(source["commit"], label="source.commit")

    workflow = _mapping(
        candidate["workflow"],
        label="workflow",
        fields={
            "producerRepository",
            "signerWorkflow",
            "signerWorkflowDigest",
            "oidcSourceRef",
            "oidcSourceCommit",
            "event",
            "runId",
            "runAttempt",
        },
    )
    if workflow["producerRepository"] != SOURCE_REPOSITORY:
        _error("workflow.producerRepository is not approved")
    signer_workflow = _string(workflow["signerWorkflow"], label="workflow.signerWorkflow")
    _commit(workflow["signerWorkflowDigest"], label="workflow.signerWorkflowDigest")
    oidc_ref = _string(workflow["oidcSourceRef"], label="workflow.oidcSourceRef")
    oidc_commit = _commit(workflow["oidcSourceCommit"], label="workflow.oidcSourceCommit")
    event = _string(workflow["event"], label="workflow.event")
    if event not in {"push", "workflow_dispatch"}:
        _error("workflow.event is not approved")
    _run_number(workflow["runId"], label="workflow.runId")
    _run_number(workflow["runAttempt"], label="workflow.runAttempt")

    attestation = _mapping(
        candidate["attestation"],
        label="attestation",
        fields={"predicateType", "subjectName", "subjectDigest", "bundleSha256"},
    )
    if attestation["predicateType"] != SLSA_PREDICATE:
        _error("attestation.predicateType is not approved")
    if attestation["subjectName"] != repository or attestation["subjectDigest"] != digest:
        _error("attestation subject must exactly equal image subject")
    bundle_sha256 = _string(attestation["bundleSha256"], label="attestation.bundleSha256")
    if not re.fullmatch(r"[0-9a-f]{64}", bundle_sha256):
        _error("attestation.bundleSha256 must be a sha256 hex hash")

    storage = _mapping(candidate["storage"], label="storage", fields={"kind", "subject", "uri"})
    if storage["subject"] != reference:
        _error("storage subject must exactly equal image reference")
    storage_kind = _string(storage["kind"], label="storage.kind")
    storage_uri = _string(storage["uri"], label="storage.uri")

    if kind == "runtime":
        expected_repository = "ghcr.io/artexis10/exomem"
        if repository != expected_repository:
            _error("runtime image.repository is not approved")
        tag_match = _TAG_REF.fullmatch(checkout_ref)
        if tag_match is None:
            _error("runtime source.checkoutRef must be an exact release tag")
        release = _mapping(candidate["release"], label="release", fields={"tag", "version"})
        tag = _string(release["tag"], label="release.tag")
        version = _string(release["version"], label="release.version")
        if not _SEMVER.fullmatch(version) or tag != f"v{version}" or tag_match.group(1) != tag:
            _error("runtime release must match source checkout tag")
        if discovery_tag != f"{repository}:{source_commit}-hosted":
            _error("runtime image.discoveryTag must match source commit")
        if oidc_commit != source_commit:
            _error("runtime OIDC source commit must equal checkout commit")
        if event == "push" and oidc_ref != "refs/heads/main":
            _error("runtime push source ref must be refs/heads/main")
        if event == "workflow_dispatch" and oidc_ref != checkout_ref:
            _error("runtime manual source ref must equal checkout tag")
        if signer_workflow != RUNTIME_SIGNER_WORKFLOW:
            _error("runtime signer workflow is not approved")
        release_asset_pattern = re.compile(
            rf"https://github\.com/{re.escape(SOURCE_REPOSITORY)}/releases/download/"
            rf"{re.escape(tag)}/[A-Za-z0-9][A-Za-z0-9._-]*"
        )
        if storage_kind != "github-release" or not release_asset_pattern.fullmatch(storage_uri):
            _error("runtime storage is not approved")
        if candidate["schemaVersion"] == RECORDS_COMPATIBILITY_SCHEMA_VERSION:
            validate_records_compatibility_claim(candidate)
        elif "recordsCompatibility" in candidate:
            _error("legacy runtime candidate cannot carry Records compatibility")
    else:
        expected_repository = "ghcr.io/artexis10/exomem-provisioner"
        if repository != expected_repository:
            _error("provisioner image.repository is not approved")
        if candidate["release"] is not None:
            _error("provisioner release must be null")
        if checkout_ref != "refs/heads/main" or oidc_ref != "refs/heads/main":
            _error("provisioner source ref must be refs/heads/main")
        if oidc_commit != source_commit:
            _error("provisioner OIDC source commit must equal checkout commit")
        if discovery_tag != f"{repository}:{source_commit}":
            _error("provisioner image.discoveryTag must match source commit")
        if signer_workflow != PROVISIONER_SIGNER_WORKFLOW:
            _error("provisioner signer workflow is not approved")
        if (
            storage_kind != "oci-referrer" or storage_uri != f"oci://{repository}@{digest}"
        ):
            _error("provisioner storage is not approved")
        if candidate["schemaVersion"] != SCHEMA_VERSION:
            _error("Records compatibility candidates must be runtime candidates")
    return candidate


def _bundle_hash(path: Path, *, label: str = "bundle") -> str:
    return hashlib.sha256(_read_regular(path, label=label, maximum=MAX_BUNDLE_BYTES)).hexdigest()


def _write_atomic(path: Path, data: bytes) -> None:
    if path.exists() or path.is_symlink():
        try:
            existing = path.lstat()
        except OSError as exc:
            _error(f"cannot inspect candidate output: {exc}")
        if stat.S_ISLNK(existing.st_mode):
            _error("candidate output must not be a symlink")
        if not stat.S_ISREG(existing.st_mode):
            _error("candidate output must be a regular file")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.chmod(temporary, 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        if temporary.exists():
            temporary.unlink()


def record_candidate(
    record: dict[str, object], bundle: Path, output: Path, *, bundle_sha256: str | None = None
) -> dict[str, object]:
    expected_hash = _bundle_hash(bundle)
    if bundle_sha256 is not None and bundle_sha256 != expected_hash:
        _error("bundle hash does not match supplied hash")
    attestation = record.get("attestation")
    if not isinstance(attestation, dict):
        _error("attestation must be an object")
    recorded_hash = attestation.get("bundleSha256")
    if recorded_hash not in {"", expected_hash}:
        _error("bundle hash does not match candidate")
    attestation["bundleSha256"] = expected_hash
    candidate = validate_candidate(record)
    encoded = (json.dumps(candidate, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    _write_atomic(output, encoded)
    return candidate


def load_candidate(path: Path) -> dict[str, object]:
    return validate_candidate(_json_object(path, label="candidate", maximum=MAX_CANDIDATE_BYTES))


def _verified_statements(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list) or not value:
        _error("attestation verification output must contain verification results")
    statements: list[dict[str, object]] = []
    for entry in value:
        if not isinstance(entry, dict):
            _error("attestation verification result is malformed")
        verification_result = entry.get("verificationResult")
        if not isinstance(verification_result, dict):
            _error("attestation verification result is malformed")
        statement = verification_result.get("statement")
        if not isinstance(statement, dict):
            _error("attestation verification output statement is malformed")
        statements.append(cast(dict[str, object], statement))
    return statements


def _require_verified_subject(
    statement: dict[str, object], name: str, digest: str, *, label: str
) -> None:
    subjects = statement.get("subject")
    if not isinstance(subjects, list) or len(subjects) != 1 or not isinstance(subjects[0], dict):
        _error("verified statement must contain exactly one subject")
    subject = cast(dict[str, object], subjects[0])
    expected_digest = digest.removeprefix("sha256:")
    if set(subject) != {"name", "digest"} or subject["name"] != name:
        _error(f"verified statement subject does not equal {label}")
    subject_digest = subject["digest"]
    if not isinstance(subject_digest, dict) or subject_digest != {"sha256": expected_digest}:
        _error(f"verified statement subject digest does not equal {label}")


def verify_candidate(
    candidate_path: Path,
    *,
    bundle: Path | None = None,
    bundle_from_oci: bool = False,
    candidate_bundle: Path | None = None,
    gh_binary: str = "gh",
) -> None:
    if (bundle is None and not bundle_from_oci) or (bundle is not None and bundle_from_oci):
        _error("supply exactly one of bundle or bundle_from_oci")
    if candidate_bundle is None:
        _error("candidate bundle is required")
    candidate = load_candidate(candidate_path)
    candidate_sha256 = hashlib.sha256(
        _read_regular(candidate_path, label="candidate", maximum=MAX_CANDIDATE_BYTES)
    ).hexdigest()
    _bundle_hash(candidate_bundle, label="candidate bundle")
    image = cast(dict[str, object], candidate["image"])
    source = cast(dict[str, object], candidate["source"])
    workflow = cast(dict[str, object], candidate["workflow"])
    attestation = cast(dict[str, object], candidate["attestation"])
    if bundle is not None and _bundle_hash(bundle) != attestation["bundleSha256"]:
        _error("bundle hash does not match candidate")
    command_prefix = [
        gh_binary,
        "attestation",
        "verify",
        "--repo",
        cast(str, source["repository"]),
        "--signer-workflow",
        cast(str, workflow["signerWorkflow"]),
        "--signer-digest",
        cast(str, workflow["signerWorkflowDigest"]),
        "--source-digest",
        cast(str, workflow["oidcSourceCommit"]),
        "--source-ref",
        cast(str, workflow["oidcSourceRef"]),
        "--predicate-type",
        cast(str, attestation["predicateType"]),
        "--deny-self-hosted-runners",
    ]
    if bundle is not None:
        image_evidence = ["--bundle", os.fspath(bundle)]
    else:
        image_evidence = ["--bundle-from-oci"]
    verifications = (
        (
            f"oci://{image['reference']}",
            image_evidence,
            cast(str, image["repository"]),
            cast(str, image["digest"]),
            "candidate image",
        ),
        (
            os.fspath(candidate_path),
            ["--bundle", os.fspath(candidate_bundle)],
            candidate_path.name,
            candidate_sha256,
            "candidate file",
        ),
    )
    for target, evidence, expected_name, expected_digest, label in verifications:
        command = command_prefix[:3] + [target] + command_prefix[3:] + evidence + ["--format", "json"]
        try:
            result = subprocess.run(
                command,
                shell=False,
                check=False,
                capture_output=True,
                text=True,
                timeout=VERIFY_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            _error(f"unable to complete gh attestation verification: {exc}")
        if result.returncode != 0:
            _error(f"gh attestation verify failed: {result.stderr.strip()}")
        try:
            verified = json.loads(result.stdout, object_pairs_hook=_reject_duplicate_keys)
        except json.JSONDecodeError as exc:
            _error(f"invalid gh attestation verify JSON: {exc}")
        for statement in _verified_statements(verified):
            _require_verified_subject(statement, expected_name, expected_digest, label=label)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    record = commands.add_parser("record", help="write one validated candidate")
    record.add_argument("--component", choices=("runtime", "provisioner"), required=True)
    record.add_argument("--source-repository", required=True)
    record.add_argument("--source-ref", required=True)
    record.add_argument("--source-commit", required=True)
    record.add_argument("--release")
    record.add_argument("--image", required=True)
    record.add_argument("--discovery-tag", required=True)
    record.add_argument("--producer-repository", required=True)
    record.add_argument("--producer-workflow", required=True)
    record.add_argument("--producer-workflow-commit", required=True)
    record.add_argument("--producer-oidc-source-ref", required=True)
    record.add_argument("--producer-oidc-source-commit", required=True)
    record.add_argument("--producer-event", choices=("push", "workflow_dispatch"), required=True)
    record.add_argument("--run-id", required=True)
    record.add_argument("--run-attempt", required=True)
    record.add_argument("--bundle", type=Path, required=True)
    record.add_argument("--storage-kind", required=True)
    record.add_argument("--storage-uri", required=True)
    record.add_argument("--records-profile")
    record.add_argument("--records-reader-version", type=int)
    record.add_argument("--lifecycle-actions-enabled", choices=("true", "false"))
    record.add_argument("--records-issued-at")
    record.add_argument("--records-expires-at")
    record.add_argument("--records-runtime-target-json")
    record.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify", help="verify one recorded candidate")
    verify.add_argument("--candidate", type=Path, required=True)
    verify.add_argument("--candidate-bundle", type=Path, required=True)
    evidence = verify.add_mutually_exclusive_group(required=True)
    evidence.add_argument("--bundle", type=Path)
    evidence.add_argument("--bundle-from-oci", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "record":
            record_candidate(_candidate_from_flags(args), args.bundle, args.output)
        else:
            verify_candidate(
                args.candidate,
                bundle=args.bundle,
                bundle_from_oci=args.bundle_from_oci,
                candidate_bundle=args.candidate_bundle,
            )
    except CandidateError as exc:
        print(f"hosted image candidate: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
