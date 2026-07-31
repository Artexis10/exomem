"""Privacy-safe synthetic vocabulary.

No real-name lists anywhere: people, organisations, projects, places, and
metrics are composed from syllable inventories, so generated corpora contain
no personal data by construction. Every function is pure given a
``random.Random`` instance. Keep this module free of absolute paths, e-mail
shapes, and personal tokens — the repository privacy scanner is asserted over
this file and over generated corpora in tests.
"""

from __future__ import annotations

from random import Random

_FIRST_START = ("Va", "Le", "Mi", "Tor", "Ren", "Sa", "Ori", "Ne", "Kai", "Del", "Ru", "Ista")
_FIRST_END = ("la", "no", "ric", "man", "vi", "dra", "sha", "len", "to", "ne")
_LAST_START = ("Kor", "Vel", "Mar", "Tas", "Bren", "Sol", "Har", "Quin", "Fen", "Ost")
_LAST_END = ("dal", "mir", "sen", "wick", "aro", "lis", "berg", "una", "eth", "ov")
_ORG_STEM = ("Norva", "Coril", "Vanta", "Meridi", "Ostra", "Kelva", "Tessel", "Arden", "Lumo", "Petra")
_ORG_SUFFIX = ("Labs", "Systems", "Group", "Works", "Analytics", "Institute", "Partners", "Dynamics")
_CODE_A = ("Lark", "Hollow", "Ember", "Cinder", "Vale", "Quarry", "Beacon", "Drift", "Sable", "Moss")
_CODE_B = ("spur", "gate", "field", "reach", "crest", "fall", "run", "point", "hollow", "bank")
_CITY_A = ("Vor", "Mel", "Tar", "Sil", "Ken", "Bar", "Nol", "Gris", "Fal", "Ulm")
_CITY_B = ("burg", "stead", "haven", "ford", "wick", "mont", "dale", "port", "moor", "ness")
_METRIC_A = ("flux", "yield", "drift", "load", "uptake", "churn", "latency", "margin", "recall", "burn")
_METRIC_B = ("index", "rate", "score", "ratio", "count", "window", "budget", "ceiling", "floor", "delta")
_NOUNS = (
    "ledger", "turbine", "orchard", "manifest", "harbor", "lattice", "archive", "beacon",
    "reservoir", "corridor", "compass", "quarry", "granary", "aqueduct", "foundry", "atlas",
)


def _pick(rng: Random, pool: tuple[str, ...]) -> str:
    return pool[rng.randrange(len(pool))]


def person_name(rng: Random) -> str:
    first = _pick(rng, _FIRST_START) + _pick(rng, _FIRST_END)
    last = _pick(rng, _LAST_START) + _pick(rng, _LAST_END)
    return f"{first} {last}"


def org_name(rng: Random) -> str:
    return f"{_pick(rng, _ORG_STEM)} {_pick(rng, _ORG_SUFFIX)}"


def project_name(rng: Random) -> str:
    return f"Project {_pick(rng, _CODE_A)}{_pick(rng, _CODE_B)}"


def city_name(rng: Random) -> str:
    return _pick(rng, _CITY_A) + _pick(rng, _CITY_B)


def metric_name(rng: Random) -> str:
    return f"{_pick(rng, _METRIC_A)} {_pick(rng, _METRIC_B)}"


def product_name(rng: Random) -> str:
    return f"{_pick(rng, _ORG_STEM)}{_pick(rng, _CODE_B)}"


def noun(rng: Random) -> str:
    return _pick(rng, _NOUNS)
