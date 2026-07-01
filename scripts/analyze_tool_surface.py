#!/usr/bin/env python
"""Offline tool-surface analysis for exomem — evidence for consolidation decisions.

Read-only, desk-side. Quantifies the LLM cost of the MCP tool surface so the
"28 tools is too many" question is answered with numbers, not vibes. Adds NO
server-side intelligence (pure-substrate): it just introspects the schemas the
server already exposes, reuses the local embedding model the server already
loads, and mines the call log the server already writes.

Three measurements:

  M1  Static surface cost   — serialized MCP schema size per tool (what an
      *eager* client like a claude.ai connector loads every conversation),
      split Tier 1 vs Tier 2, per-server and x2 for the two-connector reality.

  M2  Confusability matrix  — pairwise cosine similarity of tool summaries
      embedded with BAAI/bge-base-en-v1.5. High-similarity clusters are the
      tools an LLM is most likely to mis-select between (the merge candidates).

  M3  Real-usage mining     — tool-call frequency + adjacent bigrams parsed
      from logs/exomem.log (CallTraceMiddleware). Never-called tools are dead
      surface; recurring bigrams are merge-or-document candidates.

Emits `analysis/tool-surface-report.md` and prints a synthesis table.

Usage:
    uv run python scripts/analyze_tool_surface.py
    uv run python scripts/analyze_tool_surface.py --skip-confusability   # no torch
    uv run python scripts/analyze_tool_surface.py --log path/to/exomem.log
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_MD = REPO_ROOT / "src" / "kb_mcp" / "_scaffold" / "_Schema" / "SKILL.md"
DEFAULT_LOG = REPO_ROOT / "logs" / "exomem.log"
REPORT_PATH = REPO_ROOT / "analysis" / "tool-surface-report.md"

# Clusters we expect to be confusable / mergeable, per the plan. M2 confirms or
# refutes each with a real similarity number; the synthesis ranks them.
CANDIDATE_CLUSTERS: dict[str, list[str]] = {
    "edit-family": ["edit", "multi_edit", "set_take", "set_frontmatter_field"],
    "delete-family": ["delete_file", "delete_directory"],
    "trash-family": ["list_trash", "recover_from_trash"],
    "read-list-family": [
        "get", "get_frontmatter", "list_directory",
        "list_inbound_links", "append_to_file", "create_file", "create_directory",
    ],
    "create-family (KEEP separate — discipline)": ["add", "note", "link", "preserve"],
}

# Per-cluster caveats surfaced under the synthesis table — where the embedding
# number alone would mislead.
CLUSTER_NOTES: dict[str, str] = {
    "edit-family": (
        "Descriptions read distinct (max cosine ~0.76, below threshold), so "
        "mis-selection risk is lower than assumed — but it's functionally ONE "
        "mechanism (`multi_edit`/`set_take` are documented as variants of `edit`). "
        "Merge rationale here is redundant surface, not confusability."
    ),
}

CONFUSE_THRESHOLD = 0.80


# --------------------------------------------------------------------------- #
# token estimation                                                            #
# --------------------------------------------------------------------------- #
def _approx_tokens(text: str) -> int:
    """Estimate tokens. Uses tiktoken cl100k if available (a closer proxy than
    chars/4), else the chars/4 heuristic. Either way it's consistent across the
    before/after runs, which is all the deltas need."""
    try:
        import tiktoken

        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:
        return round(len(text) / 4)


# --------------------------------------------------------------------------- #
# server introspection                                                        #
# --------------------------------------------------------------------------- #
def _build_and_list(disable_tier2: bool) -> list:
    """Build the FastMCP server and return its registered Tool objects.

    Mirrors tests/test_tier2.py::_registered_tool_names: neutralize the server's
    load_dotenv so our env toggles (KB_MCP_DISABLE_TIER2/_EMBEDDINGS) stick, and
    list tools without running middleware.
    """
    from kb_mcp import server as server_module

    server_module.load_dotenv = lambda *a, **k: None  # type: ignore[assignment]
    if disable_tier2:
        os.environ["KB_MCP_DISABLE_TIER2"] = "1"
    else:
        os.environ.pop("KB_MCP_DISABLE_TIER2", None)
    mcp = server_module.build_server(require_auth=False)
    return asyncio.run(mcp.list_tools(run_middleware=False))


def _tool_wire_dict(tool) -> dict:
    """Serialize a Tool to the shape a client receives (name/description/schema)."""
    try:
        return tool.model_dump(by_alias=True, exclude_none=True, mode="json")
    except Exception:
        schema = getattr(tool, "inputSchema", None) or getattr(tool, "parameters", {}) or {}
        return {
            "name": tool.name,
            "description": getattr(tool, "description", "") or "",
            "inputSchema": schema,
        }


def _tool_summary_text(wire: dict) -> str:
    """The text an LLM disambiguates on: first description line + param names."""
    desc = (wire.get("description") or "").strip()
    first_line = desc.splitlines()[0] if desc else wire["name"]
    props = (wire.get("inputSchema") or {}).get("properties", {}) or {}
    return f"{wire['name']}: {first_line} | params: {', '.join(props)}"


# --------------------------------------------------------------------------- #
# M1 — static surface cost                                                    #
# --------------------------------------------------------------------------- #
def measure_static_surface() -> dict:
    full_tools = _build_and_list(disable_tier2=False)
    lean_names = {t.name for t in _build_and_list(disable_tier2=True)}

    rows = []
    for t in full_tools:
        wire = _tool_wire_dict(t)
        serialized = json.dumps(wire, ensure_ascii=False)
        desc = wire.get("description") or ""
        props = (wire.get("inputSchema") or {}).get("properties", {}) or {}
        rows.append(
            {
                "name": t.name,
                "tier": "T1" if t.name in lean_names else "T2",
                "chars": len(serialized),
                "tokens": _approx_tokens(serialized),
                "desc_chars": len(desc),
                "n_params": len(props),
                "summary": _tool_summary_text(wire),
            }
        )
    rows.sort(key=lambda r: r["tokens"], reverse=True)

    t1 = [r for r in rows if r["tier"] == "T1"]
    t2 = [r for r in rows if r["tier"] == "T2"]
    skill_chars = SKILL_MD.read_text(encoding="utf-8") if SKILL_MD.exists() else ""
    return {
        "rows": rows,
        "n_total": len(rows),
        "n_t1": len(t1),
        "n_t2": len(t2),
        "tok_total": sum(r["tokens"] for r in rows),
        "tok_t1": sum(r["tokens"] for r in t1),
        "tok_t2": sum(r["tokens"] for r in t2),
        "skill_tokens": _approx_tokens(skill_chars),
        "skill_words": len(skill_chars.split()),
    }


# --------------------------------------------------------------------------- #
# M2 — confusability matrix                                                   #
# --------------------------------------------------------------------------- #
def measure_confusability(rows: list[dict]) -> dict | None:
    import numpy as np

    from kb_mcp import embeddings

    summaries = [r["summary"] for r in rows]
    names = [r["name"] for r in rows]
    vecs = embeddings.embed_texts(summaries)  # (N, 768), L2-normalized
    sims = vecs @ vecs.T  # cosine, since normalized

    pairs = []
    n = len(names)
    for i in range(n):
        for j in range(i + 1, n):
            s = float(sims[i, j])
            if s >= CONFUSE_THRESHOLD:
                pairs.append((s, names[i], names[j]))
    pairs.sort(reverse=True)

    # connected components above threshold = confusable clusters
    parent = {nm: nm for nm in names}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for _s, a, b in pairs:
        parent[find(a)] = find(b)
    clusters: dict[str, list[str]] = {}
    for nm in names:
        clusters.setdefault(find(nm), []).append(nm)
    confusable = [c for c in clusters.values() if len(c) > 1]

    return {"pairs": pairs, "clusters": confusable, "sims": sims, "names": names}


def _cluster_max_sim(conf: dict | None, members: list[str]) -> float | None:
    if not conf:
        return None
    idx = {nm: i for i, nm in enumerate(conf["names"])}
    present = [m for m in members if m in idx]
    best = None
    for a in range(len(present)):
        for b in range(a + 1, len(present)):
            s = float(conf["sims"][idx[present[a]], idx[present[b]]])
            best = s if best is None else max(best, s)
    return best


# --------------------------------------------------------------------------- #
# M3 — real-usage mining                                                      #
# --------------------------------------------------------------------------- #
_LINE_RE = re.compile(r"event=tool_(start|success|error)\b.*?\btool=(\S+)")


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def measure_usage(log_path: Path, all_names: set[str]) -> dict:
    """Mine real usage from every source the server already writes.

    Three sources, in coverage order:
      - `logs/exomem.log`  `event=tool_*` lines (CallTraceMiddleware) — FULL
        coverage of every tool when present; gives call sequences (bigrams).
      - `logs/queries.jsonl` — one record per `find()` call (mode/scope too).
      - `logs/writes.jsonl`  — one record per `add`/`note`/`replace` write.

    The jsonl sources only cover find + the write tools, so their absence of a
    tool (get/edit/audit/Tier 2) means "not logged", NOT "unused". Dead-surface
    inference is only sound under FULL middleware coverage.
    """
    freq: Counter[str] = Counter()
    sequence: list[str] = []
    errors: Counter[str] = Counter()
    sources: list[str] = []

    # 1. middleware traces — full coverage when present
    mw_calls = 0
    if log_path.exists():
        for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
            m = _LINE_RE.search(line)
            if not m:
                continue
            event, tool = m.group(1), m.group(2)
            if tool == "?":
                continue
            if event == "start":
                freq[tool] += 1
                sequence.append(tool)
                mw_calls += 1
            elif event == "error":
                errors[tool] += 1
    if mw_calls:
        sources.append(f"middleware traces ({mw_calls})")

    # 2. queries.jsonl → find usage + mode/scope distribution
    queries = _read_jsonl(log_path.parent / "queries.jsonl")
    find_modes: Counter[str] = Counter()
    find_scopes: Counter[str] = Counter()
    for q in queries:
        freq["find"] += 1
        find_modes[str(q.get("mode", "?"))] += 1
        find_scopes[str(q.get("scope", "?"))] += 1
    if queries:
        sources.append(f"queries.jsonl ({len(queries)} find)")

    # 3. writes.jsonl → write-tool usage
    writes = _read_jsonl(log_path.parent / "writes.jsonl")
    for wr in writes:
        t = str(wr.get("tool", "?"))
        if t != "?":
            freq[t] += 1
    if writes:
        sources.append(f"writes.jsonl ({len(writes)} writes)")

    bigrams: Counter[tuple[str, str]] = Counter()
    for a, b in zip(sequence, sequence[1:]):
        if a != b:
            bigrams[(a, b)] += 1

    full_coverage = mw_calls > 0
    return {
        "available": bool(sources),
        "log_path": str(log_path),
        "sources": sources,
        "full_coverage": full_coverage,
        "n_calls": sum(freq.values()),
        "freq": freq.most_common(),
        "errors": errors.most_common(),
        "find_modes": find_modes.most_common(),
        "find_scopes": find_scopes.most_common(),
        # only meaningful under full middleware coverage
        "never_called": sorted(all_names - set(freq)) if full_coverage else [],
        "bigrams": bigrams.most_common(15),
    }


# --------------------------------------------------------------------------- #
# report                                                                       #
# --------------------------------------------------------------------------- #
def build_report(m1: dict, conf: dict | None, usage: dict) -> str:
    L: list[str] = []
    w = L.append

    w("# exomem tool-surface analysis\n")
    w("_Generated by `scripts/analyze_tool_surface.py` — read-only, offline._\n")

    # --- M1 ---
    w("## M1 — Static surface cost (eager-client load)\n")
    w(f"- **{m1['n_total']} tools** = {m1['n_t1']} Tier 1 + {m1['n_t2']} Tier 2")
    w(f"- Schema tokens (approx): **{m1['tok_total']}** total "
      f"(T1 {m1['tok_t1']} + T2 {m1['tok_t2']})")
    w(f"- Lean surface (drop T2): **{m1['n_t1']} tools / {m1['tok_t1']} tok** "
      f"— saves {m1['tok_t2']} tok/connector")
    w(f"- Two-connector reality (Desktop + Laptop): **{m1['tok_total'] * 2} tok** "
      f"full, {m1['tok_t1'] * 2} tok lean")
    w(f"- `SKILL.md`: **{m1['skill_words']} words ≈ {m1['skill_tokens']} tokens** "
      f"(always loaded, every conversation — dwarfs schemas on deferred clients)\n")
    w("| tool | tier | tokens | desc chars | #params |")
    w("|------|------|-------:|-----------:|--------:|")
    for r in m1["rows"]:
        w(f"| `{r['name']}` | {r['tier']} | {r['tokens']} | {r['desc_chars']} | {r['n_params']} |")
    w("")

    # --- M2 ---
    w("## M2 — Confusability (cosine ≥ %.2f = likely mis-selection)\n" % CONFUSE_THRESHOLD)
    if not conf:
        w("_Skipped (--skip-confusability)._\n")
    else:
        w("Confusable clusters (connected components above threshold):\n")
        if conf["clusters"]:
            for c in sorted(conf["clusters"], key=len, reverse=True):
                w(f"- **{{{', '.join(sorted(c))}}}**")
        else:
            w("- _(none above threshold)_")
        w("\nTop confusable pairs:\n")
        w("| sim | tool A | tool B |")
        w("|----:|--------|--------|")
        for s, a, b in conf["pairs"][:20]:
            w(f"| {s:.3f} | `{a}` | `{b}` |")
        w("")

    # --- M3 ---
    w("## M3 — Real usage\n")
    if not usage["available"]:
        w(f"_No usage logs near `{usage['log_path']}` — mining unavailable "
          f"(fresh box / rotated). Synthesis falls back to M1+M2._\n")
    else:
        w(f"- Sources mined: {', '.join(usage['sources'])}")
        w(f"- {usage['n_calls']} logged tool calls\n")
        if not usage["full_coverage"]:
            w("> ⚠ **Partial coverage.** Only `find` (queries.jsonl) and the write "
              "tools (writes.jsonl) are logged in a mineable form — the "
              "`event=tool_*` middleware traces aren't landing in `exomem.log`. "
              "So a tool's absence below means *not logged*, **not** unused; "
              "dead-surface inference is suppressed.\n")
        w("| tool | logged calls |")
        w("|------|------:|")
        for name, n in usage["freq"]:
            w(f"| `{name}` | {n} |")
        w("")
        if usage["find_modes"]:
            w("**`find` mode mix:** "
              + ", ".join(f"{m}={n}" for m, n in usage["find_modes"])
              + "  ·  **scope mix:** "
              + ", ".join(f"{s}={n}" for s, n in usage["find_scopes"]) + "\n")
        if usage["never_called"]:
            w("**Never called (dead surface — full coverage):** "
              + ", ".join(f"`{n}`" for n in usage["never_called"]) + "\n")
        if usage["bigrams"]:
            w("**Top adjacent sequences (merge/document candidates):**\n")
            w("| A → B | count |")
            w("|-------|------:|")
            for (a, b), n in usage["bigrams"]:
                w(f"| `{a}` → `{b}` | {n} |")
            w("")

    # --- synthesis ---
    w("## Synthesis — consolidation candidates ranked\n")
    tok_by_name = {r["name"]: r["tokens"] for r in m1["rows"]}
    usage_by_name = dict(usage["freq"]) if usage["available"] else {}
    w("| cluster | members | max cosine | combined tokens | logged calls | verdict |")
    w("|---------|---------|-----------:|----------------:|-------------:|---------|")
    for label, members in CANDIDATE_CLUSTERS.items():
        present = [m for m in members if m in tok_by_name]
        if not present:
            continue
        max_sim = _cluster_max_sim(conf, present)
        combined_tok = sum(tok_by_name[m] for m in present)
        calls = sum(usage_by_name.get(m, 0) for m in present)
        has_note = label in CLUSTER_NOTES
        keep = "KEEP separate" in label
        if keep:
            verdict = "keep — discipline"
        elif max_sim is None:
            verdict = "merge (sim n/a)"
        elif max_sim >= CONFUSE_THRESHOLD:
            verdict = f"**MERGE** (confusable, saves ~{combined_tok - max(tok_by_name[m] for m in present)} tok)"
        elif has_note:
            verdict = "**MERGE** (functional — see note)"
        else:
            verdict = "review — modes distinct"
        sim_str = f"{max_sim:.3f}" if max_sim is not None else "n/a"
        w(f"| {label} | {', '.join(f'`{m}`' for m in present)} | {sim_str} "
          f"| {combined_tok} | {calls} | {verdict} |")
    w("")
    for label, note in CLUSTER_NOTES.items():
        if any(m in tok_by_name for m in CANDIDATE_CLUSTERS.get(label, [])):
            w(f"- **{label}:** {note}")
    w("")
    w("> Merging keeps every capability (modes reach via params); it only removes "
      "redundant entry points + the selection-ambiguity tax. Create-family stays "
      "split because type-routing is the governance. `logged calls` reflects only "
      "find + write tools (partial coverage) — don't read 0 as unused.\n")
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# main                                                                         #
# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--skip-confusability", action="store_true",
                    help="skip M2 (avoids loading torch / the embedding model)")
    ap.add_argument("--log", type=Path, default=DEFAULT_LOG,
                    help=f"call log to mine for M3 (default {DEFAULT_LOG})")
    args = ap.parse_args()

    # Windows consoles default to cp1252; the report uses ≈/→/… — keep stdout utf-8.
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:
        pass

    # Populate env from .env once (vault path etc.), then the server's own
    # load_dotenv is neutralized so per-build env toggles stick. Embeddings off
    # for the server build itself (fast) — M2 loads the model separately.
    try:
        from dotenv import load_dotenv

        load_dotenv(override=True)
    except Exception:
        pass
    os.environ["KB_MCP_DISABLE_EMBEDDINGS"] = "1"

    print("M1: introspecting registered tool schemas…")
    m1 = measure_static_surface()
    all_names = {r["name"] for r in m1["rows"]}

    conf = None
    if not args.skip_confusability:
        print("M2: embedding tool summaries (loading bge-base — first run is slow)…")
        os.environ.pop("KB_MCP_DISABLE_EMBEDDINGS", None)  # M2 needs the model
        try:
            conf = measure_confusability(m1["rows"])
        except Exception as e:  # noqa: BLE001
            print(f"  M2 skipped (embedding model unavailable): {e}")

    print(f"M3: mining {args.log}…")
    usage = measure_usage(args.log, all_names)

    report = build_report(m1, conf, usage)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(report, encoding="utf-8")
    print(f"\nReport written to {REPORT_PATH}\n")

    # concise stdout synthesis
    print(f"  Tools: {m1['n_total']} ({m1['n_t1']} T1 + {m1['n_t2']} T2)")
    print(f"  Schema tokens: {m1['tok_total']} / connector  "
          f"({m1['tok_total'] * 2} across two connectors)")
    print(f"  SKILL.md: {m1['skill_words']} words ≈ {m1['skill_tokens']} tokens")
    if conf:
        print(f"  Confusable clusters: "
              + " ; ".join("{" + ", ".join(sorted(c)) + "}" for c in conf["clusters"]))
    if usage["available"]:
        cov = "full" if usage["full_coverage"] else "partial (find+writes only)"
        print(f"  Usage: {usage['n_calls']} logged calls [{cov}] from "
              f"{', '.join(usage['sources'])}")
    else:
        print("  Usage: no usage logs found (fresh box) — see report.")


if __name__ == "__main__":
    main()
