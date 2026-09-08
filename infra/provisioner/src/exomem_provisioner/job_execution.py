"""Closed Kubernetes Job execution-spec comparisons for privileged runners."""

from __future__ import annotations

import copy
import re
from typing import Any

_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,511}\Z")


def metadata_matches(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
    try:
        if any(
            actual.get(field, {}).get(key) != value
            for field in ("labels", "annotations")
            for key, value in expected[field].items()
        ):
            return False
        return not any(
            key.startswith("exomem.io/") and key not in expected[field]
            for field in ("labels", "annotations")
            for key in actual.get(field, {})
        )
    except (AttributeError, TypeError):
        return False


def pod_spec_matches(
    actual: dict[str, Any], expected: dict[str, Any], *, scheduled: bool = False
) -> bool:
    """Match a closed Pod spec plus only documented API/controller defaults."""
    defaults: dict[str, Any] = {
        "dnsPolicy": "ClusterFirst",
        "schedulerName": "default-scheduler",
        "terminationGracePeriodSeconds": 30,
        "enableServiceLinks": True,
        "serviceAccount": expected["serviceAccountName"],
        "preemptionPolicy": "PreemptLowerPriority",
        "priority": 0,
    }
    try:
        value = copy.deepcopy(actual)
        expected_value = copy.deepcopy(expected)
        for spec in (value, expected_value):
            for container in spec.get("containers", []) + spec.get("initContainers", []):
                for mount in container.get("volumeMounts", []):
                    if mount.get("readOnly") is False:
                        mount.pop("readOnly")
            for volume in spec.get("volumes", []):
                claim = volume.get("persistentVolumeClaim", {})
                if claim.get("readOnly") is False:
                    claim.pop("readOnly")
        for key, default in defaults.items():
            if key in value and value.pop(key) != default:
                return False
        if scheduled:
            if "nodeName" in value and not _IDENTITY.fullmatch(value.pop("nodeName")):
                return False
            tolerations = value.pop("tolerations", [])
            allowed = [
                {
                    "key": "node.kubernetes.io/" + key,
                    "operator": "Exists",
                    "effect": "NoExecute",
                    "tolerationSeconds": 300,
                }
                for key in ("not-ready", "unreachable")
            ]
            if any(item not in allowed for item in tolerations) or len(tolerations) > 2:
                return False
        return value == expected_value
    except (AttributeError, KeyError, TypeError):
        return False
