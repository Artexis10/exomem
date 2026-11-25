"""Local scale instrument for the paged Source reclassification preview (not committed).

EXOMEM_PREVIEW_GOVERNED=1 puts every referrer under an owner-only governed scope.
EXOMEM_PREVIEW_BUILD_BUDGET=<s> overrides the build's request budget (uncapped baseline).
EXOMEM_PROFILE=1 prints a cProfile of the build thread only.
"""
import asyncio
import json
import os
import threading
from statistics import median
from time import perf_counter

from _public_call import public_call
from fastmcp import Client
from record_fixtures import copy_dataset_fixture
from test_agent_tool_surface import _call
from test_reclassify_source import _capture

from exomem import file_watcher, freshness, lexstore, reclassification_preview, request_budget, server
from exomem import vault as vault_module

REFERRERS = int(os.environ.get("EXOMEM_PREVIEW_N", "10000"))
GOVERNED = os.environ.get("EXOMEM_PREVIEW_GOVERNED") == "1"
DEADLINE_S = float(os.environ.get("EXOMEM_PREVIEW_DEADLINE", "900"))


def _emit(**values):
    print(json.dumps(values), flush=True)


def test_served_preview_scale(vault, source_schema, monkeypatch):
    monkeypatch.setattr(server, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("EXOMEM_MODE", "quiet")
    monkeypatch.setenv("EXOMEM_DISABLE_GRAPH_INDEX", "1")
    heavy = _capture(vault, source_schema, title="Field measurements").path
    light = _capture(vault, source_schema, title="Other samples").path
    copy_dataset_fixture(vault)
    notes = vault / "Knowledge Base" / "Notes" / "Volume"
    notes.mkdir(parents=True)
    sources = (vault / heavy).parent
    for number in range(9_999):
        (sources / f"other-source-{number:05d}.md").write_text(f"# Other source {number}\n", encoding="utf-8")
    for number in range(10_001):
        link = heavy if number < REFERRERS else light if number == 10_000 else None
        body = f"See [[{link}]] and `{link}`." if link else "Unrelated generated observation."
        (notes / f"sample-{number:05d}.md").write_text(f"# Sample {number}\n\n{body}\n", encoding="utf-8")
    if GOVERNED:
        governance = vault / "Knowledge Base" / "_Governance"
        (governance / "scopes").mkdir(parents=True, exist_ok=True)
        (governance / "rules").mkdir(parents=True, exist_ok=True)
        (governance / "scopes" / "volume.yaml").write_text(
            'governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\npaths: ["Notes/Volume/**"]\n', encoding="utf-8",
        )
        (governance / "rules" / "volume.yaml").write_text(
            'governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FB0\n'
            'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FAV"]\naudience: principal:report-reader\nceiling: 0\n',
            encoding="utf-8",
        )
    started = perf_counter()
    for scope, root in (("vault", vault), ("kb", vault / "Knowledge Base")):
        freshness.seed(vault, scope, (
            (str(path), freshness.stat_signature(path)) for path in vault_module.walk_vault_md(root)
        ))
    assert vault_module.prime_inbound_index(vault, eager=True)
    lexstore.ensure_fresh(vault)
    _emit(governed=GOVERNED, referrers=REFERRERS, setup_s=round(perf_counter() - started, 3))

    if os.environ.get("EXOMEM_PREVIEW_BUILD_BUDGET"):
        override = float(os.environ["EXOMEM_PREVIEW_BUILD_BUDGET"])
        real_build = reclassification_preview._build

        def uncapped(*args, **kwargs):
            with monkeypatch.context() as inner:
                inner.setattr(request_budget, "budget_seconds", lambda env=None: override)
                return real_build(*args, **kwargs)
        # The build runs on a worker thread; only the build reads the override.
        monkeypatch.setattr(reclassification_preview, "_build", uncapped)

    build_events = []
    inner_build = reclassification_preview._build

    def timed(*args, **kwargs):
        t = perf_counter()
        profiler = None
        if os.environ.get("EXOMEM_PROFILE"):
            import cProfile
            profiler = cProfile.Profile()
        outcome = {"raised": None}
        root = args[0]
        key_vault, key_kb = freshness._key(root, "vault"), freshness._key(root, "kb")
        start_generation = freshness.preview_reference_generation(root)
        try:
            if profiler is not None:
                return profiler.runcall(inner_build, *args, **kwargs)
            return inner_build(*args, **kwargs)
        except BaseException as error:
            outcome = {"raised": type(error).__name__, "code": getattr(error, "code", None)}
            raise
        finally:
            end_generation = freshness.preview_reference_generation(root)
            moved = {}
            if start_generation and end_generation:
                with freshness._lock:
                    for label, key, history, index in (("vault", key_vault, freshness._history, 1),
                                                       ("kb", key_kb, freshness._recall_history, 2)):
                        since = start_generation[index] or 0
                        paths = set()
                        for entry in history.get(key, []):
                            if entry[1] > since:
                                paths.update(entry[2])
                        moved[label] = sorted(paths)[:20] + ([f"... {len(paths)} total"] if len(paths) > 20 else [])
            build_events.append({"build_s": round(perf_counter() - t, 3), **outcome,
                                 "generation_start": start_generation, "generation_end": end_generation,
                                 "moved_paths": moved})
            _emit(build=build_events[-1])
            if profiler is not None:
                import io
                import pstats
                buffer = io.StringIO()
                pstats.Stats(profiler, stream=buffer).sort_stats("cumulative").print_stats("exomem", 40)
                print("\nPROFILE\n" + buffer.getvalue(), flush=True)
    monkeypatch.setattr(reclassification_preview, "_build", timed)

    stop = threading.Event()
    monkeypatch.setattr(file_watcher, "observing_inbound_recovery_stop", lambda _: stop)
    mcp = server.build_server(require_auth=False)
    mcp._exomem_local_runtime_activation.fallback_seconds = 180

    async def exercise():
        async with Client(mcp, timeout=65) as client:
            async def call(tool, **kwargs):
                return await _call(client, *public_call(tool, kwargs))

            async def preview(source, **kwargs):
                return await call("manage_memory_file", operation="propose-reclassification",
                                  path=source, source_kind="dataset-export", **kwargs)

            small = await preview(light)
            assert small.get("preview_digest"), small
            started = perf_counter()
            response = await preview(heavy)
            _emit(handle_s=round(perf_counter() - started, 3), state=response.get("state"),
                  error=response.get("error", {}).get("code"))
            assert response.get("state") == "running", response
            run_id = response["run_id"]
            probes: list[float] = []
            probing = True

            async def records_probe():
                while probing:
                    t = perf_counter()
                    try:
                        await call("query_records", collection="Meter readings", limit=1)
                        probes.append(round(perf_counter() - t, 3))
                    except Exception:
                        probes.append(-1)
                    await asyncio.sleep(1)
            probe_task = asyncio.create_task(records_probe())
            restarts, polls, summary = 0, [], {}
            while perf_counter() - started < DEADLINE_S:
                t = perf_counter()
                summary = await preview(heavy, preview_run_id=run_id)
                polls.append(perf_counter() - t)
                state, code = summary.get("state"), summary.get("error_code") or summary.get("error", {}).get("code")
                if state == "running" or (code == "RECLASSIFICATION_PREVIEW_WARMING" and state is None):
                    await asyncio.sleep(.5)
                    continue
                if state == "failed" and code == "RECLASSIFICATION_PREVIEW_WARMING":
                    restarts += 1
                    if restarts > int(os.environ.get("EXOMEM_PREVIEW_MAX_RESTARTS", "1000")):
                        break
                    rerun = await preview(heavy)
                    run_id = rerun.get("run_id", run_id)
                    continue
                break
            probing = False
            probe_task.cancel()
            try:
                await probe_task
            except asyncio.CancelledError:
                pass
            job_s = perf_counter() - started
            _emit(job_s=round(job_s, 3), job_state=summary.get("state"),
                  job_error=summary.get("error_code") or summary.get("error", {}).get("code"), restarts=restarts,
                  builds=build_events, max_poll_s=round(max(polls), 3) if polls else None,
                  records_probes=len(probes), records_median_s=median(probes) if probes else None,
                  records_max_s=max(probes) if probes else None)
            assert summary.get("state") == "completed", summary
            if os.environ.get("EXOMEM_PREVIEW_LOOKUPS"):
                from exomem import path_mentions

                def lookups():
                    timings = {}
                    for _ in range(3):
                        t = perf_counter()
                        inbound = vault_module.cached_inbound_wikilinks(vault, heavy)
                        timings.setdefault("inbound_s", []).append(round(perf_counter() - t, 4))
                        t = perf_counter()
                        found = path_mentions.find_pages(vault, [heavy], current_published=True)
                        timings.setdefault("mentions_s", []).append(round(perf_counter() - t, 4))
                    return timings, None if inbound is None else len(inbound), found.complete, len(found.pages)
                _emit(lookups=await asyncio.to_thread(lookups))
            cursor = summary["next_cursor"]
            seen, pages, sizes = set(), [], []
            scans = {"notes": 0, "sources": 0}
            scandir = os.scandir

            def count_listing(path):
                target = os.readlink(f"/proc/self/fd/{path}") if isinstance(path, int) else os.fspath(path)
                for label, directory in (("notes", notes), ("sources", sources)):
                    if target == os.fspath(directory):
                        scans[label] += 1
                return scandir(path)

            warmings = 0
            with monkeypatch.context() as measured:
                measured.setattr(os, "scandir", count_listing)
                while cursor is not None:
                    t = perf_counter()
                    page = await preview(heavy, preview_run_id=run_id, cursor=cursor)
                    if page.get("error", {}).get("code") == "RECLASSIFICATION_PREVIEW_WARMING":
                        warmings += 1
                        assert warmings < 50, page
                        await asyncio.sleep(.1)
                        continue
                    assert "error" not in page, page
                    pages.append(perf_counter() - t)
                    sizes.append(len(json.dumps(page).encode()))
                    seen.update((entry["kind"], entry["path"]) for entry in page.get("entries") or ())
                    cursor = page.get("next_cursor")
            expected = {("rewritten", f"Knowledge Base/Notes/Volume/sample-{n:05d}.md") for n in range(REFERRERS)}
            _emit(seen=len(seen), missing=len(expected - seen), pages=len(pages),
                  median_page_s=round(median(pages), 3), max_page_s=round(max(pages), 3),
                  max_bytes=max(sizes), warmings=warmings, referrer_directory_scans=scans)
            assert expected <= seen

    asyncio.run(exercise())
