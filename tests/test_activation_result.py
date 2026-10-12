"""Activation accounting observes strict flags and retains no packet content."""

import pytest

from exomem.activation_result import Observation, project_block


@pytest.mark.parametrize("flag", [True, False])
def test_unknown_reason_cannot_enter_the_observation(flag):
    packet = {
        "abstained": flag,
        "abstention": {"reason": "private content"},
        "units": [{"text": "private content"}],
    }
    observation = Observation()
    observation.capture(packet)
    packet["abstained"] = not flag
    assert observation.abstained is flag
    assert observation.result == {"abstained": flag, "reason": None, "budget_limited": False}


@pytest.mark.parametrize("budget", [None, {"applied": 1}, {"applied": "true"}, {}])
def test_invalid_budget_metadata_does_not_erase_strict_abstention(budget):
    observation = Observation()
    observation.capture({"abstained": True, "request_budget": budget})
    assert observation.abstained is True
    assert observation.result is None


@pytest.mark.parametrize("flag", [None, 1, "true", []])
def test_truthy_abstention_is_not_an_observation(flag):
    observation = Observation()
    observation.capture({"abstained": flag})
    assert observation.abstained is None
    assert observation.result is None


def test_row_projection_rebuilds_only_the_fixed_protocol():
    assert project_block({
        "abstained": False, "budget_limited": True, "reason": "unavailable", "text": "secret"
    }) == {"abstained": False, "budget_limited": True, "reason": "unavailable"}
    assert project_block({"abstained": True, "budget_limited": 1}) is None


def test_nested_request_context_restores_its_parent_and_non_activation_has_no_cell():
    from exomem.command_surface import current_activation_result, mcp_request_context

    assert current_activation_result() is None
    with mcp_request_context("same", tool="activate_context"):
        parent = current_activation_result()
        parent.capture({"abstained": True})
        with mcp_request_context("same", tool="activate_context"):
            child = current_activation_result()
            child.capture({"abstained": False})
            assert child is not parent
        assert current_activation_result() is parent
        assert parent.abstained is True
        with mcp_request_context("same", tool="ask_memory"):
            assert current_activation_result() is None
        assert current_activation_result() is parent
    assert current_activation_result() is None
