"""Run-manifest construction for the epistemic bench, families declared.

``protocol.manifest`` refuses a run whose declared families are withheld by a
pending amendment, but only a caller that actually names them can be refused.
This module is that caller: it derives the family set from the scenarios a run
will execute and passes it through, so the gate fires on the run's real
composition rather than on whatever the caller remembered to type.

Two gates, deliberately. :mod:`epistemic.schema` refuses to *load* a scenario
for a withheld family, which is the choke point nothing can get around — there
is no Scenario object to run. This one covers the case where a Scenario reaches
the protocol layer by some other route (constructed in code, or a loader change
later on), and it is what stamps the declaration into the run record so
``load_epistemic_manifest`` can refuse to replay it into a claim.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from protocol.manifest import load_manifest, start_manifest

if TYPE_CHECKING:  # pragma: no cover - typing only
    from protocol.models import RunManifest

    from .schema import Scenario


def declared_family_ids(scenarios: Iterable[Scenario]) -> tuple[str, ...]:
    """The distinct families a run executes, in first-seen order.

    Order is stable rather than sorted so a refusal names families in the order
    the run declared them, which is the order a reader of the scenario set will
    look for them in.
    """

    return tuple(dict.fromkeys(scenario.family_id for scenario in scenarios))


def start_epistemic_manifest(
    run_dir: Path | str,
    *,
    scenarios: Iterable[Scenario],
    **kwargs: Any,
) -> RunManifest:
    """Start a run manifest for ``scenarios``, declaring their families.

    Refuses before any artifact exists when a scenario's family is withheld by
    an unacknowledged amendment.
    """

    return start_manifest(
        run_dir, family_ids=declared_family_ids(scenarios), **kwargs
    )


def load_epistemic_manifest(
    run_dir: Path | str, *, scenarios: Iterable[Scenario]
) -> RunManifest:
    """Load a terminal manifest for a claim about ``scenarios``' families.

    A manifest recorded while an amendment was pending stays readable; what it
    may not do is back a claim about a family that amendment still withholds.
    """

    return load_manifest(run_dir, family_ids=declared_family_ids(scenarios))


__all__ = [
    "declared_family_ids",
    "load_epistemic_manifest",
    "start_epistemic_manifest",
]
