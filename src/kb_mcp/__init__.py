"""Deprecated alias package — kb_mcp was renamed to exomem.

`import kb_mcp` (and any `kb_mcp.<submodule>`) resolves to the SAME module
objects as `exomem.<submodule>` via a meta-path alias, so there is exactly one
copy of module state (caches, locks, sqlite handles) no matter which name a
caller imports. A `__path__`-sharing shim would instead load each submodule a
second time under the old name — two module instances, subtly divergent state.

Update imports to `exomem`; this shim stays for compatibility.
"""

import importlib
import importlib.abc
import importlib.util
import sys
import warnings

_OLD = "kb_mcp"
_NEW = "exomem"

warnings.warn(
    "the 'kb_mcp' package was renamed to 'exomem'; update imports "
    "(kb_mcp remains a compatible alias)",
    DeprecationWarning,
    stacklevel=2,
)


class _AliasLoader(importlib.abc.Loader):
    """Loader that hands back the already-imported exomem module object."""

    def __init__(self, real_name: str) -> None:
        self._real_name = real_name

    def create_module(self, spec):
        return importlib.import_module(self._real_name)

    def exec_module(self, module) -> None:
        pass  # the real module is already executed

    def get_code(self, fullname):
        # runpy (`python -m kb_mcp`) needs code objects; delegate to the real
        # module's loader so the alias stays a pure passthrough.
        real_spec = importlib.util.find_spec(self._real_name)
        return real_spec.loader.get_code(self._real_name)

    def is_package(self, fullname) -> bool:
        real_spec = importlib.util.find_spec(self._real_name)
        return bool(real_spec and real_spec.submodule_search_locations)


class _AliasFinder(importlib.abc.MetaPathFinder):
    """Route any kb_mcp[.X] import to the exomem[.X] module object."""

    def find_spec(self, fullname, path=None, target=None):
        if fullname != _OLD and not fullname.startswith(_OLD + "."):
            return None
        real = _NEW + fullname[len(_OLD):]
        if importlib.util.find_spec(real) is None:
            return None
        return importlib.util.spec_from_loader(fullname, _AliasLoader(real))


if not any(isinstance(f, _AliasFinder) for f in sys.meta_path):
    sys.meta_path.insert(0, _AliasFinder())

# Make `kb_mcp` itself BE the exomem package (attributes, __version__, ...).
_real = importlib.import_module(_NEW)
sys.modules[_OLD] = _real
