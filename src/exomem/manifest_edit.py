"""In-place edits of a collection manifest's YAML frontmatter, each verified against the document it edits.

A manifest may carry comments, saved views and audit state, so an edit replaces only the parsed node it
changes and keeps every other byte, as `record_formats.render_manifest_audit_head` does, rather than dumping the
document again. Every edit is applied to the parsed document too, and the edited text must parse to exactly that
document: a layout an edit does not foresee is refused, never written.
"""

from __future__ import annotations

import copy
import textwrap
from collections.abc import Mapping, Sequence
from typing import Any

import yaml

from . import vault


class ManifestLayoutError(ValueError):
    """The manifest's layout cannot take this edit in place; the confirmed `revise` still can."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _flow(value: Any) -> str:
    """`value` as one YAML flow node, without a document end marker."""
    text = yaml.safe_dump(value, default_flow_style=True, width=2**30, allow_unicode=True, sort_keys=False)
    return text.removesuffix("\n").removesuffix("\n...")


def _child(node: Any, key: str) -> tuple[Any, Any]:
    if not isinstance(node, yaml.MappingNode):
        return None, None
    return next(((name, value) for name, value in node.value if getattr(name, "value", None) == key), (None, None))


def _container(document: Any, path: Sequence[str]) -> Any:
    """The mapping a path of keys leads to in a parsed document."""
    for key in path:
        document = document[key]
    return document


class ManifestEdit:
    """One manifest's frontmatter under edit: `text` is the whole manifest as edited so far, `document` its parsed front."""

    def __init__(self, manifest_text: str) -> None:
        _front, _body, front_text = vault.parse_frontmatter(manifest_text, strict=True)
        self._whole = manifest_text
        self._original_front = front_text or ""
        self._front = self._original_front
        self.document: dict[str, Any] = yaml.safe_load(self._front)

    @property
    def text(self) -> str:
        return self._whole.replace(self._original_front, self._front, 1)

    def _node(self, path: Sequence[str]) -> tuple[Any, Any]:
        key_node, node = None, yaml.compose(self._front)
        for key in path:
            key_node, node = _child(node, key)
            if node is None:
                raise ManifestLayoutError(f"the manifest has no {'.'.join(path)}")
        return key_node, node

    def _commit(self, revised: str, expected: Mapping[str, Any]) -> None:
        try:
            intact = yaml.safe_load(revised) == expected
        except yaml.YAMLError:
            intact = False
        if not intact:
            raise ManifestLayoutError("an edit in place would move other content of this manifest")
        self._front = revised
        self.document = copy.deepcopy(dict(expected))

    def _stop(self, node: Any) -> int:
        """Where the lines that hold `node` end: after its last line for a block node, else after the line it ends on."""
        front, end = self._front, node.end_mark.index
        block = (isinstance(node, (yaml.MappingNode, yaml.SequenceNode)) and not node.flow_style) or (
            isinstance(node, yaml.ScalarNode) and node.style in {"|", ">"}
        )
        if block:
            return len(front) if end >= len(front) else end - node.end_mark.column
        newline = front.find("\n", end)
        return len(front) if newline == -1 else newline + 1

    def set_value(self, path: Sequence[str], value: Any) -> None:
        """Replace the node at `path` with `value`, written in flow style on its key's line."""
        key_node, node = self._node(path)
        expected = copy.deepcopy(self.document)
        _container(expected, path[:-1])[path[-1]] = copy.deepcopy(value)
        if isinstance(node, (yaml.MappingNode, yaml.SequenceNode)) and not node.flow_style:
            # A block value spans the lines under its key: it becomes one flow node after the key.
            stop = self._stop(node)
            lines = self._front[: stop]
            tail = ("\r\n" if "\r\n" in self._front else "\n") if lines.endswith("\n") else ""
            revised = self._front[: key_node.end_mark.index] + ": " + _flow(value) + tail + self._front[stop:]
        else:
            revised = self._front[: node.start_mark.index] + _flow(value) + self._front[node.end_mark.index :]
        self._commit(revised, expected)

    def rename_key(self, parent: Sequence[str], old: str, new: str) -> None:
        """Rename the key `old` of the mapping at `parent`, keeping its value and its place."""
        key_node, _node = self._node((*parent, old))
        expected = copy.deepcopy(self.document)
        container = _container(expected, parent)
        renamed = {(new if name == old else name): value for name, value in container.items()}
        container.clear()
        container.update(renamed)
        self._commit(
            self._front[: key_node.start_mark.index] + _flow(new) + self._front[key_node.end_mark.index :], expected
        )

    def add_entry(self, parent: Sequence[str], name: str, value: Any) -> None:
        """Append `name: value` after the last entry of the block mapping at `parent`; `value` is a mapping or a scalar."""
        _key, declared = self._node(parent)
        if not isinstance(declared, yaml.MappingNode) or declared.flow_style or not declared.value:
            raise ManifestLayoutError(f"{'.'.join(parent)} is not written one per line, so an entry cannot be added in place")
        newline = "\r\n" if "\r\n" in self._front else "\n"
        indent = " " * declared.value[0][0].start_mark.column
        value = dict(value) if isinstance(value, Mapping) else value
        entry = textwrap.indent(yaml.safe_dump({name: value}, sort_keys=False, allow_unicode=True), indent).replace("\n", newline)
        # The mapping ends where the next token starts (a sibling key, a later top-level key), or at the document
        # end. A last entry that is a block sequence or a block scalar ends on that same mark, so its own end is no
        # insertion line.
        end = declared.end_mark
        if end.index >= len(self._front):
            revised = self._front + entry if self._front.endswith("\n") else self._front + newline + entry.removesuffix(newline)
        else:
            line_start = end.index - end.column
            revised = self._front[:line_start] + entry + self._front[line_start:]
        expected = copy.deepcopy(self.document)
        _container(expected, parent)[name] = copy.deepcopy(value)
        self._commit(revised, expected)

    def remove_entry(self, parent: Sequence[str], name: str) -> None:
        """Remove the entry `name` of the block mapping at `parent`, with the lines that hold it."""
        key_node, node = self._node((*parent, name))
        start = key_node.start_mark.index - key_node.start_mark.column
        expected = copy.deepcopy(self.document)
        del _container(expected, parent)[name]
        self._commit(self._front[:start] + self._front[self._stop(node) :], expected)
