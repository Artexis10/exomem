"""A smaller utility input envelope must constrain reservation AND usage."""
import asyncio
import json

import pytest


def backend(tmp_path, monkeypatch, limit=48000):
    from lme.metered import MeteredOpenAIBackend
    monkeypatch.setenv('OPENAI_API_KEY', 'test-only-not-a-credential')
    return MeteredOpenAIBackend(tmp_path / 'run', cap_usd=1,
        approval_token='instrument-test', input_token_limit=limit)


def test_smaller_input_reservation_is_persisted_and_charged(tmp_path, monkeypatch):
    b = backend(tmp_path, monkeypatch)
    config = json.loads((b.run_dir / 'config.json').read_text())
    assert config['reservation_input_tokens'] == 48000
    request = b._prepare_call({'model': b.wire_model}, max_tokens=2000,
        artifact_root=b.run_dir, request_id=None, kind='native_chat')
    assert request.reservation == pytest.approx(.14)  # GPT-4o fixture price, no HTTP
    assert b.ledger._running_total() == pytest.approx(.14)


@pytest.mark.parametrize('limit', [0, -1, True, 48000.5, 128001])
def test_invalid_input_limits_refuse_before_artifacts(tmp_path, monkeypatch, limit):
    from lme.metered import MeteredConfigurationError
    with pytest.raises(MeteredConfigurationError):
        backend(tmp_path, monkeypatch, limit)
    assert not (tmp_path / 'run').exists()


def test_chat_refuses_input_over_limit_before_reservation(tmp_path, monkeypatch):
    from lme.metered import MeteredConfigurationError
    b = backend(tmp_path, monkeypatch)
    monkeypatch.setattr(b, 'count_chat_tokens', lambda *a, **k: 48001)
    with pytest.raises(MeteredConfigurationError):
        asyncio.run(b.complete_messages([{'role': 'user', 'content': 'hello'}], tools=[], max_tokens=2000))
    assert b.ledger._running_total() == 0


def test_actual_usage_cannot_exceed_small_input_reservation(tmp_path, monkeypatch):
    from lme.metered import MeteredCallError
    b = backend(tmp_path, monkeypatch)
    with pytest.raises(MeteredCallError, match='reserved token bounds'):
        b._usage({'usage': {'prompt_tokens': 48001, 'completion_tokens': 1}}, max_tokens=2000)
    assert b._usage({'usage': {'prompt_tokens': 48000, 'completion_tokens': 2000}}, max_tokens=2000)[1:3] == (48000, 2000)


def test_prompt_path_obeys_input_limit_even_without_reasoning(tmp_path, monkeypatch):
    from lme.metered import MeteredConfigurationError
    b = backend(tmp_path, monkeypatch)
    monkeypatch.setattr(b, 'count_chat_tokens', lambda *a, **k: 48001)
    with pytest.raises(MeteredConfigurationError):
        b.complete('hello')
    assert b.ledger._running_total() == 0
