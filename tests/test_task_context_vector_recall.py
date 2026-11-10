"""Task discovery ranks with the dense reading search_memory uses, so wording that shares no term still finds a page.

The fixtures plant the vectors (an activation query lands where the test puts it, as a real encoder places a
paraphrase near its source); the vault, the catalogue, the resident recall matrix, the lexical index, egress
and the task-context lanes are real.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest
from test_activate_context_maps import _activate
from test_activation_related_pages import _planted_vault, _random_unit, _toward_turn
from test_context_catalogue import _built

from exomem import embeddings, lexstore, readiness

BODY = "Bring the handheld gauge to the depot service desk for a calibration quote."
PARAPHRASE = "Where do I take the pressure meter to get it recalibrated?"


def _task(vault: Path, turn: str, **kwargs) -> tuple[dict, set[str]]:
    block = _activate(vault, turn=turn, max_chars=8000, **kwargs)["task_context"]
    return block, {item["source_ref"] for item in block["candidates"]}


@pytest.fixture
def planted(vault: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    # The turn slice drops a candidate it already carries; with it off, the task context shows discovery itself.
    monkeypatch.setenv("EXOMEM_ACTIVATION_TURN_SLICE", "off")
    paths = _planted_vault(vault, monkeypatch, {"Gauge calibration": (BODY, _toward_turn(1.0))})
    _built(vault).close()
    return paths["Gauge calibration"]


def test_a_paraphrase_without_a_shared_term_finds_the_page_through_the_dense_reading(
    vault: Path, planted: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    _block, keyword_only = _task(vault, PARAPHRASE)
    assert planted not in keyword_only  # the keyword lane alone misses it

    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS")
    block, refs = _task(vault, PARAPHRASE)
    assert planted in refs
    assert block["state"] == "ready"


def test_a_stale_keyword_index_still_lets_the_dense_reading_find_the_page(
    vault: Path, planted: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Right after a cutover the keyword index lags; recall must return what the dense reading finds, marked partial."""
    stale = SimpleNamespace(value=None, readiness=SimpleNamespace(status="stale", complete=False))
    monkeypatch.setattr(lexstore, "search_bm25_result", lambda *args, **kwargs: stale)
    block, refs = _task(vault, PARAPHRASE)
    assert planted in refs
    assert block["state"] == "partial"
    assert {"role": "task_context", "reason": "lane_failed"} in block["missing"]


def test_an_unrelated_turn_gets_no_dense_candidates(vault: Path, planted: str, monkeypatch: pytest.MonkeyPatch) -> None:
    turn = "Thanks, that is everything for today."
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    keyword_block, keyword_only = _task(vault, turn)

    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS")
    # The turn's vector points nowhere near any stored chunk, so no chunk clears the chance band.
    monkeypatch.setattr(embeddings, "embed_activation_query_if_loaded", lambda text: _random_unit("unrelated " + text))
    block, refs = _task(vault, turn)
    assert refs == keyword_only
    assert block["state"] == keyword_block["state"]


def test_a_cold_matrix_in_a_running_service_is_reported_and_reloaded_in_the_background(
    vault: Path, planted: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    index = embeddings.get_embedding_index(vault)
    index.unload_cache()  # evicted after an idle gap, long after warm-up finished
    monkeypatch.setattr(readiness, "runtime_managed", lambda: True)
    monkeypatch.setattr(readiness, "should_defer", lambda component: False)
    started: list[bool] = []
    original = type(index).require_resident_matrix

    def require(self):
        started.append(True)
        return original(self)

    monkeypatch.setattr(type(index), "require_resident_matrix", require)
    block, refs = _task(vault, "Bring the handheld gauge to the depot for a calibration quote")
    assert planted in refs  # the keyword lane still ranks it
    assert block["state"] == "partial"
    assert {"role": "task_context", "reason": "lane_failed"} in block["missing"]
    assert started  # the lane asked for the matrix, which starts the one background load


def test_the_agents_focus_reaches_discovery(vault: Path, planted: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    _block, refs = _task(
        vault, "ok, continue", conversation={"focus": "handheld gauge calibration quote at the depot service desk"},
    )
    assert planted in refs
