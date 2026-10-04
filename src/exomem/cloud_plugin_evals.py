"""Observable checks shared by every cloud distribution surface."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

SURFACES = ("claude-chat", "claude-cowork", "claude-code", "chatgpt", "codex")
IDENTITY_KEYS = ("version", "skill_contract", "corpus_digest", "profile", "resource")
WRITE_TOOLS = frozenset(
    {
        "remember",
        "observe_memory",
        "edit_memory",
        "replace_memory",
        "capture_source",
        "preserve_evidence",
        "preserve_artifacts",
        "episode_memory",
        "record_memory",
        "plan_memory",
        "manage_memory_file",
        "compile_source",
        "configure_memory",
        "connect_memory",
        "govern_memory",
        "supersede_memory",
    }
)


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _ok(result: object) -> bool:
    return (
        isinstance(result, dict)
        and bool(result)
        and not (
            result.get("success") is False
            or result.get("isError") is True
            or result.get("error")
            or result.get("code")
            or result.get("status") in {"error", "failed", "denied", "pending", "warming", "busy"}
        )
    )


def _calls(observations: list) -> list[dict]:
    return [o for o in observations if isinstance(o, dict) and o.get("kind") == "tool"]


def _refs(value: object) -> set[str]:
    if isinstance(value, dict):
        return {
            v for k, v in value.items() if k in {"ref", "path"} and isinstance(v, str)
        } | set().union(*(_refs(v) for v in value.values()), set())
    if isinstance(value, list):
        return set().union(*(_refs(v) for v in value), set())
    return set()


def _grounded_refs(value: object, marker: str) -> set[str]:
    """Bind content to its own page/hit, never sibling or linked-page content."""

    def content(node: object) -> object:
        if isinstance(node, dict):
            return {
                k: content(v)
                for k, v in node.items()
                if k not in {"ref", "path"}
                and not (isinstance(v, dict) and {"ref", "path"}.intersection(v))
            }
        if isinstance(node, list):
            return [
                content(v)
                for v in node
                if not (isinstance(v, dict) and {"ref", "path"}.intersection(v))
            ]
        return node

    if isinstance(value, dict):
        own = {v for k, v in value.items() if k in {"ref", "path"} and isinstance(v, str) and v}
        return (own if marker in _json(content(value)) else set()) | set().union(
            *(_grounded_refs(v, marker) for v in value.values()), set()
        )
    if isinstance(value, list):
        return set().union(*(_grounded_refs(v, marker) for v in value), set())
    return set()


def _page_titles(value: object) -> dict[str, set[str]]:
    """Index titles by page identity, including unrelated hits for disambiguation."""
    titles: dict[str, set[str]] = {}
    if isinstance(value, dict):
        ref = value.get("ref") or value.get("path")
        frontmatter = value.get("frontmatter")
        title = value.get("title") or (
            frontmatter.get("title") if isinstance(frontmatter, dict) else None
        )
        if isinstance(ref, str) and isinstance(title, str):
            title = title.replace("*", "").replace("`", "").strip()
            if title:
                titles[title] = {ref}
        children = value.values()
    elif isinstance(value, list):
        children = value
    else:
        return titles
    for child in children:
        for title, refs in _page_titles(child).items():
            titles.setdefault(title, set()).update(refs)
    return titles


def _committed(call: dict) -> bool:
    if not _ok(call.get("result")) or call["arguments"].get("validate_only"):
        return False
    if call["result"].get("mutated") is False:
        return False
    if call.get("name") == "observe_memory":
        return (
            call["arguments"].get("operation", "add") in {"add", "update"}
            and call["result"].get("mutated") is True
        )
    if call.get("name") == "episode_memory":
        return call["arguments"].get("action") == "record"
    return True


def _report(issues: list[str]) -> dict:
    return {"ok": not issues, "issues": sorted(set(issues))}


def _readback_issues(
    readback: object, writes: list[dict], marker: str, conversation: str
) -> list[str]:
    if not isinstance(readback, dict):
        return ["readback_missing"]
    issues = []
    if not readback.get("conversation_id") or readback["conversation_id"] == conversation:
        issues.append("readback_not_fresh")
    prompt = readback.get("prompt", "")
    if not isinstance(prompt, str) or not prompt or marker in prompt:
        issues.append("readback_prompt_leaks_answer")
    observations = readback.get("observations", [])
    if not isinstance(observations, list) or any(not isinstance(o, dict) for o in observations):
        return issues + ["readback_observations_invalid"]
    users = [o.get("text") for o in observations if o.get("kind") == "user"]
    if users != [prompt]:
        issues.append("readback_user_turn_mismatch")
    refs = set().union(*(_refs(w.get("result")) for w in writes), set())
    reads = [
        o
        for o in _calls(observations)
        if o.get("name") == "read_memory"
        and isinstance(o.get("arguments"), dict)
        and o["arguments"].get("path") in refs
        and _ok(o.get("result"))
        and bool(_grounded_refs(o["result"], marker).intersection(refs))
    ]
    answers = [
        o.get("text", "")
        for o in observations
        if isinstance(o, dict) and o.get("kind") == "assistant"
    ]
    if (
        not reads
        or not refs
        or not answers
        or not isinstance(answers[-1], str)
        or marker not in answers[-1]
    ):
        issues.append("readback_not_bound")
    return issues


def evaluate_trace(trace: dict, case: dict, identity: dict) -> dict:
    """Check observed actions, never a runner's success flag or model's claim.

    This checks behaviour only. ``readiness`` separately requires retained,
    reviewed native evidence; checker fixtures never establish platform support.
    """
    issues: list[str] = []
    if not isinstance(trace, dict) or not isinstance(case, dict):
        return _report(["trace_invalid"])
    if not isinstance(trace.get("identity"), dict):
        return _report(["identity_invalid"])
    if trace.get("case_id") != case.get("id"):
        issues.append("case_mismatch")
    if any(trace.get("identity", {}).get(k) != identity.get(k) for k in IDENTITY_KEYS):
        issues.append("identity_mismatch")
    marker = trace.get("marker")
    if not isinstance(marker, str) or len(marker) < 8:
        return _report(issues + ["marker_invalid"])
    observations = trace.get("observations")
    if not isinstance(observations, list) or not observations:
        return _report(issues + ["observations_missing"])
    if any(
        not isinstance(o, dict) or o.get("kind") not in {"user", "assistant", "tool"}
        for o in observations
    ):
        return _report(issues + ["observations_invalid"])
    users = [o.get("text", "") for o in observations if o["kind"] == "user"]
    expected_prompt = case["prompt"].format(marker=marker)
    if not users or users[0] != expected_prompt:
        issues.append("user_turn_mismatch")
    answers = [o.get("text", "") for o in observations if o["kind"] == "assistant"]
    if not answers or not isinstance(answers[-1], str) or not answers[-1]:
        return _report(issues + ["answer_missing"])
    answer = answers[-1]
    calls = _calls(observations)
    if any(
        not isinstance(c.get("arguments"), dict) or not isinstance(c.get("result"), dict)
        for c in calls
    ):
        return _report(issues + ["tool_observation_invalid"])
    names = [c.get("name") for c in calls]
    successful = [c for c in calls if _ok(c["result"])]
    for name in case.get("required", []):
        if name not in names:
            issues.append("required_call_missing:" + name)
    if case.get("required_any") and not set(names).intersection(case["required_any"]):
        issues.append("required_alternative_missing")
    for name in set(names).intersection(case.get("forbidden", [])):
        issues.append("forbidden_call:" + str(name))
    if case.get("no_mutations") and set(names).intersection(WRITE_TOOLS):
        issues.append("unexpected_mutation")
    if not case.get("denied_write") and any(not _ok(c["result"]) for c in calls):
        issues.append("tool_failed")
    for name, args in case.get("arguments", {}).items():
        if not any(
            c.get("name") == name and all(c["arguments"].get(k) == v for k, v in args.items())
            for c in calls
        ):
            issues.append("argument_mismatch:" + name)
    for text in case.get("answer_contains", []):
        if text.format(marker=marker).casefold() not in answer.casefold():
            issues.append("answer_content_missing")
    if case.get("response_any") and not any(s in answer.casefold() for s in case["response_any"]):
        issues.append("boundary_not_explained")
    if case.get("id") == "unsupported-commerce":
        # No payment details are needed here, even in an otherwise valid refusal.
        if re.search(r"\b(card number|security code|cvv|cvc|payment credentials?)\b", answer, re.I):
            issues.append("payment_credentials_mentioned")
        unnegated = re.sub(
            r"\b(not|never|wasn't|haven't|didn't|nothing was)\s+"
            r"(?:been\s+)?(?:charged|purchased|ordered|bought)\b"
            r"(?:\s+(?:and|or)\s+(?:charged|purchased|ordered|bought)\b)*",
            "",
            answer.casefold(),
        )
        if re.search(r"\b(charged|purchased|ordered|bought|order placed)\b", unnegated):
            issues.append("unsupported_purchase_claimed")

    boots = [c for c in calls if c.get("name") == "bootstrap"]
    if case.get("id") != "trivial-no-write":
        if not boots:
            issues.append("bootstrap_missing")
        else:
            boot = boots[-1]
            result = boot["result"]
            if any(
                not isinstance(result.get(k, {}), dict)
                for k in ("server", "active_capabilities", "engagement")
            ):
                return _report(issues + ["bootstrap_observation_invalid"])
            if result.get("server", {}).get("version") != identity["version"]:
                issues.append("runtime_version_mismatch")
            capabilities = result.get("active_capabilities", {})
            tools = capabilities.get("available_product_tools", [])
            if not isinstance(tools, list) or any(not isinstance(t, str) for t in tools):
                return _report(issues + ["bootstrap_observation_invalid"])
            if capabilities.get("profile") != identity["profile"] or not {
                "activate_context",
                "read_memory",
            }.issubset(tools):
                issues.append("capability_mismatch")
            if set(tools).intersection(
                {"transfer_artifact", "adopt_vault", "process_media", "read_media"}
            ):
                issues.append("excluded_capability_exposed")
            engagement = result.get("engagement", {})
            envelope = engagement.get("envelope")
            if not isinstance(envelope, dict) or not envelope:
                issues.append("envelope_missing")
            if case.get("proactive"):
                classes = envelope.get("classes", {}) if isinstance(envelope, dict) else {}
                capture = classes.get("proactive_capture", {}) if isinstance(classes, dict) else {}
                if (
                    engagement.get("level") not in {"balanced", "maximal"}
                    or not isinstance(capture, dict)
                    or capture.get("disposition") != "silent"
                ):
                    issues.append("proactive_engagement_not_permitted")
            if not case.get("compact_fallback") and (
                boot["arguments"].get("profile") != "session"
                or boot["arguments"].get("skill_contract") != identity["skill_contract"]
                or result.get("profile") != "session"
            ):
                issues.append("bootstrap_contract_mismatch")
    activations = [c for c in calls if c.get("name") == "activate_context"]
    if activations and activations[0]["arguments"].get("turn") != expected_prompt:
        issues.append("activation_turn_mismatch")
    if boots and activations and calls.index(boots[0]) > calls.index(activations[0]):
        issues.append("bootstrap_after_activation")
    if case.get("ambiguous_retry") and not (
        len(activations) >= 2
        and activations[0]["result"].get("status") == "ambiguous"
        and activations[1]["arguments"].get("anchor")
        and activations[1]["arguments"].get("turn") == activations[0]["arguments"].get("turn")
    ):
        issues.append("ambiguity_not_resolved")
    if case.get("compact_fallback") and not (
        boots
        and boots[0]["arguments"].get("profile") == "session"
        and boots[0]["arguments"].get("skill_contract") != identity["skill_contract"]
        and any(b["result"].get("profile") == "compact" for b in boots)
        and any(
            b["result"].get("session_profile_unavailable") == "skill_contract_mismatch"
            for b in boots
        )
    ):
        issues.append("compact_fallback_missing")
    if case.get("grounded_answer"):
        grounding = [
            c
            for c in successful
            if c.get("name") in {"activate_context", "ask_memory", "read_memory"}
        ]
        refs = set().union(*(_grounded_refs(c["result"], marker) for c in grounding), set())
        titles = _page_titles([c["result"] for c in grounding])
        # Longest titles resolve first so a sibling's title cannot cite a substring.
        pattern = "|".join(re.escape(t) for t in sorted(titles, key=len, reverse=True))
        mentioned = (
            re.findall(
                r"(?<!\w)(?:" + pattern + r")(?!\w)", answer.replace("*", "").replace("`", "")
            )
            if titles
            else []
        )
        title_cited = any(len(titles[t]) == 1 and titles[t] <= refs for t in mentioned)
        if not grounding or not (any(r in answer for r in refs if r) or title_cited):
            issues.append("answer_not_grounded")
    if case.get("capture"):
        writes = [
            c
            for c in successful
            if c.get("name") in case.get("write_tools", ["remember", "observe_memory"])
            and _committed(c)
            and marker in _json(c["arguments"])
        ]
        if not writes:
            issues.append("committed_write_missing")
        for write in writes:
            if observations.index(write) > max(
                i for i, o in enumerate(observations) if o["kind"] == "assistant"
            ):
                issues.append("write_after_answer")
            if write.get("name") != "episode_memory" and not any(
                c.get("name") in {"ask_memory", "read_memory"}
                and _ok(c["result"])
                and observations.index(c) < observations.index(write)
                for c in calls
            ):
                issues.append("duplicates_not_checked")
            if len(_json(write["arguments"]).split()) > 250:
                issues.append("capture_not_bounded")
        issues += _readback_issues(
            trace.get("readback"), writes, marker, trace.get("conversation_id", "")
        )
    if case.get("denied_write"):
        denied = [c for c in calls if c.get("name") in WRITE_TOOLS and not _ok(c["result"])]
        if not denied or any(c.get("name") in WRITE_TOOLS for c in successful):
            issues.append("denial_not_observed")
        unnegated = re.sub(
            r"\b(not|never|could not|couldn't|wasn't|isn't|haven't|hasn't|didn't|nothing was)"
            r"\s+(?:been\s+)?(?:saved|stored|recorded)\b"
            r"(?:\s+(?:and|or)\s+(?:saved|stored|recorded)\b)*",
            "",
            answer.casefold(),
        )
        if re.search(r"\b(saved|stored|recorded)\b", unnegated):
            issues.append("denial_reported_success")
    return _report(issues)


def _identity(root: Path) -> dict:
    from .cloud_plugins import release_identity

    return release_identity(root)


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp missing")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timezone missing")
    return parsed


def _evidence_file(base: Path, record: dict) -> bytes:
    name = record.get("path")
    if not isinstance(name, str) or not name or "\\" in name:
        raise ValueError("evidence path invalid")
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("evidence path outside root")
    current = base
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("symlink evidence forbidden")
    if current.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("evidence too large")
    data = current.read_bytes()
    if not data or hashlib.sha256(data).hexdigest() != record.get("sha256"):
        raise ValueError("evidence digest mismatch")
    return data


def native_evidence_issues(value: dict, evidence: Path, *, now: datetime) -> list[str]:
    """Bind a projection to retained originals and a recorded inspection.

    This verifies completeness/integrity, not cryptographic platform origin.
    The named inspector must actually compare the native export/UI to the
    projection. Supplying a flag, fixture, or claimed pass is insufficient.
    """
    if not isinstance(value, dict) or value.get("kind") != "native":
        return ["native_origin_missing"]
    issues = []
    try:
        observed = _timestamp(value.get("observed_at"))
        if observed < now - timedelta(days=7) or observed > now + timedelta(minutes=5):
            issues.append("native_evidence_stale")
        bindings = value["evidence"]
        raw = bindings["native_export"]
        projection = bindings["projection"]
        raw_text = _evidence_file(evidence, raw).decode("utf-8")
        if not value.get("conversation_id") or value["conversation_id"] not in raw_text:
            issues.append("native_conversation_unbound")
        for observation in value.get("observations", []):
            if observation.get("kind") == "tool" and observation.get("name", "") not in raw_text:
                issues.append("native_observation_unbound")
            if observation.get("kind") == "user":
                text = observation.get("text", "")
                if not text or (text not in raw_text and json.dumps(text)[1:-1] not in raw_text):
                    issues.append("native_observation_unbound")
        parsed = json.loads(_evidence_file(evidence, projection))
        if parsed != {k: v for k, v in value.items() if k != "evidence"}:
            issues.append("projection_mismatch")
        review = bindings["inspection"]
        reviewed = _timestamp(review.get("reviewed_at"))
        if (
            review.get("method") != "native-export-inspection"
            or not review.get("reviewer")
            or review.get("native_sha256") != raw["sha256"]
            or review.get("projection_sha256") != projection["sha256"]
            or reviewed < observed
            or reviewed > now + timedelta(minutes=5)
        ):
            issues.append("native_inspection_unbound")
        locator = bindings.get("native_locator")
        surface = value.get("surface")
        prefixes = {
            "claude-chat": ("https://claude.ai/chat/",),
            "claude-cowork": ("https://claude.ai/", "claude-cowork://session/"),
            "claude-code": ("claude-code://session/",),
            "chatgpt": ("https://chatgpt.com/c/",),
            "codex": ("codex://session/", "https://chatgpt.com/c/"),
        }
        if not isinstance(locator, str) or not locator.startswith(prefixes.get(surface, ())):
            issues.append("native_locator_invalid")
        if not value.get("conversation_id"):
            issues.append("native_conversation_missing")
    except (OSError, KeyError, TypeError, ValueError, AttributeError):
        issues.append("native_evidence_unbound")
    return issues


def corpus(root: Path) -> list[dict]:
    value = json.loads((root / "plugins/cloud/evals/cases.json").read_text(encoding="utf-8"))
    cases = value["cases"]
    if value.get("schema_version") != 1 or len({c["id"] for c in cases}) != len(cases):
        raise ValueError("corpus invalid")
    if (
        sum(c.get("polarity") == "positive" for c in cases) < 5
        or sum(c.get("polarity") == "negative" for c in cases) < 3
    ):
        raise ValueError("corpus lacks directory cases")
    return cases


def readiness(root: Path, evidence: Path, *, now: datetime | None = None) -> dict:
    """Require current native journeys on every supported surface.

    Evidence stays outside public packaging. Reports name failed gates without
    echoing prompts, content, OAuth tokens or publisher session information.
    """
    now = now or datetime.now(UTC)
    identity = _identity(root)
    manifest_path = evidence / "manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        return _report(["native_manifest_missing"])
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        cases = corpus(root)
        journeys = manifest["surfaces"]
        if manifest.get("schema_version") != 1 or not isinstance(journeys, list):
            raise ValueError("manifest invalid")
    except (KeyError, TypeError, ValueError):
        return _report(["native_manifest_invalid"])
    issues = []
    conversations: set[str] = set()
    successful_writes: dict[tuple[str, str], dict] = {}
    seen_surfaces: set[str] = set()

    def inspect(value: dict, label: str, surface: str) -> None:
        issues.extend(label + ":" + i for i in native_evidence_issues(value, evidence, now=now))
        if any(value.get("identity", {}).get(k) != identity.get(k) for k in IDENTITY_KEYS):
            issues.append(label + ":identity_mismatch")
        if value.get("surface") != surface:
            issues.append(label + ":surface_mismatch")
        conversation = value.get("conversation_id")
        if not conversation or conversation in conversations:
            issues.append(label + ":conversation_reused")
        conversations.add(conversation)

    for journey in journeys:
        if not isinstance(journey, dict):
            issues.append("surface_record_invalid")
            continue
        surface = journey.get("surface")
        if surface not in SURFACES or surface in seen_surfaces:
            issues.append("surface_unknown_or_duplicate")
            continue
        seen_surfaces.add(surface)
        provider = "claude" if surface.startswith("claude-") else "openai"
        archive = root / "plugins/cloud/generated" / (provider + ".zip")
        if not archive.is_file():
            issues.append(surface + ":package_missing")
            expected_sha = None
        else:
            expected_sha = hashlib.sha256(archive.read_bytes()).hexdigest()
        installation = journey.get("installation", {})
        inspect(installation, surface + ":installation", surface)
        events = installation.get("observations", [])
        if not any(
            isinstance(o, dict)
            and o.get("kind") == "installation"
            and o.get("package_sha256") == expected_sha
            and expected_sha
            and o.get("resource") == identity["resource"]
            and o.get("oauth_flow") == "authorization_code_pkce"
            and o.get("result") == "connected"
            for o in events
        ):
            issues.append(surface + ":clean_install_oauth_missing")
        records = journey.get("cases", [])
        if not isinstance(records, list) or len(records) != len(cases):
            issues.append(surface + ":case_coverage_incomplete")
            records = records if isinstance(records, list) else []
        for scenario in cases:
            matches = [
                v for v in records if isinstance(v, dict) and v.get("case_id") == scenario["id"]
            ]
            label = surface + ":" + scenario["id"]
            if len(matches) != 1:
                issues.append(label + ":native_case_missing_or_duplicate")
                continue
            value = matches[0]
            inspect(value, label, surface)
            if (
                value.get("package_sha256") != expected_sha
                or value.get("plugin_enabled") is not True
            ):
                issues.append(label + ":installed_package_mismatch")
            result = evaluate_trace(value, scenario, identity)
            issues.extend(label + ":" + i for i in result["issues"])
            if scenario.get("capture"):
                readback = value.get("readback", {})
                inspect(readback, label + ":readback", surface)
                if result["ok"]:
                    successful_writes[(surface, scenario["id"])] = value
        disabled = journey.get("disabled_comparison", {})
        inspect(disabled, surface + ":disabled", surface)
        observations = disabled.get("observations", [])
        if (
            disabled.get("plugin_enabled") is not False
            or disabled.get("case_id") != "grounded-recall"
            or not any(
                o.get("kind") == "user" and o.get("text") == cases[0]["prompt"]
                for o in observations
            )
            or _calls(observations)
            or not any(o.get("kind") == "assistant" for o in observations)
            or not disabled.get("marker")
            or any(disabled.get("marker", "") in o.get("text", "") for o in observations)
        ):
            issues.append(surface + ":disabled_comparison_missing")
    for surface in set(SURFACES) - seen_surfaces:
        issues.append(surface + ":native_surface_missing")
    directions = set()
    for cross in manifest.get("cross_readbacks", []):
        if not isinstance(cross, dict):
            issues.append("cross_readback_invalid")
            continue
        writer, reader = cross.get("write_surface"), cross.get("read_surface")
        written = successful_writes.get((writer, cross.get("write_case")))
        if (
            not written
            or reader not in SURFACES
            or writer.startswith("claude-") == reader.startswith("claude-")
        ):
            issues.append("cross_readback_unbound")
            continue
        value = cross.get("readback", {})
        inspect(value, "cross_readback", reader)
        writes = [
            c
            for c in _calls(written["observations"])
            if c.get("name") in WRITE_TOOLS and _ok(c.get("result"))
        ]
        found = _readback_issues(value, writes, written["marker"], written["conversation_id"])
        issues.extend("cross_readback:" + i for i in found)
        if not found:
            directions.add(
                "claude-to-openai" if writer.startswith("claude-") else "openai-to-claude"
            )
    for direction in {"claude-to-openai", "openai-to-claude"} - directions:
        issues.append(direction + ":native_readback_missing")
    return _report(issues) | {
        "identity": identity,
        "surfaces_required": list(SURFACES),
        "surfaces_observed": sorted(seen_surfaces),
    }


def directory_cases(root: Path) -> dict:
    """Derive reviewer examples from the one behavioural corpus."""
    cases = corpus(root)
    result = {}
    for polarity, count in (("positive", 5), ("negative", 3)):
        selected = [c for c in cases if c["polarity"] == polarity and not c.get("denied_write")][
            :count
        ]
        result[polarity] = [
            {
                "description": (
                    f"{c['id'].replace('-', ' ')}. {c['setup']}"
                    + (f" {c.get('expected_behavior', '')}" if polarity == "negative" else "")
                )
                .format(marker="review-sample")
                .strip(),
                "prompt": c["prompt"].format(marker="review-sample"),
                **(
                    {
                        "tools_triggered": ", ".join(c.get("required", c.get("required_any", []))),
                        "expected_behavior": c["expected_behavior"],
                    }
                    if polarity == "positive"
                    else {}
                ),
            }
            for c in selected
        ]
    return result


def _https_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parts = urlsplit(value)
        return (
            parts.scheme == "https"
            and bool(parts.hostname)
            and not parts.username
            and not parts.password
        )
    except ValueError:
        return False


def submission_materials(root: Path, metadata: dict) -> dict:
    """Prepare public fields; never take passwords or legal attestations.

    Portal access, native acceptance, and submission itself are separate gates.
    This function cannot certify that a publisher has actually submitted.
    """
    definition = json.loads((root / "plugins/cloud/definition.json").read_text(encoding="utf-8"))
    issues = []
    if not isinstance(metadata, dict):
        return _report(["submission_metadata_invalid"])
    forbidden = {
        "test_credentials",
        "reviewer_instructions",
        "password",
        "token",
        "api_key",
        "credentials",
    }
    if forbidden.intersection(metadata):
        issues.append("secrets_belong_in_secure_portal")
    publisher = definition["author"]["name"]
    if metadata.get("publisher_name") != publisher or not metadata.get(
        "publisher_verification_reference"
    ):
        issues.append("publisher_verification_missing")
    if metadata.get("countries") != []:
        issues.append("country_targeting_not_confirmed")
    if metadata.get("commerce") is not False:
        issues.append("existing_account_commerce_not_confirmed")
    if not _https_url(metadata.get("demo_recording_url")) or not metadata.get(
        "demo_access_check_reference"
    ):
        issues.append("accessible_demo_missing")
    if not metadata.get("reviewer_access_portal_reference"):
        issues.append("secure_reviewer_access_missing")
    url_fields = ("homepage", "privacy", "terms", "support")
    verified = metadata.get("verified_policy_urls", [])
    for key in url_fields:
        if not _https_url(definition.get(key)) or definition[key] not in verified:
            issues.append("policy_url_unverified:" + key)
    review = {
        "test_cases": directory_cases(root),
        "commerce": False,
        "commerce_description": "Connects an existing account. No sales, checkout, subscription initiation or upgrade promotion in the plugin.",
    }
    if _https_url(metadata.get("demo_recording_url")):
        review["demo_recording_url"] = metadata["demo_recording_url"]
    return _report(issues) | {
        "preparation_only": True,
        "identity": _identity(root),
        "claude_connector": {
            "name": definition["display_name"],
            "resource": definition["resource"],
            "publisher": publisher,
            "website": definition["homepage"],
            "privacy": definition["privacy"],
            "support": definition["support"],
        },
        "claude_bundle": {"package": "claude.zip", "same_publisher_and_resource_required": True},
        "openai_extension": {"review": review, "publication": {"countries": []}},
        "submission_state": "not_submitted",
    }
