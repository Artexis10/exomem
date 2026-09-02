"""Historical Hosted profiles resolve a pinned legacy schema, not live objects.

`product_commands_for_profile` used to hand every profile the *same* canonical
`Command` objects the current product registry holds. A profile is an immutable
published identity, so that made every historical release's descriptor a
function of today's registry: adding one parameter to `capture_source` moved
`hosted-alpha-agent-v1`'s `schema_contract_sha256` and `compatibility_sha256`
away from the bytes its committed package was cut from, and widened what the
v1 wire actually admits, without touching a single v1 file.

That is not hypothetical here. The four changes that v5 carries add `adoption`
to `capture_source` and `preserve_artifacts`, `delivery` to `record_memory`,
and the curation arguments to `maintain_memory`; every one of those leaked into
v1-v4 through the shared objects.

So each historical profile pins the exact parameter names its committed
compatibility descriptor published, and v5 -- the profile these arguments were
released for -- is the only one that discovers them through the live registry.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import typing
from pathlib import Path

import pytest
import test_hosted_protected_tree_guard as guard

from exomem import (
    cli_ops,
    commands,
    hosted_gateway,
    hosted_legacy_schemas,
    hosted_plugins,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

HISTORICAL_CANDIDATES = (
    "hosted-alpha-agent-v1",
    "hosted-alpha-agent-v2",
    "hosted-alpha-agent-v3",
    "hosted-alpha-agent-v4",
)

#: Arguments introduced by the four changes v5 carries, and the command each
#: one arrived on. A historical profile must refuse every one of them.
V5_ONLY_ARGUMENTS: tuple[tuple[str, str], ...] = (
    ("capture_source", "adoption"),
    ("preserve_artifacts", "adoption"),
    ("record_memory", "delivery"),
    ("maintain_memory", "curation_action"),
    ("maintain_memory", "review_ref"),
    ("maintain_memory", "hydration_recheck"),
)


def _committed_compatibility(candidate: str) -> dict:
    generated = REPO_ROOT / "plugins" / "hosted" / "generated"
    if candidate != hosted_plugins.DEFAULT_CANDIDATE:
        generated = generated / "candidates" / candidate
    return json.loads((generated / "compatibility.json").read_text(encoding="utf-8"))


def _resolved(profile: str) -> dict[str, commands.Command]:
    return {
        command.name: command
        for command in commands.product_commands_for_profile(profile, "rest")
    }


@pytest.mark.parametrize("candidate", HISTORICAL_CANDIDATES)
def test_historical_resolved_command_schemas_reproduce_committed_identities(
    candidate: str,
) -> None:
    """The recomputed descriptor must equal the committed bytes exactly.

    This is the resolved-command-schema half of the v1-v4 immutability
    manifest: the byte manifest proves nobody edited the files, and this proves
    the pipeline still *derives* those same bytes from the current tree.
    """
    committed = _committed_compatibility(candidate)
    actual = hosted_plugins.compatibility_manifest(REPO_ROOT, candidate=candidate)
    assert committed == actual, hosted_plugins._json_difference_paths(committed, actual)


@pytest.mark.parametrize("candidate", HISTORICAL_CANDIDATES)
def test_historical_profiles_publish_their_pinned_parameter_names(candidate: str) -> None:
    committed = _committed_compatibility(candidate)
    profile = hosted_plugins.CANDIDATE_PROFILES[candidate]
    resolved = _resolved(profile)
    for entry in committed["agent_contract"]["commands"]:
        published = tuple(param["name"] for param in entry["params"])
        assert tuple(param.name for param in resolved[entry["name"]].params) == published


@pytest.mark.parametrize("candidate", HISTORICAL_CANDIDATES)
@pytest.mark.parametrize(("command_name", "argument"), V5_ONLY_ARGUMENTS)
def test_historical_profiles_refuse_v5_only_arguments_at_the_wire(
    candidate: str, command_name: str, argument: str
) -> None:
    """Absent from the descriptor is not enough; the wire must refuse the call.

    `cli_ops.coerce` is what the hosted command route runs before dispatch, so
    a parameter the pin dropped has to fail there with `UNKNOWN_PARAM` rather
    than reach a leaf that would happily accept it.
    """
    profile = hosted_plugins.CANDIDATE_PROFILES[candidate]
    resolved = _resolved(profile)
    command = resolved.get(command_name)
    if command is None:
        pytest.skip(f"{profile} does not expose {command_name}")
    assert argument not in {param.name for param in command.params}
    with pytest.raises(cli_ops.OpError) as raised:
        cli_ops.coerce(command.params, {argument: {}}, tool=command.name)
    assert raised.value.code == "UNKNOWN_PARAM"


@pytest.mark.parametrize(("command_name", "argument"), V5_ONLY_ARGUMENTS)
def test_v5_discovers_every_new_argument_through_the_live_registry(
    command_name: str, argument: str
) -> None:
    resolved = _resolved(commands.HOSTED_ALPHA_AGENT_V5_PROFILE)
    command = resolved[command_name]
    assert argument in {param.name for param in command.params}
    canonical = {entry.name: entry for entry in commands.PRODUCT_COMMANDS}
    assert command.params == canonical[command_name].params


def test_v5_is_not_pinned_and_every_historical_profile_is() -> None:
    pinned = hosted_legacy_schemas.LEGACY_PROFILE_PARAMS
    assert set(pinned) == {
        hosted_plugins.CANDIDATE_PROFILES[candidate] for candidate in HISTORICAL_CANDIDATES
    }
    assert commands.HOSTED_ALPHA_AGENT_V5_PROFILE not in pinned


@pytest.mark.parametrize("candidate", HISTORICAL_CANDIDATES)
def test_the_pin_is_derived_from_the_committed_descriptor_not_from_memory(
    candidate: str,
) -> None:
    """The pin is a copy of published bytes, so prove it against those bytes."""
    committed = _committed_compatibility(candidate)
    profile = hosted_plugins.CANDIDATE_PROFILES[candidate]
    pinned = hosted_legacy_schemas.LEGACY_PROFILE_PARAMS[profile]
    published = {
        entry["name"]: tuple(param["name"] for param in entry["params"])
        for entry in committed["agent_contract"]["commands"]
    }
    assert pinned == published


def test_the_pin_refuses_a_command_whose_pinned_parameter_disappeared() -> None:
    """Dropping a pinned parameter is a breaking change, not a silent narrowing.

    A pin that quietly tolerates a missing name would let a parameter removal
    ship as a no-op for the historical profiles and then fail far away, at the
    descriptor comparison, with nothing pointing at the removal.
    """
    command = next(
        entry for entry in commands.PRODUCT_COMMANDS if entry.name == "capture_source"
    )
    with pytest.raises(RuntimeError, match="pinned parameter"):
        commands.apply_legacy_profile_pin(
            command, ("title", "content", "a_parameter_that_never_existed")
        )


# ---------------------------------------------------------------------------
# Actual-wire identities, at the route rather than at `cli_ops.coerce`
#
# `coerce` is what the route calls, but asserting against it directly proves
# the coercer refuses the argument, not that the request does. Everything
# between -- profile resolution, trusted-context auth, the descriptor the
# gateway builds -- is exactly where a pinned schema could be lost. So these
# go through the real profile-scoped POST, using the protected-tree harness.
# ---------------------------------------------------------------------------

#: A value of the right shape for each argument. Coercion refuses an unknown
#: parameter before it looks at any value, so on a pinned profile these are
#: never read; on v5 they have to be plausible enough to get past coercion and
#: fail somewhere downstream instead.
V5_ONLY_ARGUMENT_VALUES: dict[str, object] = {
    "adoption": {"key": "synthetic:probe", "trigger": "selected", "selected_file_id": "probe"},
    "delivery": {"kind": "reference"},
    "curation_action": "status",
    "review_ref": "exomem://review/aaaaaaaaaaaaaaaaaaaaaaaa",
    "hydration_recheck": 3,
}


def _published_schema(profile: str, command_name: str) -> dict:
    contract = hosted_gateway.build_agent_gateway_contract(profile=profile)
    entry = next(item for item in contract["commands"] if item["name"] == command_name)
    return entry["mcp_tool"]["inputSchema"]


@pytest.mark.parametrize("candidate", HISTORICAL_CANDIDATES)
@pytest.mark.parametrize(("command_name", "argument"), V5_ONLY_ARGUMENTS)
def test_a_released_profile_refuses_a_v5_only_argument_over_the_real_route(
    tmp_path: Path, candidate: str, command_name: str, argument: str
) -> None:
    profile = hosted_plugins.CANDIDATE_PROFILES[candidate]
    if command_name not in _resolved(profile):
        pytest.skip(f"{profile} does not expose {command_name}")

    app, config = guard._cell(tmp_path, profile=profile)
    response = guard._call(
        app, config, command_name, {argument: V5_ONLY_ARGUMENT_VALUES[argument]}
    )

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "UNKNOWN_PARAM", response.text
    assert argument not in _published_schema(profile, command_name)["properties"]


@pytest.mark.parametrize(("command_name", "argument"), V5_ONLY_ARGUMENTS)
def test_v5_admits_the_same_body_over_the_real_route(
    tmp_path: Path, command_name: str, argument: str
) -> None:
    profile = commands.HOSTED_ALPHA_AGENT_V5_PROFILE
    app, config = guard._cell(tmp_path, profile=profile)
    response = guard._call(
        app, config, command_name, {argument: V5_ONLY_ARGUMENT_VALUES[argument]}
    )

    # Admitted, not necessarily successful: these bodies are deliberately
    # incomplete, so the call lands on a downstream refusal. What matters is
    # that it is no longer the coercer's.
    assert "UNKNOWN_PARAM" not in response.text, response.text
    assert "COMMAND_NOT_FOUND" not in response.text, response.text
    assert argument in _published_schema(profile, command_name)["properties"]


class _ForeignProbe:
    """A type that exists only in this module's namespace."""


def _foreign_module_leaf(
    vault_root: Path,
    kept: _ForeignProbe | None = None,
    dropped: _ForeignProbe | None = None,
) -> dict:
    """A leaf defined outside `exomem.commands`.

    Args:
        kept: Retained by the pin.
        dropped: Removed by the pin.
    """
    return {"vault_root": vault_root, "kept": kept, "dropped": dropped}


def test_the_pin_resolves_annotations_in_the_leaf_s_own_namespace() -> None:
    """A pinned wrapper must not lose a leaf's types by changing namespace.

    The wrapper is built inside `exomem.commands`, so its `__globals__` are that
    module's. Carrying the leaf's *string* annotations across would make
    `typing.get_type_hints` resolve them there and quietly fall back to an
    unannotated parameter -- which reaches the published MCP schema as an
    untyped field rather than as a failure.
    """
    canonical = next(
        entry for entry in commands.PRODUCT_COMMANDS if entry.name == "capture_source"
    )
    probe = dataclasses.replace(
        canonical,
        name="foreign_probe",
        leaf=_foreign_module_leaf,
        params=(
            commands.Param(name="kept", type="json", required=False, help="Retained."),
            commands.Param(name="dropped", type="json", required=False, help="Removed."),
        ),
        needs_schema=False,
    )

    pinned = commands.apply_legacy_profile_pin(probe, ("kept",))
    hints = typing.get_type_hints(pinned.leaf, include_extras=True)

    assert tuple(param.name for param in pinned.params) == ("kept",)
    assert "dropped" not in hints
    assert hints["kept"] == _ForeignProbe | None
    assert list(inspect.signature(pinned.leaf).parameters) == ["vault_root", "kept"]


def test_the_pin_source_revision_matches_the_manifest_it_was_cut_with() -> None:
    """Two pins, one moment. Say so, rather than leaving it to inspection."""
    manifest = json.loads(
        (REPO_ROOT / "tests/fixtures/hosted_v1_v4_immutability_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert hosted_legacy_schemas.SOURCE_REVISION == manifest["source_revision"]


def test_the_pin_names_the_descriptor_each_profile_was_read_from() -> None:
    payload = json.loads(
        (REPO_ROOT / "src/exomem/hosted_legacy_profile_schemas.json").read_text(encoding="utf-8")
    )
    sources = payload["sources"]

    assert set(sources) == set(hosted_legacy_schemas.LEGACY_PROFILE_PARAMS)
    for profile, relative in sources.items():
        descriptor = json.loads((REPO_ROOT / relative).read_text(encoding="utf-8"))
        assert descriptor["profile"] == profile
        assert {
            entry["name"]: tuple(param["name"] for param in entry["params"])
            for entry in descriptor["agent_contract"]["commands"]
        } == hosted_legacy_schemas.LEGACY_PROFILE_PARAMS[profile]
