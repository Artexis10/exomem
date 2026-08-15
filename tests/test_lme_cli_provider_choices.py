"""The CLI's --provider choices must be the registry, not a copy of it.

A registered row the CLI cannot select is unreachable in practice, and a
hand-maintained duplicate of the closed registry drifts silently the moment a
row is added.
"""

from __future__ import annotations

import pytest


def _provider_action():
    from lme.cli import _parser

    for action in _parser()._subparsers._group_actions[0].choices["run"]._actions:
        if action.dest == "provider":
            return action
    raise AssertionError("the run subcommand has no --provider argument")


def test_cli_provider_choices_are_exactly_the_closed_registry() -> None:
    from lme.providers.registry import registered_provider_names

    assert tuple(sorted(_provider_action().choices)) == registered_provider_names()


def test_every_registered_row_is_selectable_from_the_cli() -> None:
    """Including rows whose execution model is not in-process."""

    from lme.providers.registry import provider_spec, registered_provider_names

    choices = set(_provider_action().choices)
    for name in registered_provider_names():
        assert name in choices, f"{name} is registered but unreachable from the CLI"
    subprocess_rows = [
        name for name in registered_provider_names()
        if provider_spec(name).descriptor.execution_model
        == "owned-subprocess-terminated-at-cleanup"
    ]
    assert subprocess_rows, "expected at least one out-of-process row to be selectable"
    assert set(subprocess_rows) <= choices


def test_the_cli_still_refuses_an_unregistered_provider() -> None:
    from lme.cli import _parser

    with pytest.raises(SystemExit):
        _parser().parse_args([
            "run", "--dataset", "x", "--reader", "stub", "--out", "y",
            "--provider", "supermemory-cloud",
        ])
