"""The operator-side rehearsal reads discovery policy from the runtime image."""

import ast
import json
import os
import subprocess
import sys
from pathlib import Path


def test_rehearsal_policy_command_runs_against_the_runtime_registry():
    """The control-plane environment lacks runtime dependencies; the remote command must use the image's public names."""
    from exomem.command_surface import listed_names
    from exomem.commands import product_commands_for
    from exomem.operation_entries import DEFAULT_TOOL_SHAPE

    source = Path(__file__).resolve().parents[1] / "infra/cloud-rehearsal/src/cloud_rehearsal/scenarios.py"
    module = ast.parse(source.read_text())
    command = next(ast.literal_eval(node.value) for node in module.body
                   if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name)
                                                          and target.id == "CLOUD_DISCOVERY_POLICY_COMMAND"
                                                          for target in node.targets))
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("EXOMEM_CLOUD_") and key not in {"EXOMEM_DISABLE_TIER2", "EXOMEM_MCP_TOOL_SHAPE"}}
    policy = json.loads(subprocess.check_output([sys.executable, "-c", command], env=environment, text=True))

    commands = product_commands_for("mcp", cloud=True)
    cloud_names = {name for command in commands for name in listed_names(command, DEFAULT_TOOL_SHAPE)}
    assert policy["excluded"] and not set(policy["excluded"]) & cloud_names
    assert policy["adoption"] == sorted({name for command in commands if command.name == "adopt_vault"
                                         for name in listed_names(command, DEFAULT_TOOL_SHAPE)})
    assert policy["adoption"] and set(policy["adoption"]) <= cloud_names
