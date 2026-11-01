"""Every write command declares whether a contended call may be journaled, and why."""

from __future__ import annotations

from exomem.commands import COMMANDS, PRODUCT_COMMANDS

_TRANCHE_1_PRODUCT = {
    "remember",
    "replace_memory",
    "edit_memory",
    "observe_memory",
    "episode_memory",
    "manage_memory_file",
    "capture_source",
    "preserve_artifacts",
}


def test_every_write_command_declares_journal_eligibility_with_a_reason() -> None:
    undeclared = [
        command.name
        for command in (*COMMANDS, *PRODUCT_COMMANDS)
        if command.cli_writes and (command.intent_eligible is None or not command.intent_reason.strip())
    ]

    assert undeclared == []


def test_a_command_that_returns_a_bearer_credential_is_never_eligible() -> None:
    by_name = {command.name: command for command in PRODUCT_COMMANDS}

    assert by_name["transfer_artifact"].intent_eligible is False
    assert by_name["govern_memory"].intent_eligible is False


def test_a_read_command_declares_no_eligibility() -> None:
    reads = [command.name for command in PRODUCT_COMMANDS if not command.cli_writes and command.intent_eligible]

    assert reads == []


def test_tranche_1_is_the_set_the_design_names() -> None:
    eligible = {command.name for command in PRODUCT_COMMANDS if command.intent_eligible}

    assert eligible == _TRANCHE_1_PRODUCT
