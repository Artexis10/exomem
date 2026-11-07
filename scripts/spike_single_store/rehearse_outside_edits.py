"""Rehearse editor-style outside edits against a migrated store on a vault copy.

Usage: rehearse_outside_edits.py <run_root> <out.json> [--seed 20261107]
Needs build_base and `arm_single migrate` on the run root. Run it through
spike_env.run_child so the Exomem environment points at the copy.

Every case edits real pages the way a person in an editor would, then runs the
store's reconcile sweep (fast, then full-hash) and checks the outcome D5 of the
design promises: clean edits import, edits that race a queued store write are
held, deletes are held, renames pair by hash, and an edit that keeps size and
mtime is caught only by the full-hash sweep.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import time
from pathlib import Path

import single_store
import spike_env

MARKER = "\n- [observation] Rehearsal edit {tok} #rehearsal\n"


def body_start(text: str) -> int:
    """Offset just after the frontmatter block, or 0 when there is none."""
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            return end + 5
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_root", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--seed", type=int, default=20261107)
    args = ap.parse_args()
    paths = spike_env.layout(args.run_root)
    vault = paths["vault"]
    state_dir = next(paths["state"].glob("vault-*"))
    store = single_store.Store(vault, single_store.store_path(state_dir))
    rng = random.Random(args.seed)
    report: dict = {"sweeps": {}}

    def sweep(name: str, full: bool) -> dict:
        started = time.perf_counter()
        result = store.reconcile(full_hash=full)
        result["s"] = round(time.perf_counter() - started, 2)
        report["sweeps"][name] = result
        return result

    sweep("fast_before_edits", False)
    sweep("full_before_edits", True)

    kb = vault / "Knowledge Base"
    notes = sorted(p for p in (kb / "Notes").rglob("*.md") if not p.name.startswith(("_", "index")))
    entities = sorted(p for p in (kb / "Entities").rglob("*.md") if not p.name.startswith(("_", "index")))
    picked = rng.sample(notes, 24) + rng.sample(entities, 4)
    cases: list[dict] = []
    n = 0

    def rel(p: Path) -> str:
        return p.relative_to(vault).as_posix()

    def token() -> str:
        nonlocal n
        n += 1
        return f"zqreh{n:04d}{rng.randrange(16**6):06x}"

    kinds = ["append", "inline", "property", "crlf"]
    for i, p in enumerate(picked[:16]):
        kind, tok = kinds[i % 4], token()
        text = p.read_text(encoding="utf-8")
        if kind == "append":
            new = text.rstrip("\n") + "\n" + MARKER.format(tok=tok)
        elif kind == "inline":
            at = body_start(text)
            line_end = text.find("\n", at + 1)
            line_end = len(text) if line_end == -1 else line_end
            new = text[:line_end] + f" {tok}" + text[line_end:]
        elif kind == "property":
            new = text[:4] + f"rehearsal: {tok}\n" + text[4:] if text.startswith("---\n") else text + MARKER.format(tok=tok)
        else:
            new = (text.rstrip("\n") + "\n" + MARKER.format(tok=tok)).replace("\r\n", "\n").replace("\n", "\r\n")
        p.write_bytes(new.encode("utf-8"))
        cases.append({"kind": kind, "path": rel(p), "token": tok, "expect": "imported"})

    for p in picked[16:18]:  # touched, bytes unchanged
        os.utime(p)
        cases.append({"kind": "touch", "path": rel(p), "expect": "unchanged"})

    for p in picked[18:20]:  # an edit that keeps size and mtime
        st = p.stat()
        data = bytearray(p.read_bytes())
        at = body_start(data.decode("utf-8", "replace"))
        idx = next(k for k in range(at, len(data)) if data[k] in b"aeiou")
        data[idx] = ord("e") if data[idx] != ord("e") else ord("a")
        p.write_bytes(bytes(data))
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
        cases.append({"kind": "same_size_mtime", "path": rel(p), "expect": "full_sweep_only"})

    for p in picked[20:23]:  # a store write queued, then the person edits the file
        r = rel(p)
        text = p.read_text(encoding="utf-8")
        store_tok, person_tok = token(), token()
        store.put(r, (text.rstrip("\n") + "\n" + MARKER.format(tok=store_tok)).encode("utf-8"),
                  op="store-write")
        p.write_bytes((text.rstrip("\n") + "\n" + MARKER.format(tok=person_tok)).encode("utf-8"))
        cases.append({"kind": "concurrent", "path": r, "token": person_tok, "store_token": store_tok,
                      "expect": "held"})

    for p in picked[23:25]:
        r = rel(p)
        p.unlink()
        cases.append({"kind": "delete", "path": r, "expect": "held_deletion"})

    for p in picked[25:27]:
        r = rel(p)
        target = p.with_name(f"renamed-{p.name}")
        p.rename(target)
        cases.append({"kind": "rename", "path": r, "new_path": rel(target), "expect": "moved"})

    new_page = kb / "Notes" / "Insights" / "rehearsal-outside-create.md"
    tok = token()
    new_page.write_text(f"# Rehearsal outside create\n\n## Observations\n{MARKER.format(tok=tok)}",
                        encoding="utf-8")
    cases.append({"kind": "create", "path": rel(new_page), "token": tok, "expect": "imported"})

    fast = sweep("fast_after_edits", False)
    same_after_fast = {c["path"]: _store_bytes(store, c["path"]) == (vault / c["path"]).read_bytes()
                       for c in cases if c["kind"] == "same_size_mtime"}
    sweep("full_after_edits", True)

    held = {}
    for path, file_hash, body in store.conn.execute("SELECT path, file_hash, body FROM store_held"):
        held[path] = {"deleted": file_hash == "deleted", "body": body}
    for c in cases:
        path = c["path"]
        disk = (vault / path).read_bytes() if (vault / path).exists() else None
        stored = _store_bytes(store, path)
        if c["expect"] == "imported":
            # A frontmatter property is not lexical body text; check its bytes only.
            searchable = c["kind"] != "property"
            c["ok"] = stored == disk and (not searchable or store.lexical(path, c["token"]))
            c["graph_nodes"] = store.conn.execute(
                "SELECT count(*) FROM graph_nodes WHERE path = ?", (path,)).fetchone()[0]
        elif c["expect"] == "unchanged":
            c["ok"] = stored == disk and path not in held
        elif c["expect"] == "full_sweep_only":
            c["fast_sweep_caught"] = same_after_fast[path]
            c["ok"] = (not same_after_fast[path]) and stored == disk
        elif c["expect"] == "held":
            c["ok"] = (path in held and held[path]["body"] == disk
                       and store.lexical(path, c["store_token"]) and stored != disk)
        elif c["expect"] == "held_deletion":
            c["ok"] = path in held and held[path]["deleted"] and stored is not None
        elif c["expect"] == "moved":
            c["ok"] = (stored is None and _store_bytes(store, c["new_path"])
                       == (vault / c["new_path"]).read_bytes())
    report["cases"] = [{k: v for k, v in c.items() if k not in ("path", "new_path", "token", "store_token")}
                       for c in cases]
    report["ok"] = sum(1 for c in cases if c.get("ok"))
    report["of"] = len(cases)
    report["fast_after_edits_hashed"] = fast["hashed"]
    report["stored_files"] = store.conn.execute("SELECT count(*) FROM store_pages").fetchone()[0]
    args.out.write_text(json.dumps(report, indent=1))
    print(json.dumps({"ok": report["ok"], "of": report["of"], "sweeps": report["sweeps"]}), flush=True)
    os._exit(0)


def _store_bytes(store, path: str) -> bytes | None:
    row = store.conn.execute("SELECT body FROM store_pages WHERE path = ?", (path,)).fetchone()
    return row[0] if row else None


if __name__ == "__main__":
    main()
