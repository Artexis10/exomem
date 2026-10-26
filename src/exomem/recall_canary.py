"""Installed read-only MCP canary with separate availability and latency checks."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.resources
import inspect
import json
import logging
import math
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit


def _digest(value: str | bytes) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def _observer_identity(package_mode: str) -> dict[str, Any]:
    from .install_info import package_identity

    return package_identity(package_mode, role="observer-only")


def _connection_failure_details(error: Exception) -> dict[str, Any]:
    from httpx2 import HTTPStatusError, TimeoutException, TransportError
    from mcp.shared.exceptions import MCPError

    http_status_codes: set[int] = set()
    mcp_error_codes: set[int] = set()
    timeout_observed = transport_error_observed = truncated = False
    pending: list[BaseException] = [error]
    seen: set[int] = set()
    # SDK cause chains can cycle or contain large groups. Inspect only bounded
    # typed protocol fields; exception text, requests and responses stay private.
    max_causes = 16
    while pending and len(seen) < max_causes:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, HTTPStatusError):
            http_status_codes.add(current.response.status_code)
        if isinstance(current, MCPError):
            mcp_error_codes.add(current.error.code)
        if isinstance(current, (TimeoutError, TimeoutException)):
            timeout_observed = True
        if isinstance(current, TransportError):
            transport_error_observed = True
        if isinstance(current, BaseExceptionGroup):
            truncated |= len(current.exceptions) > max_causes
            pending.extend(current.exceptions[:max_causes])
        cause = current.__cause__ or current.__context__
        if cause is not None:
            pending.append(cause)
    return {
        "http_status_codes": sorted(http_status_codes),
        "mcp_error_codes": sorted(mcp_error_codes),
        "timeout_observed": timeout_observed,
        "transport_error_observed": transport_error_observed,
        "chain_truncated": truncated or bool(pending),
    }


def _compile_response_schema(schema):
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012, specification_with

    Draft202012Validator.check_schema(schema)
    resources = [Resource.from_contents(schema, default_specification=DRAFT202012)]
    while resources:
        resource = resources.pop()
        contents = resource.contents
        # The observer fixes the JSON Schema dialect, which is a technical format.
        if isinstance(contents, dict) and "$schema" in contents:
            if specification_with(contents["$schema"]) != DRAFT202012:
                raise ValueError("unsupported schema dialect")
        resources.extend(resource.subresources())
    # Case files are self-contained; an empty registry has no external retrieval.
    return Draft202012Validator(schema, registry=Registry())


def _canary_cases(raw: bytes):
    from pydantic import BaseModel, ConfigDict, Field, JsonValue, PrivateAttr, model_validator

    operation_validators = {}

    class Reference(BaseModel):
        model_config = ConfigDict(extra="forbid", strict=True)
        path: list[str | int] = Field(min_length=1)
        value: str = Field(min_length=1)

    class Expected(BaseModel):
        model_config = ConfigDict(extra="forbid", strict=True)
        state_path: list[str | int] = Field(min_length=1)
        state: JsonValue
        outcome: Literal["ok", "abstained"] = "ok"
        references: list[Reference]
        material_paths: list[list[str | int]]

        @model_validator(mode="after")
        def positive_material(self):
            if isinstance(self.state, (dict, list)):
                raise ValueError("state must be a scalar")
            if self.outcome == "ok" and (not self.references or not self.material_paths):
                raise ValueError("positive cases need source truth and material")
            paths = [self.state_path, *self.material_paths, *(ref.path for ref in self.references)]
            if any(
                not path
                or any(
                    type(key) not in (str, int) or isinstance(key, int) and key < 0 for key in path
                )
                for path in paths
            ):
                raise ValueError("invalid result path")
            return self

    class SchemaExpected(BaseModel):
        model_config = ConfigDict(extra="forbid", strict=True)
        outcome: Literal["ok", "abstained"] = "ok"
        response_schema: dict[str, JsonValue]
        _response_validator: Any = PrivateAttr()

        @model_validator(mode="after")
        def compile_schema(self):
            self._response_validator = _compile_response_schema(self.response_schema)
            return self

    class Case(BaseModel):
        model_config = ConfigDict(extra="forbid", strict=True)
        id: str = Field(min_length=1)
        # These are the canary protocol's closed tool tokens, not vault vocabulary.
        tool: Literal[
            "ask_memory",
            "activate_context",
            "read_memory",
            "search_memory",
            "recall_context",
            "read_records",
            "read_plans",
        ]
        bar_id: str = Field(default="programme-public-warm", min_length=1)
        arguments: dict[str, JsonValue]
        expected: Expected | SchemaExpected

        @model_validator(mode="after")
        def read_only_operation(self):
            # These tool tokens fix the observer's collection-read protocol.
            if self.tool not in ("read_records", "read_plans"):
                return self
            from . import commands
            from .operation_entries import split_operation
            from .operation_schema import operation_schema

            parsed = split_operation(self.arguments)
            # Inspect and query are the observer protocol's closed collection operation tokens.
            if parsed is None or parsed[0] not in ("inspect", "query"):
                raise ValueError("collection cases require inspect or query")
            if self.tool not in operation_validators:
                command, rendering = next(
                    (command, rendering)
                    for command in commands.product_commands_for("mcp")
                    for rendering in command.renderings
                    if rendering.name == self.tool
                )
                grouped = commands.bind_vault(command.leaf, None, command=command)
                operation_validators[self.tool] = _compile_response_schema(
                    operation_schema(rendering, inspect.signature(grouped).parameters)
                )
            operation_validators[self.tool].validate(self.arguments["operation"])
            return self

    def reject_nonfinite(value):
        raise ValueError("non-finite JSON value")

    values = json.loads(raw, parse_constant=reject_nonfinite)
    if not isinstance(values, list) or not values:
        raise ValueError("cases must be a nonempty list")
    cases = [Case.model_validate(value) for value in values]
    if len({case.id for case in cases}) != len(cases):
        raise ValueError("case identities must be unique")
    return cases


def _at_path(data: Any, path: list[str | int]) -> Any:
    for key in path:
        if isinstance(data, dict) and isinstance(key, str):
            data = data[key]
        elif isinstance(data, list) and type(key) is int:
            data = data[key]
        else:
            raise KeyError
    return data


def _canary_result(result, expected, *, diagnostics=None) -> tuple[str, str | None]:
    if result.is_error:
        return "error", "tool-error"
    # The SDK parses the published output schema and removes its transport wrapper.
    data = result.data
    if not isinstance(data, (dict, list)):
        return "error", "malformed-terminal"
    # Public OpError and activation fields are product protocol shapes.
    envelope = data if isinstance(data, dict) else {}
    outcome = (
        "refused"
        if envelope.get("success") is False
        else "abstained"
        if envelope.get("abstained") is True
        else "ok"
    )
    if outcome != expected.outcome:
        return outcome, "unexpected-outcome"
    if (validator := getattr(expected, "_response_validator", None)) is not None:
        try:
            error = next(validator.iter_errors(data), None)
        except Exception:  # noqa: BLE001 - schema failures can contain private case and response data
            return outcome, "invalid-expectation"
        if error is None:
            return outcome, None
        if diagnostics is not None:
            try:
                # JSON Schema keywords are protocol tokens; paths can contain private field names.
                diagnostics["schema_failure"] = {
                    "keyword": error.validator,
                    "schema_path_sha256": _digest(
                        json.dumps(list(error.absolute_schema_path), separators=(",", ":"))
                    ),
                }
            except Exception:  # noqa: BLE001 - optional diagnostics cannot change a proven mismatch
                pass
        return outcome, "unexpected-response"
    try:
        state = _at_path(data, expected.state_path)
        if state != expected.state or isinstance(state, bool) != isinstance(expected.state, bool):
            return outcome, "unexpected-state"
    except (KeyError, IndexError):
        return outcome, "missing-state"
    for reference in expected.references:
        try:
            if _at_path(data, reference.path) == reference.value:
                continue
        except (KeyError, IndexError):
            pass
        return outcome, "missing-reference"
    for path in expected.material_paths:
        try:
            material = _at_path(data, path)
            if isinstance(material, str) and material.strip():
                continue
        except (KeyError, IndexError):
            pass
        return outcome, "missing-material"
    return outcome, None


def _canary_warning(report: dict[str, Any]) -> dict[str, Any]:
    report.setdefault("check", "acceptance")
    report["check_verdict"] = (
        (
            "failed"
            if report.get("failure") or report.get("availability") == "failed"
            else "passed"
            if report.get("availability") == "passed"
            else "incomplete"
        )
        if report["check"] == "availability"
        else report["verdict"]
    )
    if report["check_verdict"] != "passed":
        from exomem.log_events import log_event

        log_event(
            logging.getLogger("exomem.recall_canary"),
            logging.WARNING,
            "recall_canary_failed",
            fields={
                "verdict": report["verdict"],
                "check": report["check"],
                "check_verdict": report["check_verdict"],
                "failure": report.get("failure"),
                "availability": report.get("availability"),
                "coverage": report.get("coverage"),
                "cases_sha256": report.get("cases_sha256"),
            },
        )
    return report


async def run_mcp_canary(
    *,
    client,
    cases_file: Path,
    samples: int | None = None,
    warm_up: bool = False,
    contention: str = "unknown",
    deployment_id: str | None = None,
    deployment_verified: bool = False,
    package_mode: str = "installed",
    contract_file: Path | None = None,
    target_digest: str | None = None,
    timeout_seconds: float = 30.0,
    check: Literal["availability", "acceptance"] = "acceptance",
) -> dict[str, Any]:
    from exomem import latency_watch
    from exomem.request_latency_contract import LatencyContract

    started = time.time()
    report: dict[str, Any] = {
        "mode": "mcp-canary",
        "check": check,
        "verdict": "failed",
        "availability": "unattempted",
        "coverage": "incomplete",
        "started_at": started,
        "ended_at": None,
        "target_sha256": target_digest,
        "execution_id_sha256": _digest(os.environ["INVOCATION_ID"])
        if os.environ.get("INVOCATION_ID")
        else None,
        "cases": [],
        "initialization_ms": None,
        "contention": contention,
        "deployment": {
            "identity_sha256": _digest(deployment_id) if deployment_id else None,
            "verified": bool(deployment_id and deployment_verified),
            "provenance": "caller-supplied; observer does not attest remote runtime",
        },
        "limitations": [
            "repeated cases may use result caches",
            "read-only cases do not accept other operations",
            "alert installation requires the operator runner",
        ],
    }
    try:
        raw = cases_file.read_bytes()
        report["cases_sha256"] = _digest(raw)
        cases = _canary_cases(raw)
    except Exception:  # noqa: BLE001 - private validation details must not escape
        report["failure"] = "invalid-cases"
        report["ended_at"] = time.time()
        return _canary_warning(report)
    try:
        report["observer"] = _observer_identity(package_mode)
        contract_bytes = (
            contract_file.read_bytes()
            if contract_file
            else importlib.resources.files("exomem")
            .joinpath("request_latency_contract.json")
            .read_bytes()
        )
        contract = LatencyContract.model_validate_json(contract_bytes)
        bars = [contract.bar(case.bar_id) for case in cases]
        for case, bar in zip(cases, bars, strict=True):
            # MCP is the contract protocol's surface token; the contract owns coverage.
            if bar.surface != "mcp" or not bar.covers(
                case.tool, latency_watch.deep_flag(case.arguments)
            ):
                raise ValueError("case bar does not cover its MCP operation")
        # Availability needs one actual call; acceptance uses the contract's series.
        samples = (
            (1 if check == "availability" else max(bar.min_samples for bar in bars))
            if samples is None
            else samples
        )
        report["contract"] = {
            "revision": contract.revision,
            "sha256": _digest(contract_bytes),
            "source": "controller-file" if contract_file else "observer-package",
        }
        if samples < 1 or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("invalid sample or timeout bound")
    except Exception:  # noqa: BLE001 - package and contract errors can include private paths
        report["failure"] = "invalid-observer-or-contract"
        report["ended_at"] = time.time()
        return _canary_warning(report)

    for case in cases:
        report["cases"].append(
            {
                "id_sha256": _digest(case.id),
                "tool": case.tool,
                "bar_id": case.bar_id,
                "availability": "unattempted",
                "warmth": "unknown",
                "measured_attempts": 0,
                "observed_terminals": 0,
                "p50_ms": None,
                "p95_ms": None,
                "requested_samples": samples,
                "attempts": [],
                "latency": [],
            }
        )
    initialization_start = time.perf_counter()
    # These phases describe the observer lifecycle, not the serving process.
    phase = "connect"
    try:
        async with client:
            report["initialization_ms"] = (time.perf_counter() - initialization_start) * 1000
            phase = "cases"
            for case, series, bar in zip(cases, report["cases"], bars, strict=True):
                observations = []
                for repetition in range(samples + int(warm_up)):
                    warming = warm_up and repetition == 0
                    call_start = time.perf_counter()
                    diagnostics = {}
                    terminal_received = False
                    try:
                        try:
                            async with asyncio.timeout(timeout_seconds):
                                result = await client.call_tool(
                                    case.tool,
                                    case.arguments,
                                    raise_on_error=False,
                                    timeout=timeout_seconds,
                                )
                                terminal_received = True
                        finally:
                            # Parsed caller terminals end timing before observer assertions.
                            elapsed = (time.perf_counter() - call_start) * 1000
                        outcome, failure = _canary_result(
                            result, case.expected, diagnostics=diagnostics
                        )
                    except Exception:  # noqa: BLE001 - no remote exception text enters evidence
                        outcome, failure = "error", "call-failed"
                    series["attempts"].append(
                        {
                            "phase": "warm-up" if warming else "measured",
                            "elapsed_ms": elapsed,
                            "outcome": outcome,
                            "failure": failure,
                            **(diagnostics if failure == "unexpected-response" else {}),
                        }
                    )
                    if not warming:
                        series["measured_attempts"] += 1
                        series["observed_terminals"] += int(terminal_received)
                        observations.append(
                            latency_watch.make_sample(
                                at=time.time(),
                                tool=case.tool,
                                client="live-canary",
                                deep=latency_watch.deep_flag(case.arguments),
                                total_ms=elapsed,
                                spans=None,
                                bar_id=bar.id,
                                outcome=outcome,
                                source="mcp-canary",
                                contract=contract,
                                trusted_tool=True,
                            )
                        )
                    elif failure is None:
                        series["warmth"] = "successful-case-proof"
                    # These are observer protocol codes, not service or vault vocabulary.
                    if failure == "invalid-expectation":
                        report["failure"] = failure
                        series["availability"] = "incomplete"
                    else:
                        series["availability"] = "failed" if failure else "passed"
                    if failure:
                        break
                series["latency"] = latency_watch.summarize(
                    observations,
                    now=time.time(),
                    contract=contract,
                    coverage={"population": "declared-series", "evictions": 0},
                )
                if series["availability"] in ("failed", "incomplete"):
                    break
            phase = "disconnect"
    except Exception as exc:  # noqa: BLE001 - exception text can contain credentials and targets
        report["failure"] = "session-failed"
        report["failure_phase"] = phase
        report["failure_details"] = _connection_failure_details(exc)
        if report["initialization_ms"] is None:
            report["initialization_ms"] = (time.perf_counter() - initialization_start) * 1000
    report["ended_at"] = time.time()
    for series in report["cases"]:
        measured = sorted(
            attempt["elapsed_ms"]
            for attempt in series["attempts"]
            if attempt["phase"] == "measured"
        )
        series["p50_ms"] = round(latency_watch._percentile(measured, 50), 3) if measured else None
        series["p95_ms"] = round(latency_watch._percentile(measured, 95), 3) if measured else None
    complete = all(
        series["availability"] != "incomplete"
        and series["measured_attempts"] == samples
        and samples >= bar.min_samples
        for series, bar in zip(report["cases"], bars, strict=True)
    )
    positive_coverage = {}
    for tool in dict.fromkeys(case.tool for case in cases):
        configured = [
            series
            for case, series in zip(cases, report["cases"], strict=True)
            if case.tool == tool and case.expected.outcome == "ok"
        ]
        successful = sum(series["availability"] == "passed" for series in configured)
        positive_coverage[tool] = {
            "configured_cases": len(configured),
            "successful_cases": successful,
            "coverage": "complete" if successful else "incomplete",
        }
    report["positive_coverage"] = positive_coverage
    positives_proven = all(item["successful_cases"] for item in positive_coverage.values())
    report["coverage"] = "complete" if complete and positives_proven else "incomplete"
    availability = [series["availability"] for series in report["cases"]]
    report["availability"] = (
        "failed"
        if "failed" in availability
        else "incomplete"
        if "incomplete" in availability
        else "unattempted"
        if "unattempted" in availability
        else "passed"
        if positives_proven
        else "incomplete"
    )
    failed_latency = any(row["breach"] for series in report["cases"] for row in series["latency"])
    known_warm = all(series["warmth"] == "successful-case-proof" for series in report["cases"])
    latency_passed = all(
        series["latency"]
        and all(row["verdict"] == "observed-within-bar" for row in series["latency"])
        for series in report["cases"]
    )
    report["verdict"] = (
        "failed"
        if report.get("failure") or report["availability"] == "failed" or failed_latency
        else "passed"
        if complete
        and positives_proven
        and latency_passed
        and known_warm
        and contention == "uncontended"
        and report["deployment"]["verified"]
        else "incomplete"
    )
    return _canary_warning(report)


def _write_report(path: Path, report: dict[str, Any]) -> None:
    from .service_manager import private_directory

    directory = private_directory(path.parent)
    descriptor, temporary = tempfile.mkstemp(prefix=".canary-", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(report, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def validate_target(value: str | None) -> None:
    target = urlsplit(value or "")
    _ = target.port  # The URL parser rejects invalid and out-of-range ports here.
    # HTTP(S) is this runner's transport protocol; credentials belong in headers.
    if (
        target.scheme not in ("http", "https")
        or not target.hostname
        or target.username
        or target.password
        or target.fragment
    ):
        raise ValueError("invalid target")


def _http_diagnostics_factory(observations):
    from mcp.shared._httpx_utils import create_mcp_http_client

    async def observe_response(response):
        # Bound protocol metadata; URLs, headers and bodies never enter the report.
        if len(observations["responses"]) >= 16:
            observations["truncated"] = True
            return
        try:
            observations["responses"].append(
                {
                    "method": response.request.method,
                    "status_code": response.status_code,
                    "observed_at": time.time(),
                }
            )
        except Exception:  # noqa: BLE001 - optional diagnostics must not change a response
            observations["capture_incomplete"] = True

    def client_factory(headers=None, timeout=None, auth=None, follow_redirects=False):
        # The transport constrains redirects per request; preserve the SDK client defaults.
        client = create_mcp_http_client(headers=headers, timeout=timeout, auth=auth)
        client.event_hooks.setdefault("response", []).append(observe_response)
        observations["client_created"] = True
        return client

    return client_factory


def run_cli(args) -> int:
    from exomem.log_events import JsonLinesFormatter

    logger = logging.getLogger("exomem.recall_canary")
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLinesFormatter())
    logger.addHandler(handler)
    started = time.time()
    # The legacy script's boolean --check keeps its acceptance meaning.
    check = args.check if isinstance(args.check, str) else "acceptance"
    try:
        validate_target(args.mcp_url)
    except ValueError:
        report = _canary_warning(
            {
                "mode": "mcp-canary",
                "verdict": "failed",
                "check": check,
                "failure": "invalid-target" if args.mcp_url else "unconfigured",
            }
        )
    else:
        token = os.environ.get(args.token_env, "").strip()
        if not token:
            report = _canary_warning(
                {
                    "mode": "mcp-canary",
                    "verdict": "failed",
                    "check": check,
                    "failure": "missing-token",
                }
            )
        else:
            from fastmcp import Client
            from fastmcp.client.transports import StreamableHttpTransport

            # This standalone observer emits only its content-free warning. SDK
            # logs can contain remote errors, endpoint URLs or server material.
            previous_logging = logging.root.manager.disable
            logging.disable(logging.CRITICAL)
            http_observations = {
                "client_created": False,
                "responses": [],
                "truncated": False,
                "capture_incomplete": False,
            }
            try:
                transport = StreamableHttpTransport(
                    args.mcp_url,
                    headers={"Authorization": f"Bearer {token}"},
                    httpx_client_factory=_http_diagnostics_factory(http_observations),
                )
                report = asyncio.run(
                    run_mcp_canary(
                        client=Client(transport, timeout=args.timeout, init_timeout=args.timeout),
                        cases_file=args.cases,
                        samples=args.samples,
                        warm_up=args.warm_up,
                        contention=args.contention,
                        deployment_id=args.deployment_id,
                        deployment_verified=args.deployment_verified,
                        package_mode=args.package_mode or "installed",
                        contract_file=args.contract,
                        target_digest=_digest(args.mcp_url),
                        timeout_seconds=args.timeout,
                        check=check,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - client errors can contain the target
                report = {
                    "mode": "mcp-canary",
                    "verdict": "failed",
                    "check": check,
                    "failure": "invalid-client",
                    "failure_phase": "client-setup",
                    "failure_details": _connection_failure_details(exc),
                }
            finally:
                logging.disable(previous_logging)
            report["http_transport_observations"] = http_observations
            _canary_warning(report)
    report.setdefault("started_at", started)
    report.setdefault("ended_at", time.time())
    report.setdefault(
        "execution_id_sha256",
        _digest(os.environ["INVOCATION_ID"]) if os.environ.get("INVOCATION_ID") else None,
    )
    if args.report_file:
        try:
            _write_report(args.report_file, report)
        except Exception:  # noqa: BLE001 - report paths and errors stay private
            report["failure"] = "report-write-failed"
            report["verdict"] = "failed"
            _canary_warning(report)
    logger.removeHandler(handler)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["check_verdict"] == "passed" else 1


def add_arguments(parser, *, source_group=None, selectable_check: bool = True) -> None:
    (source_group or parser).add_argument("--mcp-url", help="read-only MCP canary endpoint")
    parser.add_argument("--cases", type=Path, help="private JSON case file")
    parser.add_argument("--token-env", default="EXOMEM_MCP_TOKEN")
    parser.add_argument("--package-mode", choices=("installed", "source"))
    parser.add_argument("--contract", type=Path, help="explicit controller latency contract")
    parser.add_argument("--deployment-id")
    parser.add_argument("--deployment-verified", action="store_true")
    parser.add_argument("--samples", type=int, help="uses the selected check's sample count")
    parser.add_argument("--warm-up", action="store_true")
    # These are measurement-protocol conditions, not vault vocabulary.
    parser.add_argument(
        "--contention", choices=("uncontended", "contended", "unknown"), default="unknown"
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--report-file", type=Path, help="atomic owner-only latest report")
    if selectable_check:
        parser.add_argument("--check", choices=("availability", "acceptance"), default="acceptance")


def argument_parser(*, add_help: bool = True, exit_on_error: bool = True):
    parser = argparse.ArgumentParser(
        description=__doc__, add_help=add_help, exit_on_error=exit_on_error
    )
    add_arguments(parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = argument_parser()
    return run_cli(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
