"""Task discovery ranks with the dense lane search_memory uses, so wording that shares no term still finds a page.

The fixtures plant the vectors (every activation query lands on the planted page's direction, as a real encoder
places a paraphrase near its source); the vault, the catalogue, the resident recall matrix, the lexical index,
egress and the task-context lanes are real.
"""

from pathlib import Path

import pytest
from test_activate_context_maps import _activate
from test_activation_related_pages import _planted_vault, _toward_turn
from test_context_catalogue import _built

from exomem import embeddings, readiness

BODY = "Bring the handheld gauge to the depot service desk for a calibration quote."


def _task_refs(vault: Path, turn: str) -> tuple[dict, set[str]]:
    block = _activate(vault, turn=turn, max_chars=8000)["task_context"]
    return block, {item["source_ref"] for item in block["candidates"]}


@pytest.fixture
def planted(vault: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    # The turn slice drops a candidate it already carries; with it off, the task context shows discovery itself.
    monkeypatch.setenv("EXOMEM_ACTIVATION_TURN_SLICE", "off")
    paths = _planted_vault(vault, monkeypatch, {"Gauge calibration": (BODY, _toward_turn(1.0))})
    _built(vault).close()
    return paths["Gauge calibration"]


@pytest.mark.parametrize(
    "turn",
    [
        "Where do I take the pressure meter to get it recalibrated?",  # a paraphrase: no shared material term
        "handhled guage calbration qoute",  # typos in every word
    ],
)
def test_a_turn_without_a_shared_term_finds_the_page_through_the_dense_lane(
    vault: Path, planted: str, monkeypatch: pytest.MonkeyPatch, turn: str
) -> None:
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    _block, keyword_only = _task_refs(vault, turn)
    assert planted not in keyword_only  # the keyword lane alone misses it

    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS")
    _block, refs = _task_refs(vault, turn)
    assert planted in refs


def test_a_warming_encoder_keeps_the_keyword_result_and_says_it_is_partial(
    vault: Path, planted: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    embeddings.get_embedding_index(vault).unload_cache()  # the recall matrix is not resident yet
    monkeypatch.setattr(readiness, "should_defer", lambda component: component == "embeddings")
    block, refs = _task_refs(vault, "Bring the handheld gauge to the depot for a calibration quote")
    assert planted in refs  # the keyword lane still ranks it
    assert block["state"] == "partial"
    assert {"role": "task_context", "reason": "lane_failed"} in block["missing"]
