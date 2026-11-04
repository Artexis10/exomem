"""Optional fit inference cannot add effects or lose baseline candidates."""

import asyncio
import hashlib
import json
from pathlib import Path

import httpx
import pytest
from test_context_catalogue import _page
from test_context_views import OWNER
from test_governance_egress import _external, write_rule, write_scope

from exomem import context_fit, context_views, find_corpus, working_set_conversation
from exomem.governance import principal


def test_structured_http_fit_stably_orders_only_the_supplied_pool(vault: Path) -> None:
    """A local schema reply can reorder evidence; omitted judgments remain eligible."""
    paths = [_page(vault, name, body="Comparison data.\n") for name in ("reference", "alternative", "third")]
    candidates = [{"ref": path, "source_ref": path, "source_version": find_corpus.CACHE.get(vault / path, vault).snapshot_hash} for path in paths]
    config = context_fit.Placement(
        placement="local", runtime="ollama", endpoint="http://127.0.0.1:11434",
        model="fixture-model", model_revision="fixture-digest", runtime_version="fixture-runtime",
        timeout_seconds=1.0, max_input_chars=32000, max_output_tokens=1000, resource_allowance_gib=1.0,
    )
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "fixture-runtime"})
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "fixture-model", "digest": "fixture-digest"}]})
        payload = json.loads(request.content)
        assert "tools" not in payload and payload["stream"] is False
        assert payload["format"]["properties"]["items"]
        items = [{"ref": paths[0], "source_version": candidates[0]["source_version"], "fit": "unrelated"},
                 {"ref": paths[2], "source_version": candidates[2]["source_version"], "fit": "relevant"}]
        return httpx.Response(200, json={"model": "fixture-model", "message": {"content": json.dumps({"items": items})}})

    phrase = "distinctive ephemeral transport sentence 782194"
    with principal.request_scope(OWNER):
        caller = context_views.CallerView.current(vault)
        ordered, metadata = context_fit.interpret(vault, caller=caller, candidates=candidates, turn=phrase,
            conversation=working_set_conversation.NONE, placement=config, transport=httpx.MockTransport(handler))
    assert ordered == [candidates[2], candidates[1], candidates[0]]
    assert metadata["state"] == "ready"
    assert calls == ["/api/version", "/api/tags", "/api/chat"]
    from exomem.state_paths import vault_state_dir

    for path in vault_state_dir(vault).rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert phrase.encode() not in data and hashlib.sha256(phrase.encode()).hexdigest().encode() not in data


@pytest.mark.parametrize("items", [
    [{"ref": "invented", "source_version": "v", "fit": "relevant"}],
    [{"ref": "a", "source_version": "stale", "fit": "relevant"}],
    [{"ref": "a", "source_version": "v", "fit": "relevant"}, {"ref": "a", "source_version": "v", "fit": "unrelated"}],
])
def test_invalid_identity_judgments_reject_the_complete_result(items) -> None:
    """An invented, stale, or duplicate identity cannot acquire ranking effects."""
    with pytest.raises(ValueError):
        context_fit.reorder([{"ref": "a", "source_version": "v"}], context_fit.FitResult.model_validate({"items": items}))


def test_api_disclosure_and_provider_outage_leave_the_baseline_available(vault: Path) -> None:
    """Caller admission does not authorize provider disclosure; timeout causes no fallback."""
    path = _page(vault, "restricted-comparison", body="Source data.\n")
    source = find_corpus.CACHE.get(vault / path, vault)
    candidates = [{"ref": path, "source_ref": path, "source_version": source.snapshot_hash}]
    write_scope(vault, paths=path)
    write_rule(vault, ceiling=0, audience="fixture-provider")
    config = context_fit.Placement(
        placement="api", runtime="openai", endpoint="https://provider.invalid/v1",
        model="fixture-model", model_revision="fixture-model", runtime_version="fixture-api",
        timeout_seconds=0.01, max_input_chars=32000, max_output_tokens=1000, resource_allowance_gib=1.0,
        provider_audience="fixture-provider", disclosure_authorized=True,
    )
    called = []

    async def handler(request):
        called.append(request.url.path)
        await asyncio.sleep(1)
        raise AssertionError("the request should time out")

    with principal.request_scope(OWNER):
        caller = context_views.CallerView.current(vault)
        baseline, status = context_fit.interpret(vault, caller=caller, candidates=candidates, turn="Compare sources",
            conversation=working_set_conversation.NONE, placement=config, transport=httpx.MockTransport(handler))
        assert baseline == candidates and status == {"state": "unavailable"} and called == []
        permitted = config.model_copy(update={"provider_audience": "permitted-provider"})
        baseline, status = context_fit.interpret(vault, caller=caller, candidates=candidates, turn="Compare sources",
            conversation=working_set_conversation.NONE, placement=permitted, transport=httpx.MockTransport(handler))
    assert baseline == candidates and status == {"state": "unavailable"}
    assert called == ["/v1/chat/completions"]


def test_excerpt_admission_does_not_authorize_full_source_inference(vault: Path) -> None:
    """A reader permitted a bounded excerpt cannot send the whole page to a model."""
    path = _page(vault, "excerpt-only", body="Full source text outside the admitted excerpt.\n")
    page = find_corpus.CACHE.get(vault / path, vault)
    candidates = [{"ref": path, "source_ref": path, "source_version": page.snapshot_hash}]
    write_scope(vault, paths=path)
    write_rule(vault, ceiling=5)
    config = context_fit.Placement(
        placement="local", runtime="ollama", endpoint="http://127.0.0.1:11434",
        model="fixture-model", model_revision="fixture-digest", runtime_version="fixture-runtime",
        timeout_seconds=1.0, max_input_chars=32000, max_output_tokens=1000, resource_allowance_gib=1.0,
    )

    def forbidden(request):
        raise AssertionError("excerpt admission must not send full source input")

    with principal.request_scope(_external()):
        caller = context_views.CallerView.current(vault)
        baseline, status = context_fit.interpret(vault, caller=caller, candidates=candidates,
            turn="Read the source", conversation=working_set_conversation.NONE,
            placement=config, transport=httpx.MockTransport(forbidden))
    assert baseline == candidates and status == {"state": "unavailable"}
