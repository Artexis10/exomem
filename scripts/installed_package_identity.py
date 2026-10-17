"""Controller provenance for current and older installed acceptance runtimes."""

import importlib.util
from pathlib import Path

from exomem import install_info

package_identity = getattr(install_info, "package_identity", None)
if package_identity is None:
    # Older targets lack this helper. Load only the controller's stdlib metadata
    # reader; its function still imports the actual target, without source-path injection.
    _spec = importlib.util.spec_from_file_location(
        "_exomem_controller_install_info",
        Path(__file__).resolve().parents[1] / "src/exomem/install_info.py",
    )
    _controller = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_controller)
    package_identity = _controller.package_identity
