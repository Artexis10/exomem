"""Harness faults: what voids a unit of an outcome run, judged from a session's own evidence.

A product failure is a scored loss: a wrong answer, a refusal, a timeout, a malformed reply, a
missing deliverable, and an error result that the CLI returns (a spent turn or context budget
included). A harness fault is the harness failing to run the product at all, and only evidence of
one voids a unit. The evidence is:

- the CLI exited before it answered;
- the CLI never sent an ``init`` event, or its transcript is missing or empty;
- an ``init`` event shows a model other than the one requested (a fallback), an API key as the
  credential source, or an Exomem server that is not ``connected``.

The ``init`` event arrives on every user turn, so each one is checked, not only the first.
A harness fault is retried with the same inputs at most ``RETRIES`` times; a retry is not a repeat.
"""

from __future__ import annotations

from collections.abc import Callable, Collection

RETRIES = 2
#: f35 held-out: the one run that replaces a run voided by a harness fault. A second voided run voids the decision.
REPLACEMENT_RUN = 4
#: f36 and f37: more struck units than this voids the decision. f35 voids it at a second voided run.
STRIKE_LIMIT = 2
#: The credential source a subscription session reports. Any other value means the session used an API key.
SUBSCRIPTION_KEY_SOURCE = "none"


def init_faults(init: dict, *, model: str, exomem: bool) -> list[str]:
    """What one ``init`` event shows about the session's own set-up."""
    faults = []
    if init.get("model") != model:
        faults.append(f"the CLI reported model {init.get('model')!r}, not the requested {model!r}")
    if init.get("apiKeySource") != SUBSCRIPTION_KEY_SOURCE:
        faults.append(f"the CLI reported credential source {init.get('apiKeySource')!r}")
    if exomem:
        servers = {s.get("name"): s.get("status") for s in init.get("mcp_servers") or []}
        if servers.get("exomem") != "connected":
            faults.append(f"the Exomem MCP server was {servers.get('exomem')!r}, not 'connected'")
    return faults


def with_retries(
    attempt: Callable[[], dict],
    faults_of: Callable[[dict], list[str]],
    reset: Callable[[int], None],
) -> tuple[dict, list[dict]]:
    """Run ``attempt`` until it shows no harness fault, at most ``1 + RETRIES`` times.

    ``reset(n)`` puts back what attempt ``n`` (from 0) starts from (folder, vault, hook state), so
    a retry has the same inputs. Returns the last attempt's result and one record per failed
    attempt that was retried, ``{"attempt": n, "faults": [...]}``, which the caller stores beside
    the result. A fault in the last attempt stays in the result: it survived the retries.
    """
    retries: list[dict] = []
    for number in range(1 + RETRIES):
        reset(number)
        result = attempt()
        faults = faults_of(result)
        if not faults or number == RETRIES:
            break
        retries.append({"attempt": number + 1, "faults": faults})
    return result, retries


def books(
    scheduled: Collection[str] | int | None, valid: Collection[str], invalid: Collection[str]
) -> dict:
    """One arm's unit counts: every scheduled unit is valid, invalid (struck or voided) or missing.

    ``scheduled`` is the set of unit names, or a bare count when the units have no names yet. It
    is None when the run does not say how many units it planned; ``missing`` is then unknown. The
    names of the missing units are listed when the scheduled units are named.
    """
    names = None if scheduled is None or isinstance(scheduled, int) else set(scheduled)
    count = len(names) if names is not None else scheduled
    return {
        "scheduled": count,
        "valid": len(valid),
        "invalid": len(invalid),
        "missing": None if count is None else count - len(valid) - len(invalid),
        "invalid_units": sorted(invalid),
        "missing_units": None if names is None else sorted(names - set(valid) - set(invalid)),
    }


def incomplete_why(failed: Collection[str], arms: dict[str, dict]) -> list[str]:
    """Why a decision cannot be read yet: a failed judgement or an arm's missing units. Empty when it can.

    A decision decides nothing until a rerun completes the run, so an unknown count counts as incomplete too.
    """
    why = [f"the judgement of {item} failed" for item in sorted(failed)]
    for arm, body in sorted(arms.items()):
        units = body["units"]
        if units["missing"] is None:
            why.append(f"arm {arm}: the scheduled units are unknown")
        elif units["missing"]:
            named = units["missing_units"]
            why.append(
                f"arm {arm} is missing {named if named is not None else units['missing']} unit(s)"
            )
    return why
