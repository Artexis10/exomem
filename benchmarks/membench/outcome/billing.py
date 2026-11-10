"""Which account pays for a session: a run condition that every result records, never a defect.

The same model answers through a subscription login and through API credits, so a run may use
either. It declares which one it expects, and a session that reports the other is a harness
fault (``faults.init_faults``): one run never mixes sources by accident. The declaration also decides
the environment (``session_env``), and a report opens with one line that sets the declared source
against the sources its sessions reported (``line``).
"""

from __future__ import annotations

import os
from collections.abc import Mapping

#: The variable that makes the CLI bill API credits instead of the login.
KEY_VARIABLE = "ANTHROPIC_API_KEY"

#: The declaration: the billing name that this run's sessions must report. Absent means subscription.
ENV_VAR = "EXOMEM_OUTCOME_BILLING"

#: Billing name -> the ``apiKeySource`` that a session on it reports. The CLI fixes these values:
#: ``none`` for a subscription login, and the key's variable name when ``ANTHROPIC_API_KEY`` pays
#: (the CLI's ``init`` event reports ``apiKeySource``; ``benchmarks/import_guidance`` records it and
#: reads ``none`` as a subscription login). It is a closed set; any other value stays a fault.
KEY_SOURCES = {"subscription": "none", "credits": KEY_VARIABLE}
DEFAULT = "subscription"
#: What a source outside the set, or none at all, is called in a record.
UNKNOWN = "unknown"


def declared(env: Mapping[str, str] | None = None) -> str:
    """The billing source this run expects; refuses a name outside ``KEY_SOURCES``."""
    name = (os.environ if env is None else env).get(ENV_VAR) or DEFAULT
    if name not in KEY_SOURCES:
        raise SystemExit(f"{ENV_VAR} is {name!r}; it takes {' or '.join(KEY_SOURCES)}")
    return name


def of_key_source(api_key_source: object) -> str:
    """The billing name for the ``apiKeySource`` a session reported, ``unknown`` for any other value."""
    by_value = {value: name for name, value in KEY_SOURCES.items()}
    return by_value.get(api_key_source, UNKNOWN) if isinstance(api_key_source, str) else UNKNOWN


def session_env(env: Mapping[str, str], declared_name: str | None = None) -> dict[str, str]:
    """``env`` as every actor and judge session gets it: the declaration decides who pays.

    A subscription declaration drops ``ANTHROPIC_API_KEY``, so a shell that holds a key never bills it.
    A credits declaration needs the key, and refuses here, before any session, when it is absent.
    """
    name = declared_name or declared()
    out = {k: v for k, v in env.items() if k != KEY_VARIABLE}
    if KEY_SOURCES[name] == KEY_VARIABLE:
        if not env.get(KEY_VARIABLE):
            raise SystemExit(
                f"the run declares {ENV_VAR}={name}, but {KEY_VARIABLE} is not set: "
                f"export {KEY_VARIABLE} (a Console org's key), or declare {ENV_VAR}={DEFAULT}"
            )
        out[KEY_VARIABLE] = env[KEY_VARIABLE]
    return out


def line(declared_name: str | None, observed: list[str]) -> str:
    """The report's billing line: the declared source against the sources the run's sessions reported."""
    seen = ", ".join(observed) or UNKNOWN
    if not declared_name:
        return f"billing: declared source unknown; sessions reported {seen}."
    if observed == [declared_name]:
        return f"billing: {declared_name}, as declared."
    if not observed or UNKNOWN in observed:
        return (
            f"billing: declared {declared_name}; sessions reported {seen}, "
            "so some session's source is unknown. Read the figures as unverified."
        )
    return (
        f"billing: declared {declared_name}; sessions reported {seen}. "
        "The observed sources differ from the declaration: read the figures as a run condition."
    )
