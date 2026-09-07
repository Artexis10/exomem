#!/usr/bin/env python3
"""Smoke a live hosted cell from inside its pod, with no client involved.

Verifying a hosted release used to mean driving real Claude and real ChatGPT
through seven manual operations and reading the answers off screenshots. That
ceremony exists for a reason -- `promotion_evidence.py` attests that a genuine
client did those things, and signing it without performing it forges the gate.
But it answers "did a client work", and most release questions are "does the
cell work". Those are different jobs, and only the first one needs a human.

On 2026-09-06 three questions -- is the write durable, is it indexed, is it
findable -- cost an evening of screenshots and were then resolved in four
read-only commands against the pod. This is those commands, with assertions.

WHAT THIS DOES NOT DO, AND WHY
------------------------------
It does not write. Writing straight into the vault bypasses the service's own
write path, so it would prove nothing about indexing; and driving the CLI
in-pod takes locks the running service holds -- an out-of-process
`exomem index --scope vault` against a live cell is a measured soft-deadlock,
~2,100 receipts in 40 minutes. A write tier belongs on the MCP surface with a
real token, not here. This tier is read-only and safe against production.

    smoke_hosted_cell.py --namespace exo-abc123
    smoke_hosted_cell.py --namespace exo-abc123 --expect-image sha256:fb6ebd66...
    smoke_hosted_cell.py --namespace exo-abc123 --expect-note five-users --json
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from dataclasses import dataclass, field

VAULT = "/var/lib/exomem/vault"
STATE = "/var/lib/exomem/state"
# The index stores moved out of the vault under `relocate-machine-local-state`;
# a `.sqlite` found under the vault is a dual-state leftover, not the live one.
VAULT_STATE_GLOB = f"{STATE}/vault-state/vault-*"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str) -> None:
        self.checks.append(Check(name, ok, detail))

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]

    def render(self) -> str:
        lines = [f"  {'ok  ' if c.ok else 'FAIL'}  {c.name}: {c.detail}" for c in self.checks]
        return "\n".join(lines)


def kubectl(namespace: str, *args: str, kubeconfig: str, binary: str) -> str:
    command = [*shlex.split(binary), "-n", namespace, *args]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=120,
        env={"KUBECONFIG": kubeconfig, "PATH": "/usr/local/bin:/usr/bin:/bin"},
    )
    if result.returncode != 0:
        raise SystemExit(f"kubectl failed: {' '.join(args)[:80]}: {result.stderr.strip()[:300]}")
    return result.stdout


def in_pod(namespace: str, pod: str, script: str, **kw) -> str:
    return kubectl(namespace, "exec", pod, "-c", "exomem", "--", "sh", "-c", script, **kw)


def evaluate_pod(status: dict, expect_image: str | None, report: Report) -> None:
    """Container health, the renewal sidecar, and the running image."""
    containers = status.get("status", {}).get("containerStatuses") or []
    inits = status.get("status", {}).get("initContainerStatuses") or []

    restarts = sum(int(c.get("restartCount", 0)) for c in containers + inits)
    report.add("no restarts", restarts == 0, f"{restarts} across {len(containers + inits)} containers")

    ready = all(c.get("ready") for c in containers) and bool(containers)
    report.add("containers ready", ready, f"{sum(1 for c in containers if c.get('ready'))}/{len(containers)}")

    # Two init containers is the admission rule the cell chart pins: custody
    # seeds the projected authorization, refresh is the native sidecar that
    # renews it before the one-hour attestation lapses.
    names = {c.get("name") for c in inits}
    report.add(
        "renewal sidecar present",
        "authorization-session-refresh" in names,
        ", ".join(sorted(names)) or "none",
    )
    refresh = next((c for c in inits if c.get("name") == "authorization-session-refresh"), None)
    report.add(
        "renewal sidecar running",
        bool(refresh and refresh.get("started")),
        f"started={refresh.get('started') if refresh else 'absent'}",
    )

    if expect_image:
        # `status.containerStatuses[].image` is the local CONFIG digest and never
        # matches the manifest digest you deployed -- checking it reports a
        # mismatch against a correctly-rolled pod. `imageID` carries the manifest
        # digest, and unlike `spec` it is what is actually running rather than
        # what was asked for, so a cached or mutated image cannot pass.
        running = {c.get("imageID", "") for c in containers + inits}
        matched = any(expect_image in image for image in running)
        report.add(
            "image matches",
            matched,
            expect_image if matched else ", ".join(sorted(i for i in running if i))[:140],
        )


def evaluate_index(counts: dict, pages: list[str], expect_note: str | None, report: Report) -> None:
    """The write -> index -> recall chain, which is the thing worth smoking."""
    # An empty deferred queue means indexing finished. A backlog here with the
    # graph in recovery is the accounting funnel, not a fresh regression.
    pending = sum(counts.get(t, 0) for t in ("semantic_upserts", "full_upserts", "graph_upserts"))
    report.add("index queue drained", pending == 0, f"{pending} pending upsert(s)")

    report.add("lexical index populated", counts.get("pages", 0) > 0, f"{counts.get('pages', 0)} page(s)")
    report.add(
        "embeddings populated",
        counts.get("chunks", 0) > 0,
        f"{counts.get('chunks', 0)} chunk(s)",
    )

    if expect_note:
        hit = [p for p in pages if expect_note in p]
        report.add(
            "expected note indexed",
            bool(hit),
            hit[0] if hit else f"{expect_note!r} not among {len(pages)} indexed page(s)",
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--namespace", required=True, help="cell namespace, e.g. exo-fd31c845...")
    parser.add_argument("--kubeconfig", default="/etc/rancher/k3s/k3s.yaml")
    parser.add_argument(
        "--kubectl",
        default="/usr/local/bin/k3s kubectl",
        help="kubectl invocation; k3s ships it as a subcommand and installs no `kubectl` binary",
    )
    parser.add_argument("--expect-image", help="substring of the runtime image digest that must be running")
    parser.add_argument("--expect-note", help="substring of a vault path that must be present AND indexed")
    parser.add_argument("--json", action="store_true", help="emit machine-readable results")
    args = parser.parse_args()

    kw = {"kubeconfig": args.kubeconfig, "binary": args.kubectl}
    report = Report()

    pods = json.loads(kubectl(args.namespace, "get", "pods", "-o", "json", **kw))
    running = [p for p in pods.get("items", []) if p.get("status", {}).get("phase") == "Running"]
    if not running:
        print(f"no Running pod in {args.namespace}", file=sys.stderr)
        return 2
    pod = running[0]["metadata"]["name"]
    evaluate_pod(running[0], args.expect_image, report)

    notes = [
        line
        for line in in_pod(
            args.namespace, pod, f"find {VAULT!r} -name '*.md' -not -path '*/.exomem/*'", **kw
        ).splitlines()
        if line.strip()
    ]
    report.add("vault has notes", bool(notes), f"{len(notes)} markdown file(s)")

    if args.expect_note:
        on_disk = [n for n in notes if args.expect_note in n]
        report.add(
            "expected note on disk",
            bool(on_disk),
            on_disk[0].replace(VAULT + "/", "") if on_disk else f"{args.expect_note!r} not found",
        )

    probe = f"""
import glob, json, sqlite3
root = sorted(glob.glob({VAULT_STATE_GLOB!r}))
if not root:
    print(json.dumps({{"error": "no vault-state directory"}}))
    raise SystemExit(0)
root = root[-1]
counts, pages = {{}}, []
for name, tables in ((".lexical.sqlite", ("pages",)),
                     (".embeddings.sqlite", ("chunks",)),
                     (".deferred-index.sqlite", ("semantic_upserts", "full_upserts", "graph_upserts"))):
    try:
        con = sqlite3.connect("file:%s/%s?mode=ro" % (root, name), uri=True)
        for table in tables:
            counts[table] = con.execute("select count(*) from " + table).fetchone()[0]
        if name == ".lexical.sqlite":
            pages = [r[0] for r in con.execute("select path from pages")]
    except Exception as exc:
        counts[name] = "ERR: %s" % exc
print(json.dumps({{"counts": counts, "pages": pages}}))
"""
    raw = in_pod(args.namespace, pod, f"python3 -c {shlex.quote(probe)}", **kw)
    parsed = json.loads(raw.strip().splitlines()[-1])
    if "error" in parsed:
        report.add("index stores found", False, parsed["error"])
    else:
        report.add("index stores found", True, f"{len(parsed['pages'])} indexed page(s)")
        evaluate_index(
            {k: v for k, v in parsed["counts"].items() if isinstance(v, int)},
            parsed["pages"],
            args.expect_note,
            report,
        )

    if args.json:
        print(json.dumps({"namespace": args.namespace, "pod": pod,
                          "checks": [vars(c) for c in report.checks]}, indent=2))
    else:
        print(f"smoke {args.namespace} ({pod}):")
        print(report.render())

    if report.failed:
        print(f"\n{len(report.failed)} check(s) failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
