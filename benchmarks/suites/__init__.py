"""Public-suite LOCKFILE registry: the closed set of pinned external suites.

Each subdirectory pins one external evaluation suite (upstream repo/commit,
license, `checkout_env_var`) or, for a suite this programme cannot yet run,
records why as a `gap` entry. See `registry.py` for the schema this enforces.
"""

from __future__ import annotations
