"""Record the distinct syscalls of a container's processes with `strace -f -qq -c`, as sorted JSON per role.

Diagnostic helper of the `diag/kata-overhead` branch; it ships nothing. strace is the maintained tool; this
module only starts it as root on host process ids, stops it, and turns its `-c` table into data. The cell
runs under runc, so its processes are ordinary host processes that strace attaches to by id.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path


def parse_summary(text: str) -> dict[str, dict]:
    """Rows of the `strace -c` table: `% time  seconds  usecs/call  calls  [errors]  syscall`."""
    rows: dict[str, dict] = {}
    for line in text.splitlines():
        tokens = line.split()
        if len(tokens) not in (5, 6) or tokens[-1] == "total":
            continue
        try:
            float(tokens[0])
            calls = int(tokens[3])
            errors = int(tokens[4]) if len(tokens) == 6 else 0
        except ValueError:
            continue
        rows[tokens[-1]] = {"calls": calls, "errors": errors}
    return rows


class Tracer:
    """One `strace -f -qq -c` run, as root, on the given host process ids and every thread and child they make."""

    def __init__(self, out: Path, pids: list[int]):
        self.out = out
        self.pidfile = Path(str(out) + ".pid")
        self.pids = pids
        wanted = " ".join(f"-p {pid}" for pid in pids)
        script = f"strace -f -qq -c -o '{out}' {wanted} 2>'{out}.err' & echo $! > '{self.pidfile}'; wait $!"
        self.proc = subprocess.Popen(["sudo", "sh", "-c", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def wait_attached(self, pid: int, timeout: float = 30.0) -> bool:
        """True once the kernel reports a tracer on `pid`."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                    if line.startswith("TracerPid:") and int(line.split()[1]) != 0:
                        return True
            except (OSError, ValueError):
                return False
            time.sleep(0.05)
        return False

    def stop(self, timeout: float = 180.0) -> dict[str, dict]:
        """Interrupt strace so that it detaches and prints its table; return the parsed table."""
        try:
            pid = self.pidfile.read_text().strip()
        except OSError:
            pid = ""
        if pid.isdigit():
            subprocess.run(["sudo", "kill", "-INT", pid], check=False, capture_output=True)
        return self.finish(timeout)

    def finish(self, timeout: float = 180.0) -> dict[str, dict]:
        """Wait for strace to end by itself (every traced process exited) and return the parsed table."""
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        return parse_summary(self.out.read_text()) if self.out.exists() else {}


def role_document(role: str, phases: dict[str, dict[str, dict]], **meta) -> dict:
    """One role's syscalls as a sorted list, with each syscall's calls, errors and the phases it appeared in."""
    names = sorted({name for table in phases.values() for name in table})
    rows = []
    for name in names:
        seen = {phase: table[name] for phase, table in phases.items() if name in table}
        rows.append({
            "syscall": name,
            "calls": sum(entry["calls"] for entry in seen.values()),
            "errors": sum(entry["errors"] for entry in seen.values()),
            "phases": {phase: entry["calls"] for phase, entry in seen.items()},
        })
    only_one = {}
    for row in rows:
        if len(row["phases"]) == 1 and len(phases) > 1:
            only_one.setdefault(next(iter(row["phases"])), []).append({"syscall": row["syscall"], "calls": row["calls"]})
    return {"role": role, "tool": "strace -f -qq -c", "distinct": len(rows), "phases": sorted(phases),
            "syscalls": rows, "only_in_one_phase": only_one, **meta}


def roles_summary(documents: list[dict]) -> dict:
    """Distinct counts per role, the union, and the syscalls that only one role uses."""
    by_role = {doc["role"]: {row["syscall"] for row in doc["syscalls"]} for doc in documents}
    union = sorted(set().union(*by_role.values())) if by_role else []
    exclusive = {role: sorted(names - set().union(*(other for r, other in by_role.items() if r != role)))
                 for role, names in by_role.items()} if len(by_role) > 1 else {}
    return {"distinct_per_role": {role: len(names) for role, names in by_role.items()},
            "union_distinct": len(union), "union": union, "only_in_one_role": exclusive}
