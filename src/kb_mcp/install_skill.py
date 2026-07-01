"""install-skill: copy the bundled knowledge-base skill into Claude Code.

The MCP server is only the *hands* — the `find`/`add`/`note`/`edit` tools. The
*brain* that tells Claude when to capture, how to file a source, and how to
compile a note under the schema is the **skill**: `_scaffold/_Schema/SKILL.md`
plus its `references/`. Claude Code discovers skills at
`~/.claude/skills/<name>/SKILL.md`, so until the skill is installed the tools
just sit there and nothing captures on its own — the #1 "it does nothing" trap.

This makes that install a first-class, one-command operation straight from the
package (no Obsidian-vault round-trip), so a friend who only cloned the repo can
install the skill without access to anyone's vault.
"""

from __future__ import annotations

import shutil
from pathlib import Path

# The skill ships inside the package, the same `_Schema/` that `init` lays into a
# vault. Resolving from `__file__` works for an installed wheel too, not just a
# git checkout — same pattern as init.py's _SCAFFOLD.
_SKILL_SRC = Path(__file__).parent / "_scaffold" / "_Schema"

# Claude Code loads `~/.claude/skills/<name>/SKILL.md`; the folder name must match
# the skill's `name:` frontmatter (`knowledge-base`).
DEFAULT_TARGET = Path.home() / ".claude" / "skills" / "knowledge-base"


def install_skill(
    target: Path | None = None,
    *,
    force: bool = False,
    link: bool = False,
) -> dict:
    """Install the bundled knowledge-base skill into a Claude Code skills folder.

    Copies (or, with ``link=True``, symlinks) the canonical `_Schema/` — SKILL.md,
    references/, project-keys.yaml — to ``target`` (default
    ``~/.claude/skills/knowledge-base``). Claude Code picks it up as the
    `knowledge-base` skill on next start.

    Args:
        target: Destination skill folder. Defaults to DEFAULT_TARGET.
        force: Overwrite an existing, non-empty target. Without it we refuse
            rather than clobber.
        link: Best-effort symlink instead of copy (keeps the install in sync as
            the skill evolves). Falls back to a copy if the OS refuses the
            symlink — e.g. Windows without Developer Mode.

    Returns:
        {"target": str, "mode": "copy"|"symlink", "files": int}.

    Raises:
        FileNotFoundError: the bundled skill is missing (broken install).
        FileExistsError: target exists and is non-empty and ``force`` is False.
    """
    if not (_SKILL_SRC / "SKILL.md").exists():
        raise FileNotFoundError(
            f"bundled skill missing at {_SKILL_SRC} (SKILL.md not found) — "
            "is the exomem install intact?"
        )

    target = (Path(target) if target is not None else DEFAULT_TARGET).expanduser()

    # Refuse to clobber a real install. An empty dir is safe to fill (parent
    # mkdir often leaves one); a symlink or any non-empty content needs --force.
    if target.exists() and not force:
        empty_dir = (
            target.is_dir() and not target.is_symlink() and not any(target.iterdir())
        )
        if not empty_dir:
            raise FileExistsError(
                f"{target} already exists. Pass force=True to overwrite it, or "
                "choose a different target."
            )

    target.parent.mkdir(parents=True, exist_ok=True)

    # Start from clean ground so the install is a faithful mirror, never a
    # half-merge of old and new files. Order matters: a symlink-to-dir reports
    # is_dir() True, so test is_symlink() first and only unlink the link.
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.is_dir():
        shutil.rmtree(target)

    if link:
        try:
            target.symlink_to(_SKILL_SRC, target_is_directory=True)
            return {
                "target": str(target),
                "mode": "symlink",
                "files": _count_files(_SKILL_SRC),
            }
        except OSError:
            # Restricted FS / no symlink privilege — fall through to a copy.
            pass

    shutil.copytree(_SKILL_SRC, target)
    return {"target": str(target), "mode": "copy", "files": _count_files(target)}


def _count_files(root: Path) -> int:
    return sum(1 for p in root.rglob("*") if p.is_file())
