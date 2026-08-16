"""The f26 track-D journey: a hookless episode, driven through the real envelope.

f26 exists because f20-f25 test detectors and end states, so a runtime could
satisfy every one of them while no signal ever reached a client. This driver
runs the episode on the thinnest surface available — no retrieval hook, compact
response detail — and captures the responses it *actually received*, because the
family's claim is about delivery and a claim about delivery cannot be settled
from stored state.

**The envelope is discovered, never assumed.** :func:`discover_envelope` locates
the installed CLI and asks it what it is before a single journey step runs. If
no envelope is installed, the journey refuses with
:class:`EnvelopeNotDiscovered` rather than falling back to an in-process import:
a fallback would quietly turn a carrier test into a library test, which is the
one substitution this family cannot survive.

Nothing here reads a clock. ``captured_at`` is supplied by the caller so a
journey artifact is reproducible from its inputs.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from ..corpora.no_nudge import ABSENCE_SURFACES
from ..snapshot import (
    DECLARABLE_FIELDS,
    EpistemicStateSnapshot,
    FieldDeclaration,
    ProjectorMeta,
    StateItem,
)

#: The response detail the family is about. A due-state block only reachable at
#: a richer level has not been delivered to a thin client.
COMPACT_DETAIL = "compact"

#: Candidate executable names, in preference order.
ENVELOPE_CANDIDATES: tuple[str, ...] = ("exomem",)

JOURNEY_PROJECTOR = ProjectorMeta(
    name="f26-carrier-journey",
    version="1.0.0",
    author="benchmark-harness",
    endpoints_used=("cli:documented compact envelope",),
    loc=0,
    loc_code=0,
)


class EnvelopeNotDiscovered(RuntimeError):
    """No installed CLI envelope; the carrier journey cannot run."""


class JourneyStepFailed(RuntimeError):
    """A documented journey step did not complete."""


@dataclass(frozen=True)
class Envelope:
    """The installed CLI, as discovered rather than as assumed."""

    executable: Path
    version: str

    def run(self, args: Sequence[str], *, cwd: Path, timeout: float = 60.0) -> str:
        completed = subprocess.run(  # noqa: S603 - resolved executable, fixed argv
            [str(self.executable), *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            env={
                **os.environ,
                "EXOMEM_DISABLE_EMBEDDINGS": "1",
                # The envelope locates the vault from the environment, not from
                # the working directory. Passing only ``cwd`` produced a
                # well-formed refusal that looked like a product failure and was
                # in fact a harness one.
                "EXOMEM_VAULT_PATH": str(cwd),
            },
        )
        if completed.returncode != 0:
            # The envelope reports operational errors as a JSON envelope on
            # stdout and leaves stderr empty, so a stderr-only message said
            # nothing at all about why a step failed.
            detail = (completed.stderr.strip() or completed.stdout.strip())[:400]
            raise JourneyStepFailed(
                f"{args[0]} exited {completed.returncode}: {detail}"
            )
        return completed.stdout


def discover_envelope() -> Envelope:
    """Locate the installed CLI and ask it what it is. Never assume either."""

    for name in ENVELOPE_CANDIDATES:
        found = shutil.which(name)
        if found is None:
            continue
        executable = Path(found).resolve()
        try:
            completed = subprocess.run(  # noqa: S603 - resolved executable, fixed argv
                [str(executable), "--version"],
                capture_output=True,
                text=True,
                timeout=30.0,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise EnvelopeNotDiscovered(f"{name} is present but not runnable: {error}") from error
        if completed.returncode != 0:
            raise EnvelopeNotDiscovered(
                f"{name} --version exited {completed.returncode}; envelope not identified"
            )
        return Envelope(executable=executable, version=completed.stdout.strip())
    raise EnvelopeNotDiscovered(
        "no installed CLI envelope found on PATH; f26 measures delivery through the "
        "real client surface and must not fall back to an in-process import"
    )


#: The flag each documented command spells its response detail with. They differ,
#: and the first version of this driver assumed they did not: it passed
#: ``--detail`` to both and to a ``recall`` command that does not exist, so every
#: step exited 2. A journey that cannot run is not evidence about delivery.
DETAIL_FLAGS: Mapping[str, str] = MappingProxyType(
    {"remember": "--response-detail", "bootstrap": "--profile"}
)

#: The episode's fixture content. Sourced here rather than improvised at the call
#: site so a journey artifact is reproducible from its inputs, and written to
#: satisfy the product's own semantic-authoring precommit — a step rejected for
#: malformed input would measure the fixture, not the carrier.
JOURNEY_TITLE = "f26 carrier probe conclusion"

#: The relation target the durable conclusion points at. The product refuses a
#: compiled page with no qualifying typed relation, which is a precondition of
#: the *write*, not of the carrier — so the journey satisfies it explicitly and
#: names the page it depends on rather than discovering the refusal at runtime.
#: This page exists in the vault the product itself ships as a sample, which is
#: what :func:`seed_journey_vault` copies a run's vault from.
JOURNEY_RELATION_TARGET = "Knowledge Base/Entities/Concepts/Local First Knowledge"

#: The vault a journey run starts from, relative to the repository root. Using
#: the product's own sample vault keeps the precondition visible: the carrier is
#: measured against a vault the product itself calls valid.
SAMPLE_VAULT_PATH = "src/exomem/_sample_vault"


def seed_journey_vault(destination: Path, *, repo_root: Path) -> Path:
    """Copy the product's sample vault to ``destination`` and return it.

    The journey writes, so it must not run against a checked-in fixture. The
    copy also makes a run reproducible: every step starts from the same declared
    state rather than from whatever the last run left behind.
    """

    shutil.copytree(repo_root / SAMPLE_VAULT_PATH, destination)
    return destination


def journey_content(*, relation_target: str = JOURNEY_RELATION_TARGET) -> str:
    """The durable conclusion the episode records, as the product will accept it."""

    return (
        "A durable conclusion recorded by the f26 carrier journey.\n"
        "\n"
        "## Observations\n"
        "\n"
        "- [benchmark] The hookless episode carrier records one durable conclusion "
        "and then reconstructs it in a fresh session #f26 (epistemic-bench) ^f26-carrier\n"
        "\n"
        "## Relations\n"
        "\n"
        f"- supports [[{relation_target}]]\n"
    )


JOURNEY_CONTENT = journey_content()


def journey_steps(
    *, title: str = JOURNEY_TITLE, content: str = JOURNEY_CONTENT
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """The episode as complete, executable argv.

    A conversational durable conclusion, an abrupt session end, then a fresh
    session that must reconstruct from the thinnest surface the product offers.
    Every required option the envelope declares is supplied, because the family's
    claim is about what a thin client *receives* and nothing is received from a
    command that refuses to run.
    """

    return (
        (
            "mutation",
            (
                "remember",
                "--json",
                DETAIL_FLAGS["remember"],
                COMPACT_DETAIL,
                "--title",
                title,
                "--content",
                content,
            ),
        ),
        (
            "reconstruction",
            ("bootstrap", "--json", DETAIL_FLAGS["bootstrap"], COMPACT_DETAIL),
        ),
    )


JOURNEY_STEPS: tuple[tuple[str, tuple[str, ...]], ...] = journey_steps()

#: ``surface -> the response keys that would carry it``. Absence of every one of
#: them is what makes a surface unobserved rather than empty.
SURFACE_RESPONSE_KEYS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "due_state_counters": ("due_state", "due_state_counters"),
        "review_queue": ("review_queue", "review"),
        "proposal_queue": ("proposal_queue", "proposals"),
        "audit_findings": ("audit_findings", "audit"),
    }
)


def required_options(command: str) -> frozenset[str]:
    """The options the installed envelope declares mandatory for ``command``.

    Parsed from the envelope's own usage line rather than restated here, so the
    journey cannot drift away from the CLI it claims to drive. Anything the
    usage line shows in brackets is optional by the CLI's own account; whatever
    survives bracket removal is required.
    """

    envelope = discover_envelope()
    completed = subprocess.run(  # noqa: S603 - resolved executable, fixed argv
        [str(envelope.executable), command, "--help"],
        capture_output=True,
        text=True,
        timeout=60.0,
    )
    if completed.returncode != 0:
        raise JourneyStepFailed(
            f"{command} --help exited {completed.returncode}; options cannot be read"
        )
    return _required_options_from_usage(completed.stdout)


def _required_options_from_usage(help_text: str) -> frozenset[str]:
    usage: list[str] = []
    for line in help_text.splitlines():
        if not usage:
            if not line.startswith("usage:"):
                continue
            usage.append(line)
            continue
        if not line.startswith(" "):
            break
        usage.append(line)
    if not usage:
        # Returning an empty set here would make every argv-completeness check
        # pass vacuously the day the help format changes, which is the failure
        # this function exists to catch.
        raise JourneyStepFailed(
            "the envelope's help output carries no usage line; required options "
            "cannot be read, so argv completeness cannot be checked"
        )
    flattened = " ".join(usage)
    depth = 0
    kept: list[str] = []
    for character in flattened:
        if character == "[":
            depth += 1
        elif character == "]":
            depth = max(0, depth - 1)
        elif depth == 0:
            kept.append(character)
    return frozenset(re.findall(r"--[A-Za-z][\w-]*", "".join(kept)))


def _envelope_bodies(payload: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    """The payload and, when present, the documented ``{success, data}`` body.

    The envelope nests everything one level down. Looking only at the top level
    would have reported every surface unavailable no matter what the product
    delivered — which would make f26 permanently red for a harness reason, and
    an assertion that cannot pass measures nothing.
    """

    data = payload.get("data")
    if isinstance(data, Mapping):
        return (payload, data)
    return (payload,)


def _carries_surface(payload: Mapping[str, object], surface: str) -> bool:
    """Whether one captured compact response carries a named surface."""

    return any(
        key in body
        for body in _envelope_bodies(payload)
        for key in SURFACE_RESPONSE_KEYS[surface]
    )


def _carries_due_state(payload: Mapping[str, object]) -> bool:
    """Whether one captured compact response carries the due-state block."""

    return _carries_surface(payload, "due_state_counters")


def capture_responses(
    envelope: Envelope, *, vault: Path, steps: Sequence[tuple[str, tuple[str, ...]]] = JOURNEY_STEPS
) -> dict[str, Mapping[str, object]]:
    """Run the journey and return the parsed compact responses it received."""

    captured: dict[str, Mapping[str, object]] = {}
    for name, args in steps:
        raw = envelope.run(args, cwd=vault)
        try:
            payload = json.loads(raw)
        except ValueError as error:
            raise JourneyStepFailed(f"{name} response is not JSON: {raw[:200]}") from error
        if not isinstance(payload, dict):
            raise JourneyStepFailed(f"{name} response is not an object")
        captured[name] = payload
    return captured


def journey_snapshot(
    captured: Mapping[str, Mapping[str, object]],
    *,
    taken_at: str,
    packet_members: Sequence[str] = (),
    declarations: Sequence[FieldDeclaration] | None = None,
) -> EpistemicStateSnapshot:
    """Project captured responses into the neutral snapshot f26 asserts over.

    Only what the responses actually contained becomes state, and that rule now
    covers the *metadata* as well as the items. An earlier version stamped every
    absence surface ``complete`` and declared every field observable no matter
    what came back, which manufactured exactly the silence the anti-vacuity
    meta-predicate exists to refuse — and contradicted the vault projector, which
    honestly reports three of these four surfaces as unprojectable. A surface
    nothing was heard about is ``unavailable``, a field nothing evidenced is
    undeclared, and the quiet assertions are ``blocked`` in consequence.
    """

    items: list[StateItem] = []
    for name, payload in sorted(captured.items()):
        raw = {"response_detail": COMPACT_DETAIL}
        if _carries_due_state(payload):
            raw["targets"] = "due_state_counters"
        items.append(
            StateItem(
                id=f"f26-response-{name}",
                kind="container",
                title=f"f26-response-{name}",
                raw=raw,
            )
        )
    if packet_members:
        items.append(
            StateItem(
                id="f26-packet",
                kind="container",
                title="f26-packet",
                current="yes",
                cites=tuple(packet_members),
                raw={"packet": "f26-packet"},
            )
        )
    observed = {
        surface
        for surface in SURFACE_RESPONSE_KEYS
        if any(_carries_surface(payload, surface) for payload in captured.values())
    }
    items.extend(
        StateItem(
            id=f"surface-{surface}",
            kind="container",
            title=surface,
            raw={
                "surface": surface,
                "projection": "complete" if surface in observed else "unavailable",
            },
        )
        for surface in ABSENCE_SURFACES
    )

    return EpistemicStateSnapshot(
        provider="exomem",
        variant="native",
        phase="f26",
        taken_at=taken_at,
        items=tuple(items),
        declarations=(
            tuple(declarations)
            if declarations
            else _derived_declarations(observed, packet_members=packet_members)
        ),
        projector=JOURNEY_PROJECTOR,
        completeness_notes=(
            "Captured compact responses from the discovered CLI envelope only; "
            "nothing is read from stored state, because the family measures delivery. "
            f"Surfaces evidenced by a response: {', '.join(sorted(observed)) or 'none'}."
        ),
    )


def _derived_declarations(
    observed: set[str], *, packet_members: Sequence[str]
) -> tuple[FieldDeclaration, ...]:
    """Declare only what a captured response evidenced. Default is unavailable."""

    evidenced = {
        surface: f"captured compact response carried {surface}" for surface in observed
    }
    if packet_members:
        evidenced["continuation_packet"] = (
            f"captured response carried a continuation packet of {len(packet_members)} member(s)"
        )
    return tuple(
        FieldDeclaration(
            field=field,
            status="declared" if field in evidenced else "unavailable",
            evidence=evidenced.get(
                field, "no captured compact response carried this field"
            ),
        )
        for field in DECLARABLE_FIELDS
    )


__all__ = [
    "COMPACT_DETAIL",
    "DETAIL_FLAGS",
    "Envelope",
    "EnvelopeNotDiscovered",
    "JOURNEY_CONTENT",
    "JOURNEY_RELATION_TARGET",
    "JOURNEY_STEPS",
    "JOURNEY_TITLE",
    "JourneyStepFailed",
    "SAMPLE_VAULT_PATH",
    "SURFACE_RESPONSE_KEYS",
    "capture_responses",
    "discover_envelope",
    "journey_content",
    "journey_snapshot",
    "journey_steps",
    "required_options",
    "seed_journey_vault",
]
