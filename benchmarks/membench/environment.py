"""Environment capture, comparison and verification for run manifests.

A run directory must contain enough to establish *whether it can be compared
to another run*. It did not: ``environment.json`` recorded the interpreter
version from the first run onward and nothing ever compared it, so an
interpreter change (3.12.3 → 3.14.6) that took retrieval from 452 hits to 0
was published as 236 queries of zeros and investigated as a product
regression (findings doc, 2026-08-05 addenda; task 4b.24).

So capture is now the *whole* environment — every installed distribution, not
a summary — and there is a comparison step with exactly two tiers:

**Blocking** — a difference that invalidates a comparison. The interpreter
(``python_version``, ``python_implementation``), product identity
(``exomem_version``, the product repo's head, and *any* dirty tree), the
captured ``EXOMEM_*`` knobs, and the version of any distribution inside the
product's runtime closure.

**Reported** — recorded and surfaced, never invalidating. Every other
installed distribution (test and tooling packages the measured path cannot
import), the interpreter *build* string, ``platform``/``machine``, and
``generator_version``.

The line between them is one principle, not a taste:

    Block only where nothing else in the artifacts can independently
    establish that the difference did not matter.

Corpus identity is independently established by ``corpus-manifest.json``'s
dual hashes — which is exactly why task 4b.7 dispositioned renderer/generator
version strings as *reported* there: a Pillow bump was measured leaving all
200 artifact hashes identical on both axes, so the version string is
redundant evidence. Retrieval behaviour has no such independent check: no
artifact in a run directory can show that an interpreter or a runtime
dependency moving left the measured path alone. That asymmetry, not the
identity of the package, is what puts one in each tier.

Two consequences worth stating, because getting this boundary wrong in either
direction is the failure mode:

- Blocking is *scoped to a claim of reproduction*. A run that declares no
  reference environment is never invalidated by this module; it records its
  environment and says so. Invalidation applies only when a run was asked to
  reproduce a reference, where "the interpreter moved" is not a nuisance, it
  is the answer.
- Absence of data is never reported as agreement. A reference that predates
  full capture yields ``unverifiable`` entries and a status that is *not*
  ``match`` — the same rule as unsupported-never-zero, applied to
  provenance.
"""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path

from membench import GENERATOR_VERSION

_ENV_PREFIXES = ("EXOMEM_",)

#: Distribution(s) the measured path is rooted at. Every installed
#: distribution reachable from these through declared requirements is
#: "runtime closure": a package the product can import, therefore a package
#: whose version can change what the benchmark measured.
DEFAULT_CLOSURE_ROOTS: tuple[str, ...] = ("exomem",)

#: Tier labels. Blocking invalidates a comparison; reported never does.
BLOCKING = "blocking"
REPORTED = "reported"

_STATUS_MATCH = "match"
_STATUS_DIFFERENCES = "reported_differences"
_STATUS_BLOCKED = "blocking_mismatch"

_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[([^\]]*)\])?")
_REQUIREMENT_EXTRA_MARKER = re.compile(r"""extra\s*==\s*['"]([A-Za-z0-9._-]+)['"]""")


def _git(repo: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


def repo_state(repo: Path) -> dict[str, object] | None:
    repo = Path(repo)
    if not (repo / ".git").exists():
        return None
    head = _git(repo, "rev-parse", "HEAD")
    if head is None:
        return None
    status = _git(repo, "status", "--porcelain") or ""
    return {"path": str(repo), "head": head, "dirty": bool(status.strip())}


def normalize_distribution_name(name: str) -> str:
    """PEP 503 normalisation, so ``rank_bm25`` and ``rank-bm25`` are one key."""

    return re.sub(r"[-_.]+", "-", str(name)).strip().lower()


@dataclass(frozen=True)
class _Requirement:
    """One ``Requires-Dist`` line, reduced to what closure walking needs."""

    name: str
    #: Extras requested OF the dependency, e.g. ``pyjwt[crypto]`` → {"crypto"}.
    requested_extras: frozenset[str]
    #: The extra of the DECLARING distribution that gates this line, e.g.
    #: ``pytest; extra == "dev"`` → "dev". ``None`` means a base requirement.
    gated_by: str | None


def _parse_requirement(spec: str) -> _Requirement | None:
    text = str(spec)
    match = _REQUIREMENT_NAME.match(text)
    if not match:
        return None
    requested = frozenset(
        normalize_distribution_name(part)
        for part in (match.group(2) or "").split(",")
        if part.strip()
    )
    gate = _REQUIREMENT_EXTRA_MARKER.search(text)
    return _Requirement(
        name=normalize_distribution_name(match.group(1)),
        requested_extras=requested,
        gated_by=normalize_distribution_name(gate.group(1)) if gate else None,
    )


def installed_distributions() -> dict[str, str]:
    """Every installed distribution: normalised name → version, sorted.

    First-wins on duplicate names: that is the copy an ``import`` resolves
    to, and ``sys.path`` order is what makes the capture deterministic.
    Broken metadata is skipped rather than raised — capture must never be the
    thing that fails a run.
    """

    found: dict[str, str] = {}
    for dist in metadata.distributions():
        try:
            raw_name = dist.metadata["Name"]
            version = str(dist.version)
        except Exception:  # noqa: BLE001 - unreadable metadata is not a run fault
            continue
        if not raw_name:
            continue
        name = normalize_distribution_name(raw_name)
        if name not in found:
            found[name] = version
    return dict(sorted(found.items()))


def runtime_closure(
    roots: Iterable[str] = DEFAULT_CLOSURE_ROOTS,
    installed: Mapping[str, str] | None = None,
) -> list[str]:
    """Installed distributions reachable from ``roots`` by declared requires.

    Which edges are followed decides how wide "blocking" is, so the rule is
    narrow on purpose:

    - **base requirements** are always followed — that is what a package needs
      in order to function, hence what the product can import;
    - **extras-gated requirements are followed only when that extra was asked
      for** by whoever depends on it (``pyjwt[crypto]`` follows pyjwt's
      ``crypto`` extra, and nothing else of pyjwt's);
    - **at the roots**, any installed extras-gated dependency is followed —
      the product's own extras describe how the product is configured here,
      and a package sitting in the venv can be imported by it.

    Following every extra everywhere collapses the closure into "the whole
    venv" — measured: ``pyjwt``'s ``dev`` extra alone drags in pytest, and
    with it 81 of 81 installed distributions. That would make an unrelated
    pytest patch bump invalidate real runs, which is the failure mode that
    gets a guard switched off rather than fixed.

    Recorded in the artifact rather than recomputed at comparison time, so a
    run states for itself which distributions it considered load-bearing.
    """

    resolved = dict(installed) if installed is not None else installed_distributions()
    seen: set[str] = set()
    frontier: list[tuple[str, frozenset[str], bool]] = [
        (normalize_distribution_name(root), frozenset(), True) for root in roots
    ]
    while frontier:
        name, requested_extras, is_root = frontier.pop()
        if name in seen or name not in resolved:
            continue
        seen.add(name)
        try:
            requires = metadata.requires(name) or []
        except Exception:  # noqa: BLE001 - missing/odd metadata ends this branch
            requires = []
        for spec in requires:
            requirement = _parse_requirement(spec)
            if requirement is None or requirement.name not in resolved:
                continue
            if requirement.gated_by is not None and not (
                is_root or requirement.gated_by in requested_extras
            ):
                continue
            if requirement.name not in seen:
                frontier.append((requirement.name, requirement.requested_extras, False))
    return sorted(seen)


def capture_environment(
    *,
    extra_repos: dict[str, Path] | None = None,
    closure_roots: Iterable[str] = DEFAULT_CLOSURE_ROOTS,
) -> dict[str, object]:
    """The full environment a run was measured in, JSON-serialisable and sorted."""

    bench_root = Path(__file__).resolve().parents[1]
    repo_root = bench_root.parent
    env_knobs = {
        key: value
        for key, value in sorted(os.environ.items())
        if key.startswith(_ENV_PREFIXES)
    }
    repos: dict[str, object] = {"exomem": repo_state(repo_root)}
    for label, path in (extra_repos or {}).items():
        repos[label] = repo_state(path)
    try:
        import exomem

        exomem_version = getattr(exomem, "__version__", "unknown")
    except Exception:
        exomem_version = "unavailable"
    distributions = installed_distributions()
    return {
        "generator_version": GENERATOR_VERSION,
        "python": sys.version,
        # The version PROPER, kept apart from the build string above: the
        # build string churns (compiler, build date) and the version is what
        # a replication kit pins.
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "exomem_version": exomem_version,
        "repos": repos,
        "env_knobs": env_knobs,
        # The whole installed set, not a summary of it. This is the field
        # whose absence made the 2026-08-05 incident undiagnosable from the
        # artifacts alone.
        "distributions": distributions,
        "runtime_closure": runtime_closure(closure_roots, distributions),
    }


@dataclass(frozen=True)
class EnvironmentDifference:
    """One field that differs, or one check that could not be made."""

    field: str
    tier: str
    reference: str | None
    observed: str | None
    detail: str
    #: True when the difference is "one side did not record this", i.e. the
    #: check is unmeasurable rather than failed. Always ``REPORTED`` tier, and
    #: always enough to keep the overall status away from ``match``.
    unverifiable: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "field": self.field,
            "tier": self.tier,
            "reference": self.reference,
            "observed": self.observed,
            "detail": self.detail,
            "unverifiable": self.unverifiable,
        }

    def render(self) -> str:
        if self.unverifiable:
            return f"{self.field}: {self.detail}"
        return f"{self.field}: {self.reference!r} → {self.observed!r}"


@dataclass(frozen=True)
class EnvironmentComparison:
    """The verdict of comparing one run's environment to a reference."""

    status: str
    differences: tuple[EnvironmentDifference, ...] = field(default_factory=tuple)

    @property
    def blocking(self) -> tuple[EnvironmentDifference, ...]:
        return tuple(d for d in self.differences if d.tier == BLOCKING)

    @property
    def reported(self) -> tuple[EnvironmentDifference, ...]:
        return tuple(d for d in self.differences if d.tier == REPORTED)

    @property
    def unverifiable(self) -> tuple[EnvironmentDifference, ...]:
        return tuple(d for d in self.differences if d.unverifiable)

    @property
    def blocked(self) -> bool:
        return self.status == _STATUS_BLOCKED

    def summary(self) -> str:
        if self.blocked:
            rendered = "; ".join(d.render() for d in self.blocking)
            return (
                f"blocking environment mismatch ({len(self.blocking)}): {rendered}"
            )
        if self.status == _STATUS_MATCH:
            return "environment matches the reference on every recorded field"
        return (
            f"environment differs from the reference in {len(self.reported)} "
            f"reported field(s), {len(self.unverifiable)} of them unverifiable; "
            "no blocking difference"
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "summary": self.summary(),
            "blocking": [d.as_dict() for d in self.blocking],
            "reported": [d.as_dict() for d in self.reported],
        }


def _as_text(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _python_version(env: Mapping[str, object]) -> str | None:
    """The interpreter version proper, derived for artifacts that predate it."""

    value = env.get("python_version")
    if isinstance(value, str) and value.strip():
        return value.strip()
    raw = env.get("python")
    if isinstance(raw, str) and raw.strip():
        # sys.version starts with "3.12.3 (main, ...)".
        return raw.split()[0]
    return None


def _unverifiable(field_name: str, detail: str) -> EnvironmentDifference:
    return EnvironmentDifference(
        field=field_name,
        tier=REPORTED,
        reference=None,
        observed=None,
        detail=detail,
        unverifiable=True,
    )


def _compare_scalar(
    field_name: str,
    reference: object,
    observed: object,
    *,
    tier: str,
    detail: str | None = None,
) -> EnvironmentDifference | None:
    ref_text, obs_text = _as_text(reference), _as_text(observed)
    if ref_text is None or obs_text is None:
        if ref_text is None and obs_text is None:
            return None
        return _unverifiable(
            field_name,
            f"recorded by only one side (reference={ref_text!r}, observed={obs_text!r})",
        )
    if ref_text == obs_text:
        return None
    return EnvironmentDifference(
        field=field_name,
        tier=tier,
        reference=ref_text,
        observed=obs_text,
        detail=detail or f"{field_name} differs",
    )


def _compare_repos(
    reference: Mapping[str, object], observed: Mapping[str, object]
) -> list[EnvironmentDifference]:
    ref_repos = reference.get("repos") if isinstance(reference.get("repos"), dict) else {}
    obs_repos = observed.get("repos") if isinstance(observed.get("repos"), dict) else {}
    differences: list[EnvironmentDifference] = []
    for label in sorted(set(ref_repos) | set(obs_repos)):
        ref_state = ref_repos.get(label)
        obs_state = obs_repos.get(label)
        if not isinstance(ref_state, dict) and not isinstance(obs_state, dict):
            differences.append(
                _unverifiable(
                    f"repos.{label}.head",
                    "neither run recorded a git checkout for this repo; product "
                    "identity rests on exomem_version alone",
                )
            )
            continue
        if not isinstance(ref_state, dict) or not isinstance(obs_state, dict):
            differences.append(
                EnvironmentDifference(
                    field=f"repos.{label}.head",
                    tier=BLOCKING,
                    reference=_as_text((ref_state or {}).get("head"))
                    if isinstance(ref_state, dict)
                    else None,
                    observed=_as_text((obs_state or {}).get("head"))
                    if isinstance(obs_state, dict)
                    else None,
                    detail=(
                        "only one side recorded this repo: the product cannot be "
                        "shown to be the same source"
                    ),
                )
            )
            continue
        head = _compare_scalar(
            f"repos.{label}.head",
            ref_state.get("head"),
            obs_state.get("head"),
            tier=BLOCKING,
            detail=(
                "different source revision; equality of the measured code is "
                "not established by the artifacts"
            ),
        )
        if head is not None:
            differences.append(head)
        # A dirty tree on EITHER side is blocking even when both are dirty:
        # the head no longer identifies the source that ran.
        if bool(ref_state.get("dirty")) or bool(obs_state.get("dirty")):
            differences.append(
                EnvironmentDifference(
                    field=f"repos.{label}.dirty",
                    tier=BLOCKING,
                    reference=_as_text(bool(ref_state.get("dirty"))),
                    observed=_as_text(bool(obs_state.get("dirty"))),
                    detail=(
                        "uncommitted changes: the recorded head does not identify "
                        "the source that ran"
                    ),
                )
            )
    return differences


def _compare_knobs(
    reference: Mapping[str, object], observed: Mapping[str, object]
) -> list[EnvironmentDifference]:
    ref = reference.get("env_knobs") if isinstance(reference.get("env_knobs"), dict) else {}
    obs = observed.get("env_knobs") if isinstance(observed.get("env_knobs"), dict) else {}
    differences: list[EnvironmentDifference] = []
    for key in sorted(set(ref) | set(obs)):
        if ref.get(key) == obs.get(key):
            continue
        differences.append(
            EnvironmentDifference(
                field=f"env_knobs.{key}",
                tier=BLOCKING,
                reference=_as_text(ref.get(key)),
                observed=_as_text(obs.get(key)),
                detail=(
                    "captured knobs are captured because they change product "
                    "behaviour; a difference is not decoration"
                ),
            )
        )
    return differences


def _closure_names(*envs: Mapping[str, object]) -> set[str]:
    names: set[str] = set()
    for env in envs:
        recorded = env.get("runtime_closure")
        if isinstance(recorded, list):
            names.update(normalize_distribution_name(name) for name in recorded)
    return names


def _compare_distributions(
    reference: Mapping[str, object], observed: Mapping[str, object]
) -> list[EnvironmentDifference]:
    ref = reference.get("distributions")
    obs = observed.get("distributions")
    if not isinstance(ref, dict) or not isinstance(obs, dict):
        missing = []
        if not isinstance(ref, dict):
            missing.append("reference")
        if not isinstance(obs, dict):
            missing.append("observed")
        return [
            _unverifiable(
                "distributions",
                f"not recorded by {' and '.join(missing)}; the installed set "
                "cannot be compared (this run predates full environment capture)",
            )
        ]
    closure = _closure_names(reference, observed)
    differences: list[EnvironmentDifference] = []
    if not closure:
        differences.append(
            _unverifiable(
                "runtime_closure",
                "neither side recorded a runtime closure; every distribution "
                "difference below is reported, none blocking",
            )
        )
    for name in sorted(set(ref) | set(obs)):
        ref_version = _as_text(ref.get(name))
        obs_version = _as_text(obs.get(name))
        if ref_version == obs_version:
            continue
        in_closure = normalize_distribution_name(name) in closure
        if ref_version is None:
            detail = "installed only in the observed environment"
        elif obs_version is None:
            detail = "installed only in the reference environment"
        else:
            detail = "version differs"
        if in_closure:
            detail = f"{detail}; inside the product's runtime closure"
        else:
            detail = f"{detail}; outside the product's runtime closure"
        differences.append(
            EnvironmentDifference(
                field=f"distributions.{name}",
                tier=BLOCKING if in_closure else REPORTED,
                reference=ref_version,
                observed=obs_version,
                detail=detail,
            )
        )
    return differences


def compare_environments(
    reference: Mapping[str, object], observed: Mapping[str, object]
) -> EnvironmentComparison:
    """Compare two captured environments and tier every difference."""

    differences: list[EnvironmentDifference] = []

    ref_python, obs_python = _python_version(reference), _python_version(observed)
    interpreter = _compare_scalar(
        "python_version",
        ref_python,
        obs_python,
        tier=BLOCKING,
        detail=(
            "the interpreter changed: it re-resolves every dependency and "
            "nothing in a run's artifacts can show the measured path survived "
            "it (this exact difference, 3.12.3 → 3.14.6, took retrieval from "
            "452 hits to 0)"
        ),
    )
    if interpreter is not None:
        differences.append(interpreter)
    implementation = _compare_scalar(
        "python_implementation",
        reference.get("python_implementation"),
        observed.get("python_implementation"),
        tier=BLOCKING,
    )
    if implementation is not None:
        differences.append(implementation)
    version_of_record = _compare_scalar(
        "exomem_version",
        reference.get("exomem_version"),
        observed.get("exomem_version"),
        tier=BLOCKING,
    )
    if version_of_record is not None:
        differences.append(version_of_record)

    differences.extend(_compare_repos(reference, observed))
    differences.extend(_compare_knobs(reference, observed))
    differences.extend(_compare_distributions(reference, observed))

    # Reported provenance. The interpreter BUILD string is only worth a line
    # when the version proper matched — otherwise it repeats the blocking
    # entry above in a noisier form.
    if interpreter is None or interpreter.unverifiable:
        build_string = _compare_scalar(
            "python", reference.get("python"), observed.get("python"), tier=REPORTED
        )
        if build_string is not None:
            differences.append(build_string)
    for field_name in ("platform", "machine", "generator_version"):
        # platform embeds the kernel patch level; blocking on it would
        # manufacture invalid runs from an OS update that cannot reach the
        # measured path.
        entry = _compare_scalar(
            field_name, reference.get(field_name), observed.get(field_name), tier=REPORTED
        )
        if entry is not None:
            differences.append(entry)

    ordered = tuple(
        sorted(differences, key=lambda d: (d.tier != BLOCKING, d.field))
    )
    if any(d.tier == BLOCKING for d in ordered):
        status = _STATUS_BLOCKED
    elif ordered:
        status = _STATUS_DIFFERENCES
    else:
        status = _STATUS_MATCH
    return EnvironmentComparison(status=status, differences=ordered)


def load_environment(source: Mapping[str, object] | Path | str) -> dict[str, object]:
    """Load a captured environment from a mapping, a file, or a run directory."""

    if isinstance(source, Mapping):
        return dict(source)
    path = Path(source)
    if path.is_dir():
        path = path / "environment.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} does not contain an environment object")
    return payload


def verify_run_environment(
    run_dir: Path | str, reference: Mapping[str, object] | Path | str
) -> EnvironmentComparison:
    """Compare a completed run's recorded environment against a reference."""

    return compare_environments(load_environment(reference), load_environment(run_dir))
