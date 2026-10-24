"""A stale name lookup refuses a name-linked retained input and asks for its own repair."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from test_connector_boundary import configured_boundary as configured_boundary
from test_episode_recovery import _write_source_rule
from test_latency_gate import _seed_freshness_live
from test_observe_memory import _write_page
from test_origin_bindings import _write

from exomem import cli_ops, commands, origin_bindings, provenance, readiness, working_set_runtime
from exomem.governance.principal import RequestPrincipal, owner_principal, request_scope

READER = RequestPrincipal(audience_id="client-a", surface="mcp")
TARGET = "Knowledge Base/Sources/visible-title.md"
UNRELATED = "Knowledge Base/Notes/Legacy/unrelated.md"
UNRELATED_SOURCE = (
    "---\ntitle: Unrelated\nstatus: active\nproject: public-project\n"
    "exomem_id: 00000000-0000-4000-8000-0000000000b2\nupdated: 2026-07-15\n---\n\n"
    "The kettle holds 2 litres.\n"
)


def _seed(vault: Path) -> dict:
    """A Source that names another page by title, under a live registry."""
    (vault / TARGET).parent.mkdir(parents=True, exist_ok=True)
    (vault / TARGET).write_text(
        "---\ntype: source\nexomem_id: 42345678-1234-5678-1234-567812345678\n"
        "title: Visible title\nstatus: active\n---\n\nTarget body.\n",
        encoding="utf-8",
    )
    binding = _write(vault, "Measured as [[Visible title]] reports.\n")
    _write_page(vault, rel=UNRELATED, source=UNRELATED_SOURCE)
    return binding


def _stamp(vault: Path) -> None:
    """A recall: the one stamped build that makes the name lookup current."""
    with request_scope(owner_principal(surface="mcp")):
        commands.op_activate_context(vault, turn="What does the report measure?")


def _unrelated_edit(vault: Path) -> None:
    commands.op_edit_memory(
        vault,
        path=UNRELATED,
        why="A correction to an unrelated page.",
        operation={"kind": "replace_string", "old_string": "2 litres", "new_string": "3 litres"},
    )


def _carrier(binding: dict) -> str:
    return provenance.encode_origin(
        {
            "inputs": {"original": binding},
            "assessments": [
                {
                    "inputs": ["original"],
                    "basis": "agent_assessment",
                    "by": "agent",
                    "reason": "Read from the original.",
                }
            ],
            "bindings": [],
        }
    )


def _remember(vault: Path, binding: dict) -> dict:
    return commands.op_remember(
        vault,
        content=_carrier(binding) + "\n\nThe report names its subject.\n",
        title="Report reading",
        status="draft",
    )


def _refusal(call: Callable[[], object]) -> dict:
    """The public error block a client receives for a refused write."""
    with pytest.raises(Exception) as refused:  # noqa: PT011 - the block is the assertion
        call()
    return cli_ops.error_dict(refused.value)


def test_a_write_refused_for_a_stale_lookup_schedules_the_build_and_then_commits(
    configured_boundary,
    vault: Path,
    monkeypatch,
) -> None:
    """Any write leaves the lookup stale; the refused retry itself heals the next attempt."""
    _, authenticate = configured_boundary
    binding = _seed(vault)
    _seed_freshness_live(vault)
    _stamp(vault)
    monkeypatch.setattr(readiness, "runtime_managed", lambda: True)
    with request_scope(authenticate("full")):
        _unrelated_edit(vault)
        refusal = _refusal(lambda: _remember(vault, binding))
        assert refusal["code"] == "ORIGIN_INPUT_LOOKUP_STALE"
        working_set_runtime.join_builds(vault)
        written = _remember(vault, binding)

    assert "Retry the same write" in refusal["remediation"]
    assert cli_ops.http_status_for(refusal["code"]) == 503
    assert (vault / written["path"]).is_file()


def _seed_governed(vault: Path) -> dict:
    """The linked Source under a rule that releases it to a named audience, lookup current."""
    binding = _seed(vault)
    _write_source_rule(vault, ceiling=6)
    _seed_freshness_live(vault)
    _stamp(vault)
    return binding


def test_a_restricted_caller_keeps_the_unavailable_refusal_for_a_stale_lookup(vault: Path) -> None:
    """Lookup state flips on every write, withheld pages included; a restricted audience gets no signal."""
    binding = _seed_governed(vault)
    with request_scope(READER):
        origin_bindings.resolve_origin_input(vault, binding)
    with request_scope(owner_principal(surface="mcp")):
        _unrelated_edit(vault)
    with request_scope(READER), pytest.raises(provenance.OriginError) as refused:
        origin_bindings.resolve_origin_input(vault, binding)

    assert refused.value.code == "ORIGIN_INPUT_UNAVAILABLE"


def test_a_lookup_that_no_build_can_settle_is_not_a_retryable_refusal(vault: Path) -> None:
    """Without a live registry a retry cannot change the answer, so the writer is not asked to retry."""
    binding = _seed(vault)
    _write_source_rule(vault, ceiling=6)
    assert working_set_runtime.name_lookup(vault).state == working_set_runtime.LOOKUP_UNAVAILABLE
    with (
        request_scope(owner_principal(surface="mcp")),
        pytest.raises(provenance.OriginError) as refused,
    ):
        origin_bindings.resolve_origin_input(vault, binding)

    assert refused.value.code == "ORIGIN_INPUT_UNAVAILABLE"


def _note_with_linked_carrier(vault: Path) -> tuple[dict, str]:
    binding = _seed_governed(vault)
    with request_scope(owner_principal(surface="mcp")):
        note = _remember(vault, binding)
    _stamp(vault)
    return binding, note["path"]


def _reads_carrier(vault: Path, path: str) -> bool:
    return "exomem-origin" in str(commands.op_read_memory(vault, path=path).get("body"))


def test_a_reader_gets_the_carrier_back_once_the_refusal_scheduled_build_lands(
    vault: Path,
    monkeypatch,
) -> None:
    """A reader whose lookup check refused sees no carrier, and the refusal itself schedules the heal."""
    _binding, note = _note_with_linked_carrier(vault)
    with request_scope(READER):
        assert _reads_carrier(vault, note)
    with request_scope(owner_principal(surface="mcp")):
        _unrelated_edit(vault)
    monkeypatch.setattr(readiness, "runtime_managed", lambda: True)
    with request_scope(READER):
        withheld = not _reads_carrier(vault, note)
        working_set_runtime.join_builds(vault)
        restored = _reads_carrier(vault, note)

    assert (withheld, restored) == (True, True)


def test_projection_removes_the_carrier_of_an_input_refused_for_a_stale_lookup(
    vault: Path,
) -> None:
    """The retryable refusal is not the skipped stale-binding code: the carrier stays withheld."""
    binding, note = _note_with_linked_carrier(vault)
    with request_scope(owner_principal(surface="mcp")):
        _unrelated_edit(vault)
        text = (vault / note).read_text(encoding="utf-8")
        with pytest.raises(provenance.OriginError, match="ORIGIN_INPUT_LOOKUP_STALE"):
            origin_bindings.resolve_origin_input(vault, binding, disclosure=True)
        projected = origin_bindings.project_origin_text(vault, text)

    assert "exomem-origin" in text
    assert "exomem-origin" not in projected
