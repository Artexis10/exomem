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

from exomem import context_fit, context_task, context_views, find_corpus, working_set_conversation
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


@pytest.mark.parametrize("placement,restricted_audience", [("api", "fixture-provider"), ("api", "external"), ("local", "external"), ("api", "split")])
def test_fit_transport_releases_linked_references_and_metadata_for_both_views(vault: Path, placement: str, restricted_audience: str) -> None:
    """Full-page admission cannot send a linked target or metadata that this model's view withholds."""
    hidden = _page(vault, "restricted-target", body="Confidential calibration material.\n")
    provider_hidden = _page(vault, "provider-restricted-target", body="Provider-confined calibration material.\n") if restricted_audience == "split" else None
    extra_unit = f"\n- [technique] Provider-confined extension uses [[{provider_hidden}]] ^provider-restricted\n" if provider_hidden else ""
    public = _page(vault, "public-comparison", body=f"Compare optical alignment against [[{hidden}|the reference]].\n\n- [technique] Confidential germination extension uses [[{hidden}]] ^restricted\n{extra_unit}")
    write_scope(vault, paths=hidden)
    write_rule(vault, ceiling=0, audience="external" if provider_hidden else restricted_audience)
    if provider_hidden:
        governance = vault / "Knowledge Base" / "_Governance"
        (governance / "scopes" / "provider.yaml").write_text(
            f'governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FC0\nname: Provider scope\npaths: ["{provider_hidden}"]\n', encoding="utf-8")
        (governance / "rules" / "provider.yaml").write_text(
            'governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FC1\nscope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FC0"]\naudience: fixture-provider\nceiling: 0\n', encoding="utf-8")
    page = find_corpus.CACHE.get(vault / public, vault)
    alternative = _page(vault, "public-alternative", body="Compare optical alignment with the alternate target.\n")
    alternative_page = find_corpus.CACHE.get(vault / alternative, vault)
    candidates = [{"ref": public, "source_ref": public, "source_version": page.snapshot_hash,
                   "title": f"[[{hidden}|the reference]]", "reason": f"See {hidden}"},
                  {"ref": alternative, "source_ref": alternative, "source_version": alternative_page.snapshot_hash}]
    config = context_fit.Placement(
        placement=placement, runtime="openai" if placement == "api" else "ollama",
        endpoint="https://provider.invalid/v1" if placement == "api" else "http://127.0.0.1:11434",
        model="fixture-model", model_revision="fixture-model" if placement == "api" else "fixture-digest",
        runtime_version="fixture-runtime", timeout_seconds=1.0, max_input_chars=32000,
        max_output_tokens=1000, resource_allowance_gib=1.0,
        provider_audience="fixture-provider" if placement == "api" else "", disclosure_authorized=placement == "api",
    )
    sent = []
    def handler(request):
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "fixture-runtime"})
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "fixture-model", "digest": "fixture-digest"}]})
        sent.append(json.loads(json.loads(request.content)["messages"][1]["content"]))
        supplied = sent[-1]["candidates"]
        judgments = [{"ref": item["ref"], "source_version": item["source_version"],
                      "fit": "relevant" if index else "unrelated"} for index, item in enumerate(supplied)]
        content = json.dumps({"items": judgments})
        reply = {"model": "fixture-model"}
        if placement == "api":
            reply["choices"] = [{"message": {"content": content}}]
        else:
            reply["message"] = {"content": content}
        return httpx.Response(200, json=reply)
    with principal.request_scope(OWNER if restricted_audience == "fixture-provider" else _external()):
        caller = context_views.CallerView.current(vault)
        sources = {item["source_ref"]: context_task.project_source(vault, item["source_ref"], caller) for item in candidates}
        for item in candidates:
            item["source_version"] = sources[item["source_ref"]].version
        baseline, status = context_fit.interpret(vault, caller=caller, sources=sources,
            candidates=candidates, turn="Compare optical alignment", conversation=working_set_conversation.NONE,
            placement=config, transport=httpx.MockTransport(handler))
    assert status["state"] == "ready" and baseline == [candidates[1], candidates[0]] and len(sent) == 1
    assert hidden not in json.dumps(sent)
    assert "Confidential germination extension" not in json.dumps(sent)
    if provider_hidden:
        assert provider_hidden not in json.dumps(sent) and "Provider-confined extension" not in json.dumps(sent)
        assert sent[0]["candidates"][0]["source_version"] != candidates[0]["source_version"]
    assert "Compare optical alignment" in sent[0]["candidates"][0]["source_text"]
    assert sent[0]["candidates"][0]["ref"] == public
    assert sent[0]["candidates"][0]["source_version"] != page.snapshot_hash


@pytest.mark.parametrize("stage", ["api", "version", "models", "model_item", "local", "deep_json"])
def test_malformed_http_envelopes_preserve_the_complete_baseline(vault: Path, stage: str) -> None:
    """Malformed success envelopes at every consumed transport boundary must leave existing routes available."""
    path = _page(vault, "public-comparison", body="Optical alignment comparison data.\n")
    page = find_corpus.CACHE.get(vault / path, vault)
    candidates = [{"ref": path, "source_ref": path, "source_version": page.snapshot_hash}]
    api = stage in ("api", "deep_json")
    config = context_fit.Placement(
        placement="api" if api else "local", runtime="openai" if api else "ollama",
        endpoint="https://provider.invalid/v1" if api else "http://127.0.0.1:11434",
        model="fixture-model", model_revision="fixture-model" if api else "fixture-digest",
        runtime_version="fixture-runtime", timeout_seconds=1.0, max_input_chars=32000,
        max_output_tokens=4096, resource_allowance_gib=1.0,
        provider_audience="fixture-provider" if api else "", disclosure_authorized=api,
    )
    def handler(request):
        if stage == "deep_json":
            return httpx.Response(200, content=b'{"model":"fixture-model","choices":' + b'[' * 20000 + b'0' + b']' * 20000 + b'}')
        if request.url.path == "/api/version":
            return httpx.Response(200, json=[] if stage == "version" else {"version": "fixture-runtime"})
        if request.url.path == "/api/tags":
            payload = ([] if stage == "models" else {"models": ["malformed"]} if stage == "model_item"
                       else {"models": [{"name": "fixture-model", "digest": "fixture-digest"}]})
            return httpx.Response(200, json=payload)
        return httpx.Response(200, json=[])
    with principal.request_scope(OWNER):
        baseline, status = context_fit.interpret(vault, caller=context_views.CallerView.current(vault),
            candidates=candidates, turn="Compare optical alignment", conversation=working_set_conversation.NONE,
            placement=config, transport=httpx.MockTransport(handler))
    assert baseline == candidates and status == {"state": "unavailable"}


@pytest.mark.parametrize("remaining_after_read", [0.5, 1.4])
def test_preparation_cannot_extend_the_inference_deadline(vault: Path, monkeypatch: pytest.MonkeyPatch, remaining_after_read: float) -> None:
    """Source preparation must preserve the delivery reserve and reduce the eventual HTTP allowance."""
    from types import SimpleNamespace

    from exomem import request_budget

    path = _page(vault, "public-comparison", body="Optical alignment comparison data.\n")
    page = find_corpus.CACHE.get(vault / path, vault)
    candidates = [{"ref": path, "source_ref": path, "source_version": page.snapshot_hash}]
    config = context_fit.Placement(
        placement="api", runtime="openai", endpoint="https://provider.invalid/v1",
        model="fixture-model", model_revision="fixture-model", runtime_version="fixture-runtime",
        timeout_seconds=5.0, max_input_chars=32000, max_output_tokens=1000,
        resource_allowance_gib=1.0, provider_audience="fixture-provider", disclosure_authorized=True,
    )
    remaining = [2.0]
    monkeypatch.setattr(request_budget, "current", lambda: SimpleNamespace(remaining=lambda: remaining[0]))
    original_get = find_corpus.CACHE.get
    def preparation(*args, **kwargs):
        result = original_get(*args, **kwargs)
        remaining[0] = remaining_after_read
        return result
    monkeypatch.setattr(find_corpus.CACHE, "get", preparation)
    called = []
    async def request(config, data, timeout, transport):
        called.append(timeout)
        return context_fit.FitResult(items=[])
    monkeypatch.setattr(context_fit, "_request", request)
    with principal.request_scope(OWNER):
        baseline, status = context_fit.interpret(vault, caller=context_views.CallerView.current(vault),
            candidates=candidates, turn="Compare optical alignment", conversation=working_set_conversation.NONE,
            placement=config)
    assert baseline == candidates
    if remaining_after_read <= request_budget.ACTIVATION_STAGE_RESERVE_SECONDS:
        assert status == {"state": "unavailable"} and called == []
    else:
        assert status["state"] == "ready"
        assert called == [pytest.approx(remaining_after_read - request_budget.ACTIVATION_STAGE_RESERVE_SECONDS)]


@pytest.mark.parametrize("failure", ["PreparationBudgetExceeded", "PreparationCapacityBusy"])
def test_optional_preparation_capacity_preserves_the_complete_baseline(vault: Path, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    """An optional source preparation lease cannot remove already-admitted caller routes."""
    from exomem import runtime_resources

    path = _page(vault, "public-comparison", body="Optical alignment comparison data.\n")
    page = find_corpus.CACHE.get(vault / path, vault)
    candidates = [{"ref": path, "source_ref": path, "source_version": page.snapshot_hash}]
    config = context_fit.Placement(
        placement="api", runtime="openai", endpoint="https://provider.invalid/v1",
        model="fixture-model", model_revision="fixture-model", runtime_version="fixture-runtime",
        timeout_seconds=1.0, max_input_chars=32000, max_output_tokens=1000,
        resource_allowance_gib=1.0, provider_audience="fixture-provider", disclosure_authorized=True,
    )
    def exhausted(*args, **kwargs):
        raise getattr(runtime_resources, failure)("source preparation is unavailable")
    monkeypatch.setattr(context_task, "project_source", exhausted)
    def forbidden(request):
        raise AssertionError("unprepared inputs must not reach transport")
    with principal.request_scope(OWNER):
        baseline, status = context_fit.interpret(vault, caller=context_views.CallerView.current(vault),
            candidates=candidates, turn="Compare optical alignment", conversation=working_set_conversation.NONE,
            placement=config, transport=httpx.MockTransport(forbidden))
    assert baseline == candidates and status == {"state": "unavailable"}
