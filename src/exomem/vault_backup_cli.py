"""`exomem backup`: the owner's commands for the personal vault backup (OpenSpec change `add-personal-vault-backup`).

The commands run in a shell on the machine that holds the vault, so the person who runs one is the owner, as for
`exomem status`. There is no MCP tool or HTTP route for them: a restore writes bytes anywhere on the host's disk.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import vault_backup


def summary_line(block: dict[str, Any]) -> str:
    """One line for `exomem status`: the state, then each activity's last outcome and its next due time."""
    if block["state"] != vault_backup.CONFIGURED:
        return str(block.get("message") or block["state"])
    parts = [
        f"{label} {block[key]['state']} {block[key].get('at') or ''}".rstrip()
        for label, key in (("backup", "last_backup"), ("check", "last_check"), ("sampled restore", "last_sampled_restore"))
    ]
    findings = block["findings"]
    if findings == vault_backup.UNKNOWN:  # a lost record: no findings is not what it says
        return "; ".join([*parts, "findings unknown"])
    return "; ".join([*parts, f"{len(findings)} finding(s)" if findings else "no findings"])


def _print_status(block: dict[str, Any]) -> None:
    print(f"vault backup: {block.get('message') or block['state']}")
    if block["state"] != vault_backup.CONFIGURED:
        for key in ("code", "field"):
            if block.get(key):
                print(f"  {key}: {block[key]}")
        return
    print(f"  repository: {block['repository_id']} (fingerprint)  originals included: {block['include_originals']}")
    print("  retention keeps: " + ", ".join(f"{count} {name}" for name, count in block["keep"].items() if count))
    for label, key, due in (
        ("last backup", "last_backup", "backup"),
        ("last retention", "last_retention", None),
        ("last check", "last_check", "check"),
        ("last sampled restore", "last_sampled_restore", "sampled_restore"),
    ):
        entry = block[key]
        line = f"  {label}: {entry['state']} {entry.get('at') or ''}"
        print((f"{line}  next due: {block['next_due'][due]}" if due else line).rstrip())
    error = block["last_error"]
    if error == vault_backup.UNKNOWN:
        print("  last error: unknown")
    elif error:
        print(f"  last error: {error['code']} at {error['at']}")
    if "originals_excluded" in block:
        excluded = block["originals_excluded"]
        print(f"  originals excluded: {excluded['count']}, without a recoverable replica: "
              f"{excluded['without_recoverable_replica']}")
    if block["findings"] == vault_backup.UNKNOWN:
        print("  findings: unknown")
    for finding in [] if block["findings"] == vault_backup.UNKNOWN else block["findings"]:
        print(f"  finding: {finding['activity']} {finding.get('kind', '')} {finding.get('path', '')} {finding['code']}")


def _vault(raw: str | None) -> Path:
    if not raw:
        raise SystemExit("backup: set --vault or EXOMEM_VAULT_PATH")
    vault = Path(raw).expanduser()
    from . import state_migration

    state_migration.require_vault_state_ready(vault)
    return vault


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="evermatter backup",
        description=(
            "Back up this vault and its machine-local state to the restic repository that "
            f"{vault_backup.CONFIG_ENV} names, check the repository, and restore into a separate directory."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name, text in (
        ("status", "report the last backup, check and sampled restore, what is due next, and any finding"),
        ("run", "back up now; a check or sampled restore that is due runs after it"),
        ("restore", "restore files from a snapshot into a new directory, never into the vault"),
    ):
        child = commands.add_parser(name, help=text)
        child.add_argument("--vault", default=os.environ.get("EXOMEM_VAULT_PATH"), help="vault root (default: $EXOMEM_VAULT_PATH)")
        child.add_argument("--json", action="store_true", help="emit stable JSON")
        if name == "run":
            child.add_argument("--init-repository", action="store_true", help="create the repository when it does not exist")
        if name == "restore":
            child.add_argument("--target", type=Path, required=True, help="a new or empty directory outside the vault")
            child.add_argument("--snapshot", default="latest", help="snapshot ID (default: this vault's latest)")
            child.add_argument("--path", action="append", default=[], help="vault-relative path to restore; repeat for several; default: everything")
            child.add_argument("--state", action="store_true", help="restore the staged machine-local state instead of the vault")
    return parser


def main(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    vault = _vault(args.vault)
    try:
        if args.command == "status":
            result = vault_backup.status(vault)
            if args.json:
                print(json.dumps(result))
            else:
                _print_status(result)
            return 0
        if args.command == "run":
            result = vault_backup.run_now(vault, init_repository=args.init_repository)
            failed = [name for name in result["ran"] if result[name]["state"] in (vault_backup.FAILED, vault_backup.PARTIAL)]
            if args.json:
                print(json.dumps(result))
            else:
                for name in result["ran"]:
                    entry = result[name]
                    print(f"{name}: {entry['state']}" + (f" ({entry['code']})" if entry.get("code") else ""))
            return 1 if failed else 0
        result = vault_backup.restore(vault, args.target, snapshot=args.snapshot, paths=args.path, state=args.state)
        if args.json:
            print(json.dumps(result))
        else:
            print(f"restored {result['files']} file(s) of snapshot {result['snapshot_id'][:8]} into {result['target']}")
        return 0
    except vault_backup.BackupError as error:
        print(f"Error [{error.code}]" + (f": {error.detail}" if error.detail else ""), file=sys.stderr)
        return 1
