"""Corpus generation: templates → rendered artifacts + jsonl + hashed manifest.

Fails loudly (``GenerationError``) on: unjustified status spans, ontology
vocabulary leaking into corpus text, non-unique basenames, impure canaries,
or an existing non-empty output directory without ``force``.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from membench import GENERATOR_VERSION, families, oracle
from membench.artifacts import render_artifact, renderer_versions
from membench.compile_plan import derive_compile_plan
from membench.ids import sentinel as sentinel_token
from membench.ids import slugify
from membench.schema import (
    ArtifactEntry,
    ArtifactKind,
    ClaimRecord,
    CorpusManifest,
    EntityRecord,
    EventRecord,
    ExpectedRecord,
    Persona,
    PolicySet,
    QueryRecord,
    ScheduleOp,
    SourceRecord,
    TemplateInfo,
    dump_jsonl,
)
from membench.templates import registry
from membench.templates.base import (
    BuildContext,
    GenerationError,
    ScenarioGraph,
    Template,
    finalize_expected,
)

# Ontology lint: corpus text must not mirror any contender's internal
# vocabulary (would gift lexical matches). Extend deliberately; checked
# case-insensitively over artifact text and titles.
BANNED_VOCABULARY: tuple[str, ...] = (
    "exomem",
    "basic memory",
    "basic-memory",
    "graybox",
    "gray box",
    "mem0",
    "ask_memory",
    "capture_source",
    "replace_memory",
    "knowledge base",
    "semantic unit",
    "supersession lineage",
)

_TEXT_KINDS = {
    ArtifactKind.MARKDOWN,
    ArtifactKind.CSV,
    ArtifactKind.TRANSCRIPT,
    ArtifactKind.PDF_UNAVAILABLE,
    ArtifactKind.PNG_UNAVAILABLE,
}


def _merge_policies(graphs: list[ScenarioGraph]) -> PolicySet:
    audiences: dict[str, None] = {}
    personas: dict[str, Persona] = {}
    rules = []
    tombstones = []
    for graph in graphs:
        for audience in graph.policy.audiences:
            audiences.setdefault(audience)
        for persona in graph.policy.personas:
            existing = personas.get(persona.persona_id)
            if existing is not None and existing != persona:
                raise GenerationError(f"conflicting persona definitions: {persona.persona_id}")
            personas[persona.persona_id] = persona
        rules.extend(graph.policy.rules)
        tombstones.extend(graph.policy.tombstones)
    if "owner" not in personas:
        personas["owner"] = Persona(persona_id="owner", audiences=list(audiences) or ["owner"])
    return PolicySet(
        audiences=list(audiences) or ["owner"],
        personas=list(personas.values()),
        rules=rules,
        tombstones=tombstones,
    )


def _lint_ingestion_order(
    schedule: list[ScheduleOp], sources_by_id: dict[str, SourceRecord]
) -> list[str]:
    """Captured intra-day instants must agree with the order sources are fed in.

    The schedule is the order a contender receives the corpus, and it is the
    *only* way an intra-day ordering reaches one: the sources themselves carry
    no clock, so a memory system can only know that A preceded B by having
    stamped its own knowledge time when each arrived. If a template declares
    instants that contradict the ingestion order, the corpus asks contenders to
    reproduce an order they were never shown — an unwinnable query dressed as a
    capability test. Refuse at generation instead.

    Sources with no captured instant constrain nothing: an unknown instant
    ranges over its whole week, so it is indeterminate against everything in
    that week and cannot be out of order (the same four-valued rule
    :func:`membench.oracle.compare_recorded` applies everywhere).
    """

    errors: list[str] = []
    previous: SourceRecord | None = None
    for op in schedule:
        source = sources_by_id.get(op.source_id or "")
        if source is None or source.recorded_offset_s is None:
            continue
        if previous is not None and oracle.compare_recorded(source, previous) is oracle.Order.BEFORE:
            errors.append(
                f"{source.source_id}: ingested after {previous.source_id} but records an "
                f"earlier instant (week {source.recorded_week} offset "
                f"{source.recorded_offset_s}s vs week {previous.recorded_week} offset "
                f"{previous.recorded_offset_s}s)"
            )
        previous = source
    return errors


def _lint_vocabulary(text: str, where: str, errors: list[str]) -> None:
    lowered = text.lower()
    for token in BANNED_VOCABULARY:
        if token in lowered:
            errors.append(f"{where}: banned vocabulary {token!r}")


def generate_corpus(
    master_seed: int,
    out_dir: Path,
    *,
    template_ids: list[str] | None = None,
    templates: dict[str, Template] | None = None,
    force: bool = False,
) -> CorpusManifest:
    available = templates if templates is not None else registry()
    selected = (
        {tid: available[tid] for tid in template_ids} if template_ids is not None else available
    )
    if not selected:
        raise GenerationError("no templates selected")

    out_dir = Path(out_dir)
    if out_dir.exists() and any(out_dir.iterdir()) and not force:
        raise GenerationError(f"output directory {out_dir} exists and is not empty")

    graphs: list[ScenarioGraph] = []
    template_infos: list[TemplateInfo] = []
    entities: list[EntityRecord] = []
    sources: list[SourceRecord] = []
    events: list[EventRecord] = []
    claims: list[ClaimRecord] = []
    queries: list[QueryRecord] = []
    expected: list[ExpectedRecord] = []
    schedule: list[ScheduleOp] = []
    contents = {}

    for template_id in sorted(selected):
        template = selected[template_id]
        family_error = families.family_registration_error(
            template.template_id, template.family
        )
        if family_error:
            raise GenerationError(family_error)
        template_infos.append(
            TemplateInfo(
                template_id=template.template_id,
                family=template.family,
                summary=template.summary,
                variants=template.variants,
            )
        )
        for variant in range(template.variants):
            ctx = BuildContext(template.template_id, variant, master_seed)
            template.build(ctx)
            graph = ctx.graph
            graphs.append(graph)
            entities.extend(graph.entities)
            sources.extend(graph.sources)
            events.extend(graph.events)
            claims.extend(graph.claims)
            queries.extend(graph.queries)
            schedule.extend(graph.schedule)
            contents.update(graph.contents)
            expected.extend(finalize_expected(graph))

    lint_errors = oracle.lint_corpus(claims, sources)
    if lint_errors:
        raise GenerationError("span lint failed:\n" + "\n".join(lint_errors))

    ordered_schedule = sorted(schedule, key=lambda op: (op.week, schedule.index(op)))
    schedule = [op.model_copy(update={"seq": index}) for index, op in enumerate(ordered_schedule)]

    order_errors = _lint_ingestion_order(schedule, {s.source_id: s for s in sources})
    if order_errors:
        raise GenerationError("ingestion order lint failed:\n" + "\n".join(order_errors))

    sources_dir = out_dir / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)
    artifact_entries: list[ArtifactEntry] = []
    degradations: list[str] = []
    vocabulary_errors: list[str] = []
    basenames: set[str] = set()
    text_by_source: dict[str, str] = {}

    for source in sources:
        content = contents[source.source_id]
        token = sentinel_token(source.source_id)
        result = render_artifact(content, token)
        basename = f"{source.source_id.lower()}-{slugify(source.title)}.{result.extension}"
        if basename in basenames:
            raise GenerationError(f"basename collision: {basename}")
        basenames.add(basename)
        relative = f"sources/{basename}"
        (sources_dir / basename).write_bytes(result.data)
        source.path = relative
        if result.actual_kind is not source.artifact_kind:
            source.artifact_kind = result.actual_kind
        if result.degradation:
            degradations.append(result.degradation)
        if result.actual_kind in _TEXT_KINDS:
            text = result.data.decode("utf-8")
            text_by_source[source.source_id] = text
            _lint_vocabulary(text, relative, vocabulary_errors)
        _lint_vocabulary(source.title, f"{relative} (title)", vocabulary_errors)
        artifact_entries.append(
            ArtifactEntry(
                path=relative,
                kind=result.actual_kind,
                bytes_sha256=result.bytes_sha256,
                logical_sha256=result.logical_sha256,
                source_id=source.source_id,
            )
        )
    if vocabulary_errors:
        raise GenerationError("ontology lint failed:\n" + "\n".join(vocabulary_errors))

    expected_by_query = {e.query_id: e for e in expected}
    for query in queries:
        if query.canary:
            values = expected_by_query[query.query_id].answer.values
            if not values:
                raise GenerationError(f"{query.query_id}: canary query has no expected value")
            hits = sum(1 for text in text_by_source.values() if values[0] in text)
            if hits != 1:
                raise GenerationError(
                    f"{query.query_id}: canary value {values[0]!r} found in {hits} artifacts "
                    "(need exactly 1)"
                )

    policy = _merge_policies(graphs)
    dump_jsonl(entities, out_dir / "entities.jsonl")
    dump_jsonl(sources, out_dir / "sources.jsonl")
    dump_jsonl(events, out_dir / "events.jsonl")
    dump_jsonl(claims, out_dir / "claims.jsonl")
    dump_jsonl(queries, out_dir / "queries.jsonl")
    dump_jsonl(expected, out_dir / "expected.jsonl")
    dump_jsonl(schedule, out_dir / "schedule.jsonl")
    # The compiled altitude's input. Written into the corpus rather than built
    # per-adapter so every contender is handed identical conclusions, hashed and
    # reproducible from the seed — an adapter-side plan would be the renderer
    # defect wearing a new hat.
    compile_plan = derive_compile_plan(claims, entities)
    # Only the REAL corpus must carry a disagreement — neither a narrowed
    # selection nor a substituted template set. Both of those are fixtures, and
    # a guard that fires on fixtures protects nothing while breaking every
    # single-template test. What it protects is the artifact that gets
    # published, which is the full registry.
    publishable = template_ids is None and templates is None
    if publishable and not any(conclusion.disputes for conclusion in compile_plan):
        # A contradiction dimension with nothing to detect has a ceiling of zero
        # and discriminates nothing. That shipped once (4b.33); refuse rather
        # than emit a corpus in which it cannot be passed.
        raise ValueError(
            "compile plan contains no disputed pair: the contradiction dimension "
            "would be structurally unpassable (see 4b.33)"
        )
    dump_jsonl(compile_plan, out_dir / "compile-plan.jsonl")
    (out_dir / "policies.yaml").write_text(
        yaml.safe_dump(policy.model_dump(mode="json"), sort_keys=True), encoding="utf-8"
    )

    manifest = CorpusManifest(
        generator_version=GENERATOR_VERSION,
        master_seed=master_seed,
        templates=template_infos,
        counts={
            "entities": len(entities),
            "sources": len(sources),
            "events": len(events),
            "claims": len(claims),
            "queries": len(queries),
            "expected": len(expected),
            "schedule_ops": len(schedule),
            "conclusions": len(compile_plan),
        },
        renderer_versions=renderer_versions(),
        degradations=degradations,
        artifacts=sorted(artifact_entries, key=lambda entry: entry.path),
    )
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest
