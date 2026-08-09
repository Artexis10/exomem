"""Privacy and determinism guarantees of the membench vocabulary layer.

The synthetic corpus must be publishable: the wordbank and everything it can
emit have to pass the same privacy scanner that gates the repository's public
artifacts, and identical seeds must reproduce identical vocabulary.
"""

from __future__ import annotations

from pathlib import Path
from random import Random

from exomem.public_artifact_privacy import scan_artifact

from membench import wordbank

_GENERATORS = (
    wordbank.person_name,
    wordbank.org_name,
    wordbank.project_name,
    wordbank.city_name,
    wordbank.metric_name,
    wordbank.product_name,
    wordbank.noun,
)


def _sample_output(seed: int, rounds: int = 200) -> str:
    rng = Random(seed)
    lines = [fn(rng) for _ in range(rounds) for fn in _GENERATORS]
    return "\n".join(lines)


def test_wordbank_module_passes_privacy_scan() -> None:
    module_path = Path(wordbank.__file__)
    assert scan_artifact(module_path, label="membench/wordbank.py") == ()


def test_wordbank_output_passes_privacy_scan(tmp_path: Path) -> None:
    sample = tmp_path / "wordbank-sample.md"
    sample.write_text(_sample_output(seed=7), encoding="utf-8")
    assert scan_artifact(sample, label="wordbank-sample.md") == ()


def test_wordbank_output_contains_no_path_or_contact_shapes() -> None:
    text = _sample_output(seed=11)
    assert "@" not in text
    assert "/home/" not in text
    assert "\\" not in text


def test_wordbank_is_deterministic_per_seed() -> None:
    assert _sample_output(seed=3) == _sample_output(seed=3)
    assert _sample_output(seed=3) != _sample_output(seed=4)
