"""Closed registry over the suite directories under benchmarks/suites/.

Every directory here MUST carry a valid LOCKFILE.json: pinned upstream
identity, license provenance, and an honest `runability` claim. This is the
suite-level mirror of `lme/providers/registry.py`'s closed provider set — an
unregistered or malformed suite is refused outright, never silently
substituted or partially trusted.

The LOCKFILE-or-GAP invariant: a suite the programme cannot yet run still
gets an entry, with `runability: "gap"` and a non-empty `gap_reason` standing
in for the upstream pin it does not have. A `gap` entry may never also carry
a `commit_sha` — that would claim a pinned, verifiable checkout for a suite
declared unrunnable.

Validation helpers mirror `memorybench/setup.py`'s required-key and digest
checks, narrowed to what a suite LOCKFILE actually carries: a 40-hex git
commit SHA (`commit_sha`) and 64-hex sha256 file digests (`license_sha256`,
and lme_v1's nested `evaluate_qa.sha256`). A suite that declares an
`evaluate_qa` judge-script pin (currently only lme_v1) gets that block
deep-validated against a closed schema of its own, mirroring how
`memorybench/setup.py`:63-96 validates its nested provider-file records.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

SUITES_ROOT = Path(__file__).resolve().parent
LOCKFILE_NAME = "LOCKFILE.json"

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")

RUNABILITY_VALUES = frozenset({"runnable", "runnable-with-cost", "gap"})

#: Keys every LOCKFILE.json must carry regardless of runability.
_REQUIRED_KEYS = frozenset(
    {
        "suite",
        "paper",
        "repo_url",
        "upstream_last_commit",
        "license_spdx",
        "checkout_env_var",
        "runability",
        "verified_at_utc",
        "notes",
    }
)

#: Every required key must be a non-empty string except `runability`, which
#: is validated against RUNABILITY_VALUES separately (and type-checked first,
#: since testing membership of a non-hashable value in a frozenset raises
#: TypeError rather than failing closed as a SuiteRegistryError).
_REQUIRED_STRING_KEYS = _REQUIRED_KEYS - {"runability"}

#: Keys a LOCKFILE.json may carry in addition to the required set. Mirrors the
#: union of fields actually used across the pinned suites (stale, memops,
#: memoryagentbench, oida-corpora, lme_v1): `dataset` and `shape` describe the
#: data differently per suite, `license_sha256` is absent where no single
#: LICENSE file is hashed, `commit_sha`/`gap_reason` are mutually exclusive
#: (see `_validate_entry`), and `evaluate_qa` pins a suite's official judge
#: script interface (deep-validated by `_validate_evaluate_qa`).
_OPTIONAL_KEYS = frozenset(
    {"commit_sha", "license_sha256", "dataset", "shape", "gap_reason", "evaluate_qa"}
)

_KNOWN_KEYS = _REQUIRED_KEYS | _OPTIONAL_KEYS

#: Suites whose LOCKFILE.json must carry a deep-validated `evaluate_qa` block.
#: Today only lme_v1 declares an official judge script this package emits
#: commands for; a future runnable-with-cost suite that adds one belongs here
#: too — there is no separate "declares a judge" marker field to key off of.
_EVALUATE_QA_REQUIRED_SUITES = frozenset({"lme_v1"})

#: evaluate_qa's own closed key set. No `flags` key: the pinned script (see
#: LOCKFILE.json notes) takes exactly `arity` positional arguments, not
#: flags. No `log_derivation`: the script writes no log file at this pin,
#: whatever its upstream README claims.
_EVALUATE_QA_KEYS = frozenset(
    {
        "path",
        "sha256",
        "invocation",
        "arity",
        "argv_template",
        "result_file_derivation",
        "readme_example",
    }
)


class SuiteRegistryError(ValueError):
    """A suite directory or its LOCKFILE.json violates the closed registry."""


def _suite_directories(root: Path) -> tuple[Path, ...]:
    if not root.is_dir():
        return ()
    return tuple(
        sorted(
            path
            for path in root.iterdir()
            if path.is_dir() and not path.name.startswith(("_", "."))
        )
    )


def _load_lockfile(directory: Path) -> dict[str, Any]:
    lockfile = directory / LOCKFILE_NAME
    if not lockfile.is_file():
        raise SuiteRegistryError(f"suite directory has no {LOCKFILE_NAME}: {directory.name}")
    try:
        data = json.loads(lockfile.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SuiteRegistryError(f"{directory.name}: {LOCKFILE_NAME} is unreadable") from exc
    if not isinstance(data, dict):
        raise SuiteRegistryError(f"{directory.name}: {LOCKFILE_NAME} is not a JSON object")
    return data


def _safe_relative_posix_path(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and not value.startswith("/")
        and ".." not in Path(value).parts
    )


def _validate_evaluate_qa(name: str, evaluate_qa: object) -> None:
    if not isinstance(evaluate_qa, dict):
        raise SuiteRegistryError(f"{name}: evaluate_qa must be a JSON object")
    missing = _EVALUATE_QA_KEYS - set(evaluate_qa)
    if missing:
        raise SuiteRegistryError(f"{name}: evaluate_qa is missing {sorted(missing)}")
    unknown = set(evaluate_qa) - _EVALUATE_QA_KEYS
    if unknown:
        raise SuiteRegistryError(f"{name}: evaluate_qa has unknown keys {sorted(unknown)}")

    if evaluate_qa["invocation"] != "positional":
        raise SuiteRegistryError(f"{name}: evaluate_qa.invocation must be 'positional'")
    if evaluate_qa["arity"] != 3:
        raise SuiteRegistryError(f"{name}: evaluate_qa.arity must be 3")
    argv_template = evaluate_qa["argv_template"]
    if not isinstance(argv_template, list) or len(argv_template) != evaluate_qa["arity"]:
        raise SuiteRegistryError(
            f"{name}: evaluate_qa.argv_template must have exactly arity entries"
        )
    sha256 = evaluate_qa["sha256"]
    if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
        raise SuiteRegistryError(f"{name}: evaluate_qa.sha256 is not a valid digest")
    if not _safe_relative_posix_path(evaluate_qa["path"]):
        raise SuiteRegistryError(f"{name}: evaluate_qa.path is not a safe relative path")
    for key in ("result_file_derivation", "readme_example"):
        value = evaluate_qa[key]
        if not isinstance(value, str) or not value:
            raise SuiteRegistryError(f"{name}: evaluate_qa.{key} must be a non-empty string")


def _validate_entry(name: str, data: dict[str, Any]) -> None:
    missing = _REQUIRED_KEYS - set(data)
    if missing:
        raise SuiteRegistryError(f"{name}: {LOCKFILE_NAME} is missing {sorted(missing)}")
    unknown = set(data) - _KNOWN_KEYS
    if unknown:
        raise SuiteRegistryError(f"{name}: {LOCKFILE_NAME} has unknown keys {sorted(unknown)}")

    for key in _REQUIRED_STRING_KEYS:
        value = data[key]
        if not isinstance(value, str) or not value:
            raise SuiteRegistryError(f"{name}: {key} must be a non-empty string")

    if data["suite"] != name:
        raise SuiteRegistryError(
            f"{name}: {LOCKFILE_NAME} suite field {data['suite']!r} does not match its directory"
        )

    runability = data["runability"]
    if not isinstance(runability, str) or runability not in RUNABILITY_VALUES:
        raise SuiteRegistryError(f"{name}: unknown runability {runability!r}")

    if runability == "gap":
        if not data.get("gap_reason"):
            raise SuiteRegistryError(f"{name}: gap runability requires a non-empty gap_reason")
        if data.get("commit_sha"):
            raise SuiteRegistryError(
                f"{name}: gap runability forbids a commit_sha-based runnability claim"
            )
    else:
        if "gap_reason" in data:
            raise SuiteRegistryError(f"{name}: gap_reason is only valid when runability is gap")
        if not data.get("commit_sha"):
            raise SuiteRegistryError(f"{name}: {runability!r} runability requires commit_sha")

    commit_sha = data.get("commit_sha")
    if commit_sha is not None and not _COMMIT_RE.fullmatch(str(commit_sha)):
        raise SuiteRegistryError(f"{name}: commit_sha is not a valid digest")

    license_sha256 = data.get("license_sha256")
    if license_sha256 is not None and not _SHA256_RE.fullmatch(str(license_sha256)):
        raise SuiteRegistryError(f"{name}: license_sha256 is not a valid digest")

    if "evaluate_qa" in data:
        # Branch on key *presence*, not on the retrieved value: an explicit
        # `"evaluate_qa": null` must still reach the type check below and
        # fail as "must be a JSON object" for every suite, not only the
        # suites in _EVALUATE_QA_REQUIRED_SUITES.
        _validate_evaluate_qa(name, data["evaluate_qa"])
    elif name in _EVALUATE_QA_REQUIRED_SUITES:
        raise SuiteRegistryError(f"{name}: evaluate_qa is required for this suite")


def validate_all(root: Path | None = None) -> dict[str, dict[str, Any]]:
    """Validate every suite directory and return its parsed LOCKFILE.json.

    Raises :class:`SuiteRegistryError` naming the offending directory (and,
    for a string-key or nested `evaluate_qa` violation, the offending key)
    on the first violation: a missing lockfile, an unreadable or malformed
    one, a missing or empty required key, an unknown key, a malformed digest
    (`commit_sha`, `license_sha256`, or `evaluate_qa.sha256`), a
    non-hashable or unrecognized `runability`, `gap` without `gap_reason`,
    or a malformed `evaluate_qa` block.
    """

    entries: dict[str, dict[str, Any]] = {}
    for directory in _suite_directories(root or SUITES_ROOT):
        data = _load_lockfile(directory)
        _validate_entry(directory.name, data)
        entries[directory.name] = data
    return entries


def registered_suite_names(root: Path | None = None) -> tuple[str, ...]:
    """The closed set of suite names: the directories under the suites root."""

    return tuple(directory.name for directory in _suite_directories(root or SUITES_ROOT))


def suite_lockfile(name: str, root: Path | None = None) -> dict[str, Any]:
    """Return the validated LOCKFILE.json for `name`, or raise if unregistered."""

    resolved_root = root or SUITES_ROOT
    if name not in registered_suite_names(resolved_root):
        raise SuiteRegistryError(f"unknown suite {name!r}")
    data = _load_lockfile(resolved_root / name)
    _validate_entry(name, data)
    return data
