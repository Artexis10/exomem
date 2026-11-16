"""Image-side script for the catalogue-prewarm mode: raise the installed parser version.

`build.build_catalogue_target_images` copies this file into a derived-image build and runs it as
root. Only the standard library is used, because it runs under the cell image's interpreter before
anything else is imported. It raises the integer literal `PARSER_VERSION` in the installed
`exomem/semantic_index.py` by the given step. The catalogue identity includes that number, so the
image then holds the same product with a different catalogue identity, as a release that changes
how pages are parsed does.
"""

from __future__ import annotations

import ast
import importlib.util
import pathlib
import sys

NAME = "PARSER_VERSION"


def bumped(source: str, step: int) -> str:
    """`source` with the module-level `PARSER_VERSION` integer literal raised by `step`."""

    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == NAME
            and isinstance(node.value, ast.Constant)
            and type(node.value.value) is int
        ):
            value = node.value
            if value.lineno != value.end_lineno:
                raise SystemExit(f"{NAME} spans several lines")
            # ast offsets count UTF-8 bytes, not characters.
            raw = lines[value.lineno - 1].encode("utf-8")
            raised = raw[: value.col_offset] + str(value.value + step).encode() + raw[value.end_col_offset :]
            lines[value.lineno - 1] = raised.decode("utf-8")
            return "".join(lines)
    raise SystemExit(f"no module-level integer assignment of {NAME} found")


def main(step: int) -> None:
    spec = importlib.util.find_spec("exomem")
    if spec is None or not spec.submodule_search_locations:
        raise SystemExit("the exomem package is not installed")
    target = pathlib.Path(next(iter(spec.submodule_search_locations))) / "semantic_index.py"
    before = target.read_text(encoding="utf-8")
    target.write_text(bumped(before, step), encoding="utf-8")
    # Bytecode records the source's mtime and size, so a stale file is ignored; removing it keeps the layer clean.
    for stale in target.parent.glob("__pycache__/semantic_index.*.pyc"):
        stale.unlink()


if __name__ == "__main__":
    main(int(sys.argv[1]))
