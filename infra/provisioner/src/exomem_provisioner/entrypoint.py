"""Minimal environment-free smoke surface for container console scripts."""

from __future__ import annotations

import sys


def help_requested(program: str, description: str) -> bool:
    if sys.argv[1:] != ["--help"]:
        return False
    print(f"{program} - {description}; configuration is supplied through environment variables")
    return True
