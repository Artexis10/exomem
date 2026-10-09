"""Sync-provider evidence for store custody: a shipped pack the vault overlay can only extend.

Custody refuses a live store inside a file-sync root. Which files, folder names,
environment variables and vault plugins reveal such a root changes as providers
change, so it is registry data. Code knows only how to look for each evidence kind.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from .vault import kb_root
from .vocabulary import registry

#: How custody looks for a provider; collection_store.custody implements each kind.
# nosemgrep: ep-word-set -- The custody detector implements exactly these evidence kinds.
EVIDENCE_KINDS = frozenset({
    "root-entry", "root-file", "root-journal-prefix", "path-component",
    "platform-env", "windows-folder", "vault-core-plugin", "vault-community-plugin",
})
PACK = "sync-providers.yaml"


def registry_path(root: Path) -> Path:
    return kb_root(root) / "_Schema" / "sync-providers.yaml"


def _check(entry: registry.Entry, entries: Mapping[str, registry.Entry]) -> None:
    attributes = entry.attributes
    if attributes.get("evidence") not in EVIDENCE_KINDS:
        raise registry.RegistryError(
            "INVALID_SYNC_EVIDENCE: evidence must be one of " + ", ".join(sorted(EVIDENCE_KINDS)))
    value = attributes.get("value")
    if not isinstance(value, str) or not value.strip() or len(value) > 255:
        raise registry.RegistryError("INVALID_SYNC_EVIDENCE: value must be a short non-empty string")


SPEC = registry.RegistrySpec(
    name="sync-providers",
    stem="sync-providers",
    overlay=registry_path,
    adapter=registry.FixedPackAdapter(PACK, lambda: SPEC, _check),
    fields=frozenset({"label", "description", "status", "attributes"}),
    attributes=frozenset({"evidence", "value"}),
    immutable=frozenset({"attributes.evidence", "attributes.value"}),
    # Agents meet sync providers only through a custody refusal, not the bootstrap.
    summarize_keys=False,
)


def evidence(vault_root: Path) -> dict[str, tuple[str, ...]]:
    """Each evidence kind's values: every shipped entry plus the vault's added ones."""
    found: dict[str, list[str]] = {kind: [] for kind in EVIDENCE_KINDS}
    for entry in registry.load(SPEC, vault_root).active:
        found[entry.attributes["evidence"]].append(entry.attributes["value"])
    return {kind: tuple(values) for kind, values in found.items()}
