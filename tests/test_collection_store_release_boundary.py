"""What a store-routed request releases follows the ceiling and policy in force at its release.

A read pins path placement, the connector ceiling and governance policy while it works; a write
pins none of them. Each case changes the governance state inside the request, after the store
work and before the terminal filter, and reads what the caller is sent.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from test_collection_store_service import CID, service  # noqa: F401 - service is a fixture
from test_connector_boundary import PRIVATE, SCOPE, configured_boundary  # noqa: F401 - a fixture

from exomem import commands, writer_lease
from exomem.governance.principal import request_scope

NAMES_PRIVATE_PAGE = {"path": PRIVATE}


@pytest.fixture
def vault(service):  # noqa: F811 - the connector fixtures act on the vault the service serves
    return service.root


def released(root, who, change_governance, **arguments):
    """The result of a record_memory request that `change_governance` alters after its store work.

    The leaf also names the private page, as a read's related entry and as a write's affected path,
    so the result shows whether the release let it out.
    """
    command = next(command for command in commands.product_commands_for("mcp") if command.name == "record_memory")

    def leaf(*args, **kwargs):
        result = command.leaf(*args, **kwargs)
        change_governance()
        return {**result, "related": [NAMES_PRIVATE_PAGE], "affected_paths": [PRIVATE]}

    with request_scope(who):
        return writer_lease.invoke_command(replace(command, leaf=leaf), root, **arguments)


def test_a_ceiling_tightened_during_a_store_routed_read_applies_to_what_it_releases(
    service, configured_boundary  # noqa: F811
):
    """Defect: a read releases under the connector ceiling it first read, so a ceiling the host
    tightened while the read ran does not apply to what the caller receives."""
    config, authenticate = configured_boundary
    who = authenticate("full")

    def tighten():
        data = json.loads(config.read_text())
        data["clients"][1]["denied_scope_ids"] = [SCOPE]
        config.write_text(json.dumps(data))

    query = {"action": "query", "collection": CID}
    assert released(service.root, who, lambda: None, **query)["related"] == [NAMES_PRIVATE_PAGE]
    assert not released(service.root, who, tighten, **query).get("related")


def test_a_policy_changed_during_a_store_routed_write_applies_to_what_it_releases(
    service, configured_boundary, vault  # noqa: F811
):
    """Defect: a write releases under the policy it loaded before it waited for the mutation boundary,
    so a scope that grew to cover a page while the write ran does not apply to what the caller receives."""
    _, authenticate = configured_boundary
    who = authenticate("limited")
    scope = vault / "Knowledge Base/_Governance/scopes/private.yaml"

    def cover(project):
        scope.write_text(f"governance_version: 1\nid: {SCOPE}\nprojects: [{project}]\n", encoding="utf-8")

    cover("unrelated-project")
    def append(title):  # a repeated item would replay the first write instead of committing again
        return {"action": "append", "collection": CID, "item": {"title": title}, "why": "scope"}

    assert released(service.root, who, lambda: None, **append("first"))["affected_paths"] == [PRIVATE]
    assert released(service.root, who, lambda: cover("private-project"), **append("second"))["affected_paths"] == []
