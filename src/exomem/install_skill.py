"""install-skill: copy the bundled Exomem skill into Claude Code.

The Exomem MCP server is only the *hands* — the `find`/`add`/`note`/`edit` tools. The
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

from . import workflow_skills

# The Exomem skill ships inside the package, the same `_Schema/` that `init` lays into a
# vault. Resolving from `__file__` works for an installed wheel too, not just a
# git checkout — same pattern as init.py's _SCAFFOLD.
_SKILL_SRC = Path(__file__).parent / "_scaffold" / "_Schema"

# Claude Code loads `~/.claude/skills/<name>/SKILL.md`; the folder name must match
# the skill's `name:` frontmatter (`exomem`).
DEFAULT_TARGET = Path.home() / ".claude" / "skills" / "exomem"

# Before the skill was renamed to `exomem` it installed here. Left behind after an
# upgrade it loads as a stale duplicate, so `remove_legacy_skill` retires it — but
# only when it is unmistakably ours (see that function).
_LEGACY_TARGET = Path.home() / ".claude" / "skills" / "knowledge-base"
_LEGACY_MARKER = "name: knowledge-base"
_LEGACY_FINGERPRINT = "Exomem"


def install_skill(
    target: Path | None = None,
    *,
    force: bool = False,
    link: bool = False,
) -> dict:
    """Install the bundled Exomem skill into a Claude Code skills folder.

    Copies (or, with ``link=True``, symlinks) the canonical `_Schema/` — SKILL.md,
    references/, project-keys.yaml, and the workflow-skill definitions — to
    ``target`` (default ``~/.claude/skills/exomem``). The named workflow skills
    are also installed as sibling Claude Code skills beside the core `exomem`
    skill so clients can discover them directly.

    Args:
        target: Destination skill folder. Defaults to DEFAULT_TARGET.
        force: Overwrite an existing, non-empty target. Without it we refuse
            rather than clobber.
        link: Best-effort symlink instead of copy (keeps the install in sync as
            the skill evolves). Falls back to a copy if the OS refuses the
            symlink — e.g. Windows without Developer Mode.

    Returns:
        {"target": str, "mode": "copy"|"symlink"|"mixed", "files": int,
        "workflow_skills": [...]}.

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
    targets = _install_targets(target)

    for item in targets:
        src = item["source"]
        if not (src / "SKILL.md").is_file():
            raise FileNotFoundError(
                f"bundled skill missing at {src} (SKILL.md not found) — "
                "is the exomem install intact?"
            )
        dest = item["target"]
        if dest.exists() and not force and not _empty_dir(dest):
            raise FileExistsError(
                f"{dest} already exists. Pass force=True to overwrite it, or "
                "choose a different target."
            )

    records: list[dict] = []
    for item in targets:
        mode = _mirror_tree(item["source"], item["target"], link=link)
        files = _count_files(item["source"] if mode == "symlink" else item["target"])
        records.append(
            {
                "kind": item["kind"],
                "name": item["name"],
                "target": str(item["target"]),
                "mode": mode,
                "files": files,
            }
        )

    modes = {r["mode"] for r in records}
    return {
        "target": str(target),
        "mode": records[0]["mode"] if len(modes) == 1 else "mixed",
        "files": sum(r["files"] for r in records),
        "workflow_skills": [r for r in records if r["kind"] == "workflow"],
    }


def _install_targets(target: Path) -> list[dict]:
    targets = [
        {
            "kind": "core",
            "name": "exomem",
            "source": _SKILL_SRC,
            "target": target,
        }
    ]
    for skill in workflow_skills.list_skills():
        name = str(skill["name"])
        targets.append(
            {
                "kind": "workflow",
                "name": name,
                "source": workflow_skills.source_dir(name),
                "target": target.parent / name,
            }
        )
    return targets


def _empty_dir(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink() and not any(path.iterdir())


def _mirror_tree(src: Path, dest: Path, *, link: bool) -> str:
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Start from clean ground so the install is a faithful mirror, never a
    # half-merge of old and new files. Order matters: a symlink-to-dir reports
    # is_dir() True, so test is_symlink() first and only unlink the link.
    if dest.is_symlink() or dest.is_file():
        dest.unlink()
    elif dest.is_dir():
        shutil.rmtree(dest)

    if link:
        try:
            dest.symlink_to(src, target_is_directory=True)
            return "symlink"
        except OSError:
            # Restricted FS / no symlink privilege — fall through to a copy.
            pass

    shutil.copytree(src, dest)
    return "copy"


def _count_files(root: Path) -> int:
    return sum(1 for p in root.rglob("*") if p.is_file())


def remove_legacy_skill(legacy: Path | None = None) -> Path | None:
    """Retire a pre-rename ``knowledge-base`` skill install, if present and ours.

    Before the rename to ``exomem`` the skill installed at
    ``~/.claude/skills/knowledge-base``; left in place after an upgrade it loads as a
    stale, second skill with the old description. We remove it only when its SKILL.md
    still carries the old ``name: knowledge-base`` marker *and* the ``Exomem``
    fingerprint — so a user's own, unrelated skill that merely shares that folder name
    is never touched. Best-effort for copy installs; a ``--link`` symlink (whose
    content already reflects the renamed scaffold) is left for the user to clear.

    Returns the removed path, or ``None`` if there was nothing of ours to remove.
    """
    legacy = (Path(legacy) if legacy is not None else _LEGACY_TARGET).expanduser()
    skill_md = legacy / "SKILL.md"
    if not skill_md.is_file():
        return None
    try:
        head = skill_md.read_text(encoding="utf-8", errors="replace")[:4096]
    except OSError:
        return None
    if _LEGACY_MARKER not in head or _LEGACY_FINGERPRINT not in head:
        return None
    if legacy.is_symlink() or legacy.is_file():
        legacy.unlink()
    else:
        shutil.rmtree(legacy)
    return legacy
