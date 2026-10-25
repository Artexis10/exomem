"""Where the next converted families need rules that Records and Planning did not.

Records and Planning classify every operation by its selector value and import mode
alone. Two other families do not, and a rendering that splits them into read and write
tools needs more than a selector table. These tests pin the facts that a round-two
design starts from, using `resolve_invocation` on the grouped names (no rendering).
"""

from __future__ import annotations

import pytest

from exomem import commands
from exomem.command_surface import resolve_invocation
from exomem.governance import authorization_request, egress
from exomem.governance.authorization_request import AuthorizationRouteUnclassified, CredentialRule


def _command(name: str):
    return next(command for command in commands.PRODUCT_COMMANDS if command.name == name)


def test_govern_memory_credential_rule_reads_arguments_beyond_the_selector() -> None:
    """A split of `govern_memory` must hand authorization these flattened keys.

    The rule differs inside one `operation` value by `scope` and by `session_action`, so
    a rendering that keyed the rule on the operation alone would admit or refuse the wrong
    credential. Round two must flatten every key below to the top level before the
    authorization middleware runs, and must key variants on the pair, not the operation.
    """
    cases = [
        ({"operation": "grant"}, CredentialRule.REQUIRED),
        ({"operation": "grant", "scope": "session"}, CredentialRule.REQUIRED),
        ({"operation": "grant", "scope": "standing"}, CredentialRule.NON_AUTHORIZING),
        ({"operation": "session", "session_action": "open"}, CredentialRule.FORBIDDEN),
        ({"operation": "session", "session_action": "status"}, CredentialRule.REQUIRED),
    ]

    for arguments, expected in cases:
        command, flat = resolve_invocation("govern_memory", arguments, commands.PRODUCT_COMMANDS)
        assert flat == arguments
        assert authorization_request.credential_rule(command.name, flat) is expected

    published = {param.name for param in _command("govern_memory").params}
    assert {"operation", "session_action", "scope", "backfill_action"} <= published


@pytest.mark.parametrize(
    "arguments",
    [{}, {"operation": {"kind": "grant"}}, {"operation": "session"}, {"operation": "grant", "scope": "bogus"}],
    ids=["no-operation", "nested-operation", "session-without-action", "unknown-scope"],
)
def test_govern_memory_credential_rule_fails_closed_on_arguments_it_cannot_read(arguments) -> None:
    """An unflattened or incomplete call has no rule; it must never fall to a default."""
    with pytest.raises(AuthorizationRouteUnclassified):
        authorization_request.credential_rule("govern_memory", arguments)


def test_maintain_memory_effect_depends_on_dry_run_not_only_on_mode() -> None:
    """A split of `maintain_memory` must place a variant by (mode, dry_run), default included.

    Modes with a `dry-run-default` adapter read until `dry_run` is explicitly false, and
    modes with a `dry-run-opt-in` adapter write until `dry_run` is explicitly true. The
    omitted flag therefore means opposite effects for different modes, so a rendering must
    carry the flag in its variant key and keep the default per mode, not one global default.
    """
    command = _command("maintain_memory")
    adapters = egress.selector_registry()[("maintain_memory", "mode")]
    default_dry_run = [mode for mode, adapter in adapters.items() if adapter == "dry-run-default"]
    opt_in_dry_run = [mode for mode, adapter in adapters.items() if adapter == "dry-run-opt-in"]
    assert default_dry_run
    assert opt_in_dry_run

    def reads(mode: str, **flags: object) -> bool:
        _, flat = resolve_invocation(
            "maintain_memory", {"mode": mode, **flags}, commands.PRODUCT_COMMANDS
        )
        return commands.invocation_is_read_only(command, flat)

    for mode in default_dry_run:
        assert (reads(mode), reads(mode, dry_run=True), reads(mode, dry_run=False)) == (
            True,
            True,
            False,
        )
    for mode in opt_in_dry_run:
        assert (reads(mode), reads(mode, dry_run=True), reads(mode, dry_run=False)) == (
            False,
            True,
            False,
        )
