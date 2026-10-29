"""The reviewed development references, materialized through existing product leaves.

These frozen source bundles are tuning evidence. They never enter the legacy
held-out generator. Keys read only their invented input facts, not seed responses.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from .templates import FileKey, Key, ManifestKey, Op, SeedStep, Template


def source_template(params: dict) -> Template:
    return Template(
        "source-derived", params["kind"], params["fixture_id"],
        lambda rng: params, source_steps, lambda values: values["prompt"], source_key,
        summary="Source-derived cross-family development workflow",
    )


def source_steps(params: dict) -> list[SeedStep]:
    if params.get("blocked"):
        raise ValueError(f"fixture admission blocked: {params['blocked']}")
    fixture = params["fixture"]
    family = fixture["seed_kind"]
    # Seed kinds are the closed fixture mechanisms this source-reference format implements.
    if family == "compiled-note":
        from exomem.observe_memory import _render_unit

        frontmatter = fixture["frontmatter"]
        content = fixture["body"]
        units = fixture.get("units", [])
        if units:
            first = units[0]
            # Reuse the product's pure rich-unit renderer to satisfy the live-page creation contract.
            content += "\n\n" + _render_unit(
                kind=first["kind"], category=first["category"], content=first["content"],
                tags=tuple(first["tags"]), context=first["context"],
                relations=tuple((relation["kind"], relation["target"]) for relation in first["relations"]),
                anchor=first["id"], verdict=first["verdict"], check_by=first["check_by"],
            )
        steps = [SeedStep("create_note", "create", {
            "note_type": frontmatter["type"], "title": frontmatter["title"],
            "status": frontmatter["status"],
            "slug": PurePosixPath(fixture["path"]).stem.removeprefix(frontmatter["started"][:7] + "-"),
            "content": content,
            **{name: frontmatter[name] for name in ("domain", "started", "duration")},
        })]
        for unit in units[1:]:
            steps.append(SeedStep("observe_memory", "add", {"path": fixture["path"], **unit}))
        return steps
    if family == "preferences":
        return [
            SeedStep("configure_memory", "set", {"prominence": fixture["stored"]}),
            *(SeedStep("configure_memory", "set", {"context": context, "prominence": level})
              for context, level in fixture["contexts"].items()),
        ]
    if family == "entity":
        frontmatter = fixture["frontmatter"]
        return [SeedStep("connect_memory", "create-entity", {
            "entity_type": frontmatter["entity_type"], "name": frontmatter["title"],
            "slug": PurePosixPath(fixture["path"]).stem,
            "summary": fixture["summary"],
        })]
    if family == "registry":
        return [SeedStep("schema_memory", "save", {
            "subject": fixture["subject"],
            "proposal": {"upsert": fixture["overlay"][fixture["subject"]]},
        })]
    if family in ("empty-media-ledger", "media-ledger", "empty-records"):
        return []
    if family == "adoption":
        return [
            SeedStep("adoption_studio", "start", {"path": fixture["source_root"]},
                     originals=fixture["originals"]),
            SeedStep("adoption_studio", "select", {"include": fixture["selected_paths"]},
                     match={"source_root": fixture["source_root"]}),
        ]
    raise ValueError(f"fixture admission has no source recipe: {family}")


def seed_owned_fixture_state(params: dict, vault: Path) -> None:
    """Materialize retained failed media references through their canonical owner."""
    fixture = params["fixture"]
    if fixture["seed_kind"] != "media-ledger":
        return
    from exomem.media_jobs import FAILED, MediaJob, MediaJobStore, status

    owner = MediaJobStore(vault)
    for job in fixture["jobs"]:
        paths = [PurePosixPath(job[name]) for name in ("binary_path", "sidecar_path")]
        if (job["state"] != FAILED or any(path.is_absolute() or ".." in path.parts for path in paths)
                or any(type(job[name]) is not bool for name in ("do_ocr", "do_clip", "do_reembed"))):
            raise ValueError("media fixtures require relative retained references and failed jobs")
        binary, sidecar = (vault / path for path in paths)
        if binary.exists() or sidecar.exists():
            raise ValueError("retained media references must not materialize binary or sidecar bytes")
        ident = owner.enqueue(MediaJob(binary_path=binary, sidecar_path=sidecar,
                                      media_type=job["media_type"], do_ocr=job["do_ocr"],
                                      do_clip=job["do_clip"], do_reembed=job["do_reembed"]))
        if not owner.mark(ident, FAILED, error=job["error"]):
            raise ValueError("the media owner did not persist the declared failed job")
    diagnostic = status(vault, diagnostic_snapshot=True)
    if (diagnostic["counts"].get(FAILED) != len(fixture["jobs"])
            or diagnostic["worker_active"] or owner.needs_worker()):
        raise ValueError("the media fixture must contain valid retained jobs without runnable work")


def source_key(params: dict) -> Key:
    expected = params["expected"]
    state = expected["canonical_end_state"]
    operations = tuple(
        Op(operation["command"], operation["value"], constraints=operation.get("constraints", {}))
        for operation in expected["target_operations"]
    )
    answer = state.get("answer", {})
    files = ()
    if "units" in state:
        units = state["units"]
        files = (FileKey(units["path"], units=tuple(units["exact_units"])),)
    if "file" in state:
        file = state["file"]
        body = params["fixture"]["body"].rstrip() + "\n\n" + params["fixture"]["append"] + "\n"
        files = (FileKey(file["path"], body=body),)
    preview = state.get("required_preview") or ({"delta": state["preview_delta"]} if "preview_delta" in state else None)
    if preview is not None and "exact_unit_anchor" in preview:
        unit = next(unit for unit in params["fixture"]["units"] if unit["id"] == preview["exact_unit_anchor"])
        preview = {**preview, "unit": {**unit, "content": preview["proposed_content"]}}
    adoption = state.get("adoption")
    if isinstance(adoption, dict):
        from exomem.context_refs import source_ref

        items = []
        for item in adoption["plan"]["items"]:
            target = item["target_path"].format(case_date=params["fixture"]["case_date"])
            items.append({**item, "target_path": target, "target_ref": source_ref(target)})
        adoption = {**adoption, "originals": params["fixture"]["originals"], "plan": {
            **adoption["plan"], "date": params["fixture"]["case_date"], "items": items,
            # The planner's note counters derive from independent expected items, never actor output.
            "notes": {
                "to_create": sum(1 for item in items if item.get("note")),
                "existing": sum(1 for item in items if item.get("existing_note")),
                "previous": sum(1 for item in items if item.get("previous_note")),
            },
        }}
    return Key(
        kind=params["kind"], target_ops=operations, answer=answer.get("value"),
        manifests=tuple(ManifestKey(
            manifest=item["manifest"], profile=item["profile"], title=item["title"],
            natural_key=tuple(item["natural_key"]),
            fields={name: tuple(types) for name, types in item["fields"].items()},
            required=tuple(item["required"]),
        ) for item in state.get("manifests", [])),
        answer_mode=answer.get("mode"), files=files, preference=state.get("preferences")
        if isinstance(state.get("preferences"), dict) else None,
        question=state.get("review_questions") if isinstance(state.get("review_questions"), dict) else None,
        registry={**state["registry"], "reason": state["registry_history"]["reason"]}
        if "registry" in state else None,
        preview=preview,
        adoption=adoption if isinstance(adoption, dict) else None,
    )


def admit_fixture(params: dict, state: dict) -> None:
    """Check source-authored facts before admitting a generated fixture to either arm."""
    from .vault import markdown_facts

    if state.get("unknown"):
        raise ValueError(f"fixture canonical read is incomplete: {state['unknown']}")
    fixture = params["fixture"]
    kind = fixture["seed_kind"]
    if kind in ("compiled-note", "entity"):
        file = state["files"].get(fixture["path"], {})
        expected = fixture["frontmatter"]
        # The writer mints identities; all other declared frontmatter facts are independent fixture inputs.
        fields = tuple(name for name in expected if name != "exomem_id")
        if any(file.get("frontmatter", {}).get(name) != expected[name] for name in fields):
            raise ValueError("the product did not materialize the declared page facts")
        if file["frontmatter"].get("status") != "active":
            raise ValueError("the seeded page is not live and discoverable")
        if "units" in fixture:
            if file.get("units") != fixture["units"]:
                raise ValueError("the seeded units differ from the independently authored facts")
            if file.get("remainder") != markdown_facts(fixture["body"]):
                raise ValueError("the seeded page remainder differs from the declared prose")
        elif file.get("body") != markdown_facts(fixture["body"]):
            raise ValueError("the seeded Markdown differs from the independently authored body")
    elif kind == "empty-records":
        if state["collections"]:
            raise ValueError("the Records creation fixture must start without collections")
    elif kind == "preferences":
        if any(state["preferences"].get(name) != fixture[name] for name in ("stored", "contexts")):
            raise ValueError("the seeded preferences differ from the declared owner choices")
    elif kind == "registry":
        owner = state["registry_paths"][fixture["subject"]]
        overlay = state["files"][owner["overlay"]]["value"]
        for name, entry in fixture["overlay"][fixture["subject"]].items():
            if any(overlay.get(fixture["subject"], {}).get(name, {}).get(field) != value for field, value in entry.items()):
                raise ValueError("the seeded registry differs from the declared entry")
    elif kind == "media-ledger":
        tables = state["media"].get("tables", {})
        rows = tables.get("jobs", [])
        fields = ("binary_rel", "sidecar_rel", "media_type", "do_ocr", "do_clip", "do_reembed",
                  "state", "last_error", "attempts", "claim_revision", "ocr_generation",
                  "blocked_reason", "blocked_context")
        expected = [{"binary_rel": job["binary_path"], "sidecar_rel": job["sidecar_path"],
                     **{name: job[name] for name in ("media_type", "do_ocr", "do_clip", "do_reembed", "state")},
                     "last_error": job["error"], "attempts": 0, "claim_revision": 0, "ocr_generation": 0,
                     "blocked_reason": None, "blocked_context": None} for job in fixture["jobs"]]
        actual = [{name: row.get(name) for name in fields} for row in rows]
        # These table names are the media owner's persisted job/result protocol, not inferred categories.
        if (state["media"].get("status") != "present" or tables.get("media_job_results") != []
                or sorted(actual, key=lambda row: row["binary_rel"]) != sorted(expected, key=lambda row: row["binary_rel"])):
            raise ValueError("the retained media ledger differs from the independently declared jobs")
    elif kind == "adoption":
        import datetime as dt
        import hashlib

        from .vault import adoption_runs

        runs = adoption_runs(state)
        if len(runs) != 1:
            raise ValueError("the adoption fixture must contain exactly one run")
        run = next(iter(runs.values()))
        if (run.get("source_root") != fixture["source_root"]
                or run.get("collection") != {"label": fixture["source_root"], "root": fixture["source_root"]}
                or run.get("phase") != "selecting" or run.get("plan") is not None
                or run.get("outcomes") != {}
                or run.get("selection", {}).get("paths") != fixture["selected_paths"]
                or run.get("selection", {}).get("rules") != {
                    "include": fixture["selected_paths"], "exclude": [], "overrides": [], "include_junk": False,
                }
                or run.get("selection", {}).get("classification") != {}
                or run.get("selection", {}).get("rejected_counts") != {}):
            raise ValueError("the seeded adoption run differs from the declared selection")
        if fixture["case_date"] != dt.date.today().isoformat():
            raise ValueError("capture the source-default local date before freezing the adoption pair")
        for path, body in fixture["originals"].items():
            if state["evidence"]["files"].get(path, {}).get("sha256") != hashlib.sha256(body.encode()).hexdigest():
                raise ValueError("the seeded adoption original differs from the declared bytes")
        if any(fact.get("frontmatter", {}).get("imported_from") in fixture["originals"]
               for fact in state["files"].values()):
            raise ValueError("the adoption fixture already contains an import")
    if state["review"].get("vocabulary", {}).get("items"):
        raise ValueError("the seed unexpectedly contains vocabulary questions")
