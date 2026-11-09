"""automate-cell-storage-deletion: the platform chart's parts of the deletion purge. The live K3s job proves
what admission admits (test_k3s_integration); these renders prove which parts exist in which configuration."""

from __future__ import annotations

import pytest

from .test_media_edge_chart import EDGE, NAMESPACE, _scope_expression
from .test_platform_chart_headers import HELM, _find, _helm_template

pytestmark = pytest.mark.skipif(HELM is None, reason="helm binary not on PATH")


@pytest.mark.parametrize("settings", [(), EDGE], ids=["edge off", "edge on"])
def test_cellctl_may_relay_the_deletion_grants_with_the_media_edge_on_or_off(settings: tuple[str, ...]) -> None:
    """Defect: the relay's grant and admission exception hang on the media edge switch, so a platform without
    the edge never relays a grant and the key custodian never purges a deleted cell's backups."""
    documents = _helm_template(*settings)

    role = _find(documents, "Role", "cellctl-cell-deletions")
    binding = _find(documents, "RoleBinding", "cellctl-cell-deletions")
    assert role["metadata"]["namespace"] == binding["metadata"]["namespace"] == NAMESPACE
    assert role["rules"] == [
        {"apiGroups": [""], "resources": ["configmaps"], "resourceNames": ["exomem-cell-deletions"],
         "verbs": ["get", "patch"]},
        {"apiGroups": [""], "resources": ["configmaps"], "verbs": ["create"]},
    ]
    assert binding["subjects"] == [{"kind": "ServiceAccount", "name": "cellctl", "namespace": NAMESPACE}]
    assert "'exomem-cell-deletions'" in _scope_expression(documents, "may write only the ConfigMap")
