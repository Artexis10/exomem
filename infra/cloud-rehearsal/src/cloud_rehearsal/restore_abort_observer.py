"""One ordered kernel witness beside the actual Bash restore process.

The operator container mounts only the disposable PVC directory, read-only.
The writer holds one lease fd until process exit. Its CLOSE_WRITE event must
precede activity that removes partial files or roots and original root return.
Kernel queue overflow makes
this proof incomplete rather than silently losing an unsafe event.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

OBSERVER_SCHEMA = "exomem-restore-abort-observer-v1"


def main() -> int:
    from inotify_simple import INotify, flags

    root = Path("/observed-cell-volume")
    state_dir = Path("/var/lib/exomem-restore") / sys.argv[1]
    state_dir.mkdir(mode=0o700, parents=True)
    result_path = state_dir / "observer.json"
    roots = json.loads(sys.argv[2])
    original = {name: (root / name).stat().st_ino for name in roots}
    watch = INotify()
    parent = watch.add_watch(root, flags.CLOSE_WRITE | flags.CREATE | flags.DELETE | flags.MOVED_TO | flags.UNMOUNT)
    partial_watches = {}
    removed_watches = set()
    lease = f".restore-abort-{sys.argv[1]}.lease"
    state = {"schema": OBSERVER_SCHEMA, "armed": True, "original_inodes": original, "partial_inodes": {},
             "events": [], "errors": [], "bash_exit_code": None}

    def save() -> None:
        temporary = result_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state))
        temporary.replace(result_path)

    def consume(events) -> None:
        # These tokens are the maintained Linux inotify protocol, not a vocabulary.
        for event in events:
            if event.mask & (flags.Q_OVERFLOW | flags.UNMOUNT):
                state["errors"].append({"mask": event.mask, "reason": "incomplete kernel event stream"})
            if event.wd in partial_watches:
                if event.mask & (flags.DELETE | flags.MOVED_FROM | flags.DELETE_SELF | flags.MOVE_SELF):
                    state["events"].append({"event": "partial_activity", "root": partial_watches[event.wd], "mask": event.mask})
                if event.mask & flags.DELETE_SELF:
                    removed_watches.add(event.wd)
                if event.mask & flags.IGNORED and (event.wd not in removed_watches
                        or not any(item["event"] == "writer_closed" for item in state["events"])):
                    state["errors"].append({"reason": "a partial root watch stopped before writer closure and root removal"})
                continue
            if event.mask & flags.IGNORED:
                state["errors"].append({"reason": "the parent watch stopped"})
            if event.wd != parent:
                continue
            if event.name in original and event.mask & flags.CREATE:
                inode = (root / event.name).stat().st_ino
                if inode != original[event.name]:
                    if event.name in state["partial_inodes"]:
                        raise RuntimeError("a watched partial root was replaced")
                    child = watch.add_watch(root / event.name,
                        flags.DELETE | flags.MOVED_FROM | flags.DELETE_SELF | flags.MOVE_SELF | flags.UNMOUNT)
                    partial_watches[child] = event.name
                    state["partial_inodes"][event.name] = inode
            if event.name == lease and event.mask & flags.CLOSE_WRITE:
                state["events"].append({"event": "writer_closed"})
            # Initial move-aside uses MOVED_FROM; DELETE removes a partial root.
            if event.name in original and event.mask & flags.DELETE:
                state["events"].append({"event": "root_deleted", "root": event.name})
            if event.name in original and event.mask & flags.MOVED_TO:
                inode = (root / event.name).stat().st_ino
                state["events"].append({"event": "root_returned", "root": event.name, "inode": inode,
                                        "original": inode == original[event.name]})
        save()

    save()
    process = subprocess.Popen(["bash", "/release/infra/scripts/cloud_restore.sh", "run", "restore", sys.argv[1], *sys.argv[3:]])
    state["bash_pid"] = process.pid
    save()
    try:
        while process.poll() is None:
            consume(watch.read(timeout=250))
        # Read all events that preceded Bash's exit, without a background reader.
        while events := watch.read(timeout=0):
            consume(events)
    except Exception as error:  # noqa: BLE001 - preserve incomplete evidence and run recovery
        state["errors"].append({"reason": f"{type(error).__name__}: {error}"})
        if process.poll() is None:
            process.terminate()
    finally:
        state["bash_exit_code"] = process.wait(timeout=1800)
        save()
        # Docker retains stdout after it discards the container's private tmpfs.
        print(json.dumps(state), flush=True)
    return state["bash_exit_code"] if state["bash_exit_code"] >= 0 else 128 - state["bash_exit_code"]


if __name__ == "__main__":
    sys.exit(main())
