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
_ORG_STEM = (
    "Norva", "Coril", "Vanta", "Meridi", "Ostra", "Kelva", "Tessel", "Arden", "Lumo",
    "Petra", "Halden", "Corvi",
)
_ORG_SUFFIX = (
    "Labs", "Systems", "Group", "Works", "Analytics", "Institute", "Partners", "Dynamics",
    # Extended for 4b.32: the 10x8 space held 80 names against 56 organisations
    # drawn per corpus, so collisions were a birthday certainty rather than bad
    # luck. Reserved allocation needs room for every context's worst case.
    "Collective", "Foundry", "Consortium", "Bureau", "Holdings", "Cooperative",
    "Studios", "Ventures", "Networks", "Chambers", "Guild", "Union",
)
_CODE_A = (
    "Lark", "Hollow", "Ember", "Cinder", "Vale", "Quarry", "Beacon", "Drift", "Sable",
    "Moss", "Thistle", "Warren",
)
_CODE_B = (
    "spur", "gate", "field", "reach", "crest", "fall", "run", "point", "hollow", "bank",
    # Extended for 4b.32, same reason: 100 project names against 64 drawn.
    "ridge", "march", "cove", "span", "mere", "close", "shaw", "holt", "wold", "garth",
)
_CITY_A = ("Vor", "Mel", "Tar", "Sil", "Ken", "Bar", "Nol", "Gris", "Fal", "Ulm")
_CITY_B = ("burg", "stead", "haven", "ford", "wick", "mont", "dale", "port", "moor", "ness")
_METRIC_A = ("flux", "yield", "drift", "load", "uptake", "churn", "latency", "margin", "recall", "burn")
_METRIC_B = ("index", "rate", "score", "ratio", "count", "window", "budget", "ceiling", "floor", "delta")
_NOUNS = (
    "ledger", "turbine", "orchard", "manifest", "harbor", "lattice", "archive", "beacon",
    "reservoir", "corridor", "compass", "quarry", "granary", "aqueduct", "foundry", "atlas",
)


_CONCEPT_SUFFIX = (
    "Framework", "Charter", "Protocol", "Standard", "Programme", "Doctrine",
    "Scheme", "Accord",
)


def _pick(rng: Random, pool: tuple[str, ...]) -> str:
    return pool[rng.randrange(len(pool))]


# -- reserved allocation (4b.32) ---------------------------------------------
#
# Random draws made names collide by construction: 64 projects drawn from 100
# and 56 organisations from 80 is the birthday problem, and it produced 18
# names shared by 37 entities. Two entities under one name make every query
# naming it ambiguous, and in t07 it made three prompts grade mutually
# exclusive answers.
#
# The fix is the idiom t20 already uses for its cross-lingual pair, generalised:
# names come from an *index* rather than a draw, and each build context owns a
# disjoint set of indices. Templates coordinate through nothing, so isolation
# and determinism both hold — a context's names depend only on its own identity.
#
# Each space enumerates so that the fastest-varying component is the first one,
# which keeps a context's strided indices visually distinct rather than handing
# it a run of one stem.

ORG_SPACE = len(_ORG_STEM) * len(_ORG_SUFFIX)
PROJECT_SPACE = len(_CODE_A) * len(_CODE_B)
PERSON_SPACE = len(_FIRST_START) * len(_FIRST_END) * len(_LAST_START) * len(_LAST_END)
CITY_SPACE = len(_CITY_A) * len(_CITY_B)
PRODUCT_SPACE = len(_ORG_STEM) * len(_CODE_B)
CONCEPT_SPACE = len(_NOUNS) * len(_CONCEPT_SUFFIX)


def org_name_at(index: int) -> str:
    index %= ORG_SPACE
    return f"{_ORG_STEM[index % len(_ORG_STEM)]} {_ORG_SUFFIX[index // len(_ORG_STEM)]}"


def project_name_at(index: int) -> str:
    index %= PROJECT_SPACE
    return f"Project {_CODE_A[index % len(_CODE_A)]}{_CODE_B[index // len(_CODE_A)]}"


def person_name_at(index: int) -> str:
    index %= PERSON_SPACE
    first_start = _FIRST_START[index % len(_FIRST_START)]
    index //= len(_FIRST_START)
    first_end = _FIRST_END[index % len(_FIRST_END)]
    index //= len(_FIRST_END)
    last_start = _LAST_START[index % len(_LAST_START)]
    index //= len(_LAST_START)
    return f"{first_start}{first_end} {last_start}{_LAST_END[index % len(_LAST_END)]}"


def city_name_at(index: int) -> str:
    index %= CITY_SPACE
    return _CITY_A[index % len(_CITY_A)] + _CITY_B[index // len(_CITY_A)]


def product_name_at(index: int) -> str:
    index %= PRODUCT_SPACE
    return f"{_ORG_STEM[index % len(_ORG_STEM)]}{_CODE_B[index // len(_ORG_STEM)]}"


def concept_name_at(index: int) -> str:
    """A composed name for the kinds with no dedicated space.

    The bare 16-noun pool is smaller than the number of build contexts, so
    striding it would overflow on the very first slot. Composition buys the
    room without asking anyone to hand-write a hundred nouns.
    """

    index %= CONCEPT_SPACE
    return (
        f"{_NOUNS[index % len(_NOUNS)].title()} "
        f"{_CONCEPT_SUFFIX[index // len(_NOUNS)]}"
    )


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


_CYR_FIRST_START = (
    ("Жа", "Zha"),
    ("Ке", "Ke"),
    ("Фи", "Fi"),
    ("Цо", "Tso"),
    ("Рю", "Ryu"),
    ("Шэ", "She"),
    ("Ля", "Lya"),
    ("Дзо", "Dzo"),
)
_CYR_FIRST_END = (
    ("вара", "vara"),
    ("нело", "nelo"),
    ("мири", "miri"),
    ("таля", "talya"),
    ("сэна", "sena"),
    ("кори", "kori"),
)
_CYR_LAST_START = (
    ("Брэ", "Bre"),
    ("Вэ", "Ve"),
    ("Гро", "Gro"),
    ("Кша", "Ksha"),
    ("Мю", "Myu"),
    ("Тре", "Tre"),
)
_CYR_LAST_END = (
    ("дал", "dal"),
    ("мир", "mir"),
    ("сэн", "sen"),
    ("вик", "vik"),
    ("лар", "lar"),
    ("нов", "nov"),
)
_CYR_ORG_STEM = (
    ("Жавэ", "Zhave"),
    ("Керю", "Keryu"),
    ("Фицо", "Fitso"),
    ("Шэля", "Shelya"),
    ("Дзомю", "Dzomyu"),
    ("Трекша", "Treksha"),
)
_CYR_ORG_SUFFIX = (
    ("Тара", "Tara"),
    ("Вэна", "Vena"),
    ("Миро", "Miro"),
    ("Сэла", "Sela"),
    ("Корю", "Koryu"),
    ("Люма", "Lyuma"),
)
_CYR_DISCRIMINATOR = (
    ("Жэ", "Zhe"),
    ("Кю", "Kyu"),
    ("Фра", "Fra"),
    ("Цэ", "Tse"),
)


def _pick_pair(rng: Random, pool: tuple[tuple[str, str], ...]) -> tuple[str, str]:
    return pool[rng.randrange(len(pool))]


def person_name_cyr(rng: Random) -> tuple[str, str]:
    """Return one synthetic ``(Cyrillic name, Latin alias)`` from shared draws."""

    first_start, first_start_latin = _pick_pair(rng, _CYR_FIRST_START)
    first_end, first_end_latin = _pick_pair(rng, _CYR_FIRST_END)
    last_start, last_start_latin = _pick_pair(rng, _CYR_LAST_START)
    last_end, last_end_latin = _pick_pair(rng, _CYR_LAST_END)
    native = f"{first_start}{first_end} {last_start}{last_end}"
    latin = f"{first_start_latin}{first_end_latin} {last_start_latin}{last_end_latin}"
    return native, latin


def org_name_cyr(
    rng: Random, *, discriminator: int | None = None
) -> tuple[str, str]:
    """Return one synthetic ``(Cyrillic name, Latin alias)`` from shared draws."""

    stem, stem_latin = _pick_pair(rng, _CYR_ORG_STEM)
    suffix, suffix_latin = _pick_pair(rng, _CYR_ORG_SUFFIX)
    native = f"{stem} {suffix}"
    latin = f"{stem_latin} {suffix_latin}"
    if discriminator is not None:
        try:
            marker, marker_latin = _CYR_DISCRIMINATOR[discriminator]
        except IndexError as exc:
            raise ValueError("Cyrillic name discriminator is out of range") from exc
        native = f"{native} {marker}"
        latin = f"{latin} {marker_latin}"
    return native, latin
