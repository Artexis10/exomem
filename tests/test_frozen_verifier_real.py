"""Real-byte admission gate for the frozen multilingual stance verifier.

The lean suite collects this file but skips the model load. Dedicated CI sets
``EXOMEM_RUN_REAL_NLI=1`` after downloading the pin's exact declared files.
"""

from __future__ import annotations

import os

import pytest

from exomem import claims


pytestmark = [
    pytest.mark.nli,
    pytest.mark.skipif(
        os.environ.get("EXOMEM_RUN_REAL_NLI") != "1",
        reason="real frozen-verifier gate requires EXOMEM_RUN_REAL_NLI=1",
    ),
]


def test_exact_pinned_multilingual_bytes_admit_every_fixture(monkeypatch) -> None:
    pin = claims.VERIFIER_PINS[0]
    digest, snapshot = claims._hash_resident_snapshot(pin)

    assert digest == pin.weights_sha256
    assert snapshot.endswith(f"/snapshots/{pin.model_revision}")

    monkeypatch.setenv("EXOMEM_CLAIM_POLARITY_NLI", "1")
    claims.reset_verifier_cache()
    admission = claims.verifier_admission()

    assert admission.admitted is True, admission.as_dict()
    assert admission.model_revision == pin.model_revision
    assert admission.artifact_files == pin.artifact_files
    assert admission.label_map_version == "v2"
    assert admission.fixture_set == "stance-v2-multilingual"
    assert admission.detail == (
        f"{len(claims.VERIFICATION_FIXTURES[pin.fixture_set])} fixtures green "
        f"for {pin.fixture_set!r}"
    )
