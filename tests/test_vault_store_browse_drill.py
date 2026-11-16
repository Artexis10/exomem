"""What a store-mode browse reads: its cost grows with its page limit, not with the vault.

OpenSpec make-vault-store-canonical, task 3.2 (browse latency budget). The load drill reads a
store of N generated pages with one child process per arm and sample, so two builds of the code
read the same bytes and the arms interleave under one load. The window tests reuse its pages and probe.
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
from pathlib import Path

import pytest
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_migration import vault  # noqa: F401
from test_vault_store_read_write import stored, writer  # noqa: F401

from exomem.vault_store import write

HUB = "Knowledge Base/Notes/Hub/Lantern Hub.md"
KINDS = ("note", "note", "note", "insight", "idea")
#: The hub's in-degree, fixed across vault sizes: a relation browse reads its relation set.
HUB_LINKS = 100

#: The drill's cells, as `commands.op_find` keywords. A browse is an empty query.
CELLS = {
    "types=note": {"types": ["note"]},
    "types=insight": {"types": ["insight"]},
    "no filter": {},
    "relation_of hub": {"relation_of": HUB},
}

#: One arm's sample, run in a child: each cell once cold (after `clear_cache`) and once warm. It
#: counts what a browse reads: the catalogue rows that its readers return (the same meter that
#: the call spans report) and the view payloads that the lexical store decodes.
PROBE = r"""
import json, sys, time
from pathlib import Path

root = Path(sys.argv[1])
cells = json.loads(sys.argv[2])
expected_src = sys.argv[3]

import exomem
assert exomem.__file__.startswith(expected_src), (exomem.__file__, expected_src)
from exomem import call_spans, commands, find as find_module, lexstore
from exomem.governance.principal import library_scope

decodes = [0]
real_loads = json.loads


class CountingJson:
    def __getattr__(self, name):
        return getattr(json, name)

    @staticmethod
    def loads(text, *args, **kwargs):
        if isinstance(text, str) and text.startswith('{"view":'):
            decodes[0] += 1
        return real_loads(text, *args, **kwargs)


lexstore.json = CountingJson()


def call(kwargs):
    decodes[0] = 0
    wall, cpu = time.perf_counter(), time.process_time()
    with call_spans.meter(call_spans.CATALOGUE_ROWS) as measured:
        hits = commands.op_find(root, query="", limit=15, graph=False, **kwargs)
    return {
        "wall_ms": (time.perf_counter() - wall) * 1000,
        "cpu_ms": (time.process_time() - cpu) * 1000,
        "rows": measured[call_spans.CATALOGUE_ROWS],
        "decodes": decodes[0],
        "hits": [hit.path for hit in hits],
    }


out = {}
with library_scope():
    call({"types": ["note"]})  # imports and first-use state belong to no cell
    for name, kwargs in cells.items():
        find_module.clear_cache()
        out[name] = {"cold": call(kwargs), "warm": call(kwargs)}
print(json.dumps(out))
"""


def drill_page(index):
    link = " It follows [[Lantern Hub]]." if index < HUB_LINKS else ""
    return (
        f"Knowledge Base/Notes/Gen/Page-{index:05d}.md",
        f"---\ntitle: Page {index}\ntype: {KINDS[index % len(KINDS)]}\n"
        f"updated: 2026-01-{1 + index % 28:02d}\n---\n# Page {index}\n\n"
        f"The lantern number {index} burns at the lighthouse harbour.{link}\n".encode(),
    )


def commit_drill_pages(writer, pages):  # noqa: F811
    hub = write.PagePut(HUB, b"---\ntitle: Lantern Hub\ntype: note\n---\n# Lantern Hub\n")
    write.commit(writer, write.StoreChange(write.Origin.GOVERNED, "drill", puts=(hub,)))
    for start in range(0, pages, 200):
        puts = tuple(write.PagePut(*drill_page(i)) for i in range(start, min(pages, start + 200)))
        write.commit(writer, write.StoreChange(write.Origin.GOVERNED, "drill", puts=puts))


def run_probe(store, source):
    """One sample of every cell, read by a child process that imports `source`'s build of the code."""
    done = subprocess.run(
        [sys.executable, "-c", PROBE, str(store), json.dumps(CELLS), source],
        capture_output=True,
        text=True,
        timeout=900,
        env={**os.environ, "PYTHONPATH": source},
    )
    assert done.returncode == 0, done.stderr[-3000:]
    return json.loads(done.stdout.splitlines()[-1])


def arms_from_environment():
    """`name=path-to-src` pairs from EXOMEM_BROWSE_DRILL_ARMS; this tree alone when unset."""
    named = os.environ.get("EXOMEM_BROWSE_DRILL_ARMS")
    if not named:
        return {"tree": str(Path(__file__).resolve().parents[1] / "src")}
    return dict(item.split("=", 1) for item in named.split(","))


@pytest.mark.skipif(not os.environ.get("EXOMEM_BROWSE_DRILL_REPORT"), reason="Explicit load drill")
@pytest.mark.timeout(7200)
def test_browse_load_drill(stored, writer):  # noqa: F811
    """Wall and CPU time, catalogue rows and view decodes of each browse cell, arm by arm."""
    pages = int(os.environ.get("EXOMEM_BROWSE_DRILL_PAGES", "2000"))
    samples = int(os.environ.get("EXOMEM_BROWSE_DRILL_SAMPLES", "7"))
    arms = arms_from_environment()
    commit_drill_pages(writer, pages)
    report_path = Path(os.environ["EXOMEM_BROWSE_DRILL_REPORT"])
    report = {"pages": pages, "samples": samples, "arms": arms, "cells": {}}
    found = {arm: [] for arm in arms}
    for _ in range(samples):
        for arm, source in arms.items():  # interleaved: a load swing reaches every arm alike
            found[arm].append(run_probe(stored, source))
    for cell in CELLS:
        report["cells"][cell] = {}
        for arm in arms:
            runs = [sample[cell] for sample in found[arm]]
            report["cells"][cell][arm] = {
                state: {
                    "wall_ms_median": round(
                        statistics.median(r[state]["wall_ms"] for r in runs), 1
                    ),
                    "cpu_ms_median": round(statistics.median(r[state]["cpu_ms"] for r in runs), 1),
                    "wall_ms_all": [round(r[state]["wall_ms"], 1) for r in runs],
                    "rows": sorted({r[state]["rows"] for r in runs}),
                    "decodes": sorted({r[state]["decodes"] for r in runs}),
                }
                for state in ("cold", "warm")
            }
        answers = {tuple(sample[cell]["cold"]["hits"]) for arm in arms for sample in found[arm]}
        report["cells"][cell]["same_hits_in_every_arm"] = len(answers) == 1
        report["cells"][cell]["hits"] = len(next(iter(answers)))
    report_path.write_text(json.dumps(report, indent=2))
    assert all(report["cells"][cell]["same_hits_in_every_arm"] for cell in CELLS), report_path
