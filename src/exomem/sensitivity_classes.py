"""Sensitivity classes: the label a page carries when it holds material an owner may later restrict.

A class is one entry of the `sensitivity-classes` registry: a shipped pack plus the vault overlay
`_Schema/sensitivity-classes.yaml`. A page records its classes in the `sensitivity` frontmatter
field. The agent that captures the material decides them, because only reading the material says
what it is; the server validates the keys and never infers one from content. A captured page is
kept in full whatever its class.

Code holds no class key. It branches only on the closed `default_disclosure` value, which a later
change turns into a disclosure ceiling; here the value is recorded and reported.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .vault import kb_root
from .vocabulary import registry

PACK = "sensitivity-classes.yaml"
#: The `sensitivity` frontmatter field, which the `page-fields` pack declares a classify-only pointer.
FIELD = "sensitivity"
UNKNOWN_CODE = "SENSITIVITY_CLASS_UNKNOWN"
#: The closed `default_disclosure` values this registry declares; a later change implements them.
# nosemgrep: ep-word-set -- A closed attribute value that the registry schema fixes, not vocabulary.
DISCLOSURES = frozenset({"owner-only", "vault-default"})
#: How a result says where a page's classes came from.
BASIS_DECLARED, BASIS_KIND_DEFAULT, BASIS_NONE = "declared", "kind-default", "none"


class SensitivityError(ValueError):
    """A class that no capture or classify call may apply; the message starts with a stable code."""


def registry_path(root: Path) -> Path:
    return kb_root(root) / "_Schema" / "sensitivity-classes.yaml"


def _check(entry: registry.Entry, entries: Mapping[str, registry.Entry]) -> None:
    if entry.attributes.get("default_disclosure") not in DISCLOSURES:
        raise registry.RegistryError(
            "INVALID_SENSITIVITY_CLASS: default_disclosure must be one of " + ", ".join(sorted(DISCLOSURES)))


SPEC = registry.RegistrySpec(
    name="sensitivity-classes",
    stem="sensitivity-classes",
    overlay=registry_path,
    adapter=registry.FixedPackAdapter(PACK, lambda: SPEC, _check),
    fields=frozenset({"label", "description", "status", "attributes"}),
    # The sensitivity-classes schema fixes this attribute name.
    attributes=frozenset({"default_disclosure"}),
    # An entry bears behaviour once a rule reads `default_disclosure`, so a new key is a proposal.
    promotion="propose-save",
)


def active_keys(vault_root: Path | None) -> tuple[str, ...]:
    """The keys a page may carry: every active entry, in registry order.

    A malformed overlay leaves every shipped class in force (its finding reaches the owner through
    the registry), so a capture never loses a shipped class to an overlay typo.
    """
    return tuple(entry.key for entry in registry.load(SPEC, vault_root).active)


def validated(vault_root: Path | None, supplied: object) -> tuple[str, ...]:
    """The canonical classes of a supplied list, or a refusal that names the registered keys.

    A key is compared in registry token form, so `Identity` names `identity`. A duplicate counts
    once and the list keeps the caller's order. An empty list is no class.
    """
    if supplied is None:
        return ()
    if not isinstance(supplied, Sequence) or isinstance(supplied, str | bytes):
        raise SensitivityError(f"INVALID_SENSITIVITY: sensitivity is a list of class keys, such as {_example(vault_root)}")
    known = active_keys(vault_root)
    classes: list[str] = []
    for raw in supplied:
        key = registry.token(raw) if isinstance(raw, str) else ""
        if key not in known:
            raise SensitivityError(
                f"{UNKNOWN_CODE}: {raw!r} is not a registered sensitivity class. The registered classes are "
                f"{', '.join(known)}; save a new one with schema_memory(subject='sensitivity-classes') first")
        if key not in classes:
            classes.append(key)
    return tuple(classes)


def _example(vault_root: Path | None) -> str:
    return ", ".join(active_keys(vault_root)[:2])


@dataclass(frozen=True, slots=True)
class Classification:
    """The classes a page takes at capture and where they came from."""

    classes: tuple[str, ...] = ()
    basis: str = BASIS_NONE

    def frontmatter(self) -> dict[str, list[str]]:
        """The field the page records, or nothing when it is unclassified."""
        return {FIELD: list(self.classes)} if self.classes else {}


def classify_capture(
    vault_root: Path | None, declared: object, *, kind_default: Iterable[str] = ()
) -> Classification:
    """The classes of a capture: the caller's own judgement, else its Source kind's default, else none.

    An empty declaration is a decision of its own: the caller read the material and found it
    unclassified, so the kind default does not apply.
    """
    if declared is not None:
        classes = validated(vault_root, declared)
        return Classification(classes, BASIS_DECLARED if classes else BASIS_NONE)
    defaults = validated(vault_root, list(kind_default))
    return Classification(defaults, BASIS_KIND_DEFAULT if defaults else BASIS_NONE)


def report(vault_root: Path, classification: Classification, *, rel_path: str | None) -> dict[str, Any]:
    """`sensitivity` as a capture or classify result states it: the classes, their basis and the disclosure in force."""
    return {
        "classes": list(classification.classes),
        "basis": classification.basis,
        "disclosure": disclosure(vault_root, rel_path),
    }


def disclosure(vault_root: Path, rel_path: str | None) -> dict[str, Any]:
    """What governs who reads a page now, read from the live governance state.

    Nothing in governance reads a page's class yet, so this never claims a restriction: with no
    policy every client signed in as the owner reads the page, and with a policy the caller's own
    ceiling for the page is the policy's answer, whatever the class.
    """
    from .governance import egress, policy

    if policy.load(Path(vault_root)).empty:
        return {
            "governance": "off",
            "statement": "Governance is off, so every client signed in as the owner reads this page.",
        }
    level = None if rel_path is None else egress.release_level_for(Path(vault_root), rel_path, record=False)
    return {
        "governance": "on",
        "your_ceiling": "unknown" if level is None else level,
        "statement": "Governance is on. The ceiling is yours alone, from the current policy; "
                     "no rule reads a sensitivity class yet, so the class restricts nothing by itself.",
    }
