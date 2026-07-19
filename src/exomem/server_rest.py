"""Personal REST facade generated from the command registry."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import typing
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from pydantic import TypeAdapter
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import cf_access, cli_ops, upload_tokens
from . import commands as commands_module
from .command_surface import canonical_request_id
from .server_transfer import TransferConfig


class RestJSONResponse(JSONResponse):
    """JSONResponse that renders frontmatter dates as ISO-like strings."""

    def render(self, content) -> bytes:  # noqa: ANN001
        return json.dumps(
            content, ensure_ascii=False, allow_nan=False, default=str
        ).encode("utf-8")


_OPENAPI_TYPES = {
    "str": {"type": "string"},
    "int": {"type": "integer"},
    "bool": {"type": "boolean"},
    "list[str]": {"type": "array", "items": {"type": "string"}},
    "dict": {"type": "object"},
    "json": {},
}

_OPENAPI_OUTCOME_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string"},
        "committed": {"type": "boolean"},
        "incomplete": {"type": "boolean"},
        "affected_count": {"type": "integer", "minimum": 0},
        "targets": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 16,
        },
        "omitted_target_count": {"type": "integer", "minimum": 0},
    },
    "required": [
        "kind",
        "committed",
        "incomplete",
        "affected_count",
        "targets",
        "omitted_target_count",
    ],
    "additionalProperties": False,
}
_OPENAPI_MUTATION_HOLDER_SCHEMA = {
    "type": "object",
    "properties": {
        "state": {"const": "held"},
        "request_id": {"type": "string"},
        "operation": {"type": "string"},
        "holder_kind": {"type": "string"},
        "age_seconds": {"type": "number", "minimum": 0},
        "overdue": {"type": "boolean"},
    },
    "required": [
        "state",
        "request_id",
        "operation",
        "holder_kind",
        "age_seconds",
        "overdue",
    ],
    "additionalProperties": False,
}
_OPENAPI_ERROR_SCHEMA = {
    "type": "object",
    "properties": {
        "code": {"type": "string"},
        "message": {"type": "string"},
        "remediation": {"type": ["string", "null"]},
        "outcome": _OPENAPI_OUTCOME_SCHEMA,
        "ok": {"type": "boolean"},
        "error_code": {"type": "string"},
        "status": {"type": "string"},
        "committed": {"type": ["boolean", "null"]},
        "retry_after_ms": {"type": "integer", "minimum": 0},
        "holder": _OPENAPI_MUTATION_HOLDER_SCHEMA,
        "request_id": {"type": "string", "format": "uuid"},
        "receipt_id": {"type": ["string", "null"]},
        "idempotency_key": {"type": "string"},
    },
    "required": ["code", "message", "remediation"],
    "additionalProperties": False,
}
_OPENAPI_ERROR_ENVELOPE_SCHEMA = {
    "type": "object",
    "properties": {
        "success": {"const": False},
        "error": {"$ref": "#/components/schemas/Error"},
    },
    "required": ["success", "error"],
    "additionalProperties": False,
}
_OPENAPI_ERROR_RESPONSE = {
    "description": (
        "{success: false, error: {code, message, remediation, terminal-state fields?}}"
    ),
    "content": {
        "application/json": {"schema": {"$ref": "#/components/schemas/ErrorEnvelope"}}
    },
}


def _openapi_success_schema(
    command: commands_module.Command,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Derive a REST success envelope from the command's public return type."""
    try:
        annotation = typing.get_type_hints(command.leaf).get("return", Any)
        result_schema = TypeAdapter(annotation).json_schema(
            ref_template=(
                f"#/components/schemas/{command.name}_" + "{model}"
            )
        )
    except Exception:  # noqa: BLE001 - undocumented legacy returns stay generic
        result_schema = {}
    definitions = result_schema.pop("$defs", {})
    components = {
        f"{command.name}_{name}": schema for name, schema in definitions.items()
    }
    return (
        {
            "type": "object",
            "properties": {
                "success": {"const": True},
                "data": result_schema,
            },
            "required": ["success", "data"],
            "additionalProperties": False,
        },
        components,
    )


def register_rest_facade(
    mcp_app: FastMCP,
    *,
    vault_root: Path,
    source_schema: Any,
    transfer_config: TransferConfig,
) -> bool:
    """Register /api routes and return whether Tier 2 is exposed."""
    rest_api_key = os.environ.get("EXOMEM_REST_API_KEY", "").strip() or None
    rest_enabled = rest_api_key is not None
    expose_tier2 = not os.environ.get("EXOMEM_DISABLE_TIER2")
    rest_commands = commands_module.product_commands_for("rest", expose_tier2=expose_tier2)

    def _rest_principal(request: Request) -> tuple[bool, str | None]:
        if rest_api_key is not None:
            header = request.headers.get("authorization", "")
            if header.startswith("Bearer "):
                presented = header[len("Bearer ") :].strip()
                if secrets.compare_digest(presented, rest_api_key):
                    return True, None
                if upload_tokens.verify(presented, rest_api_key, scope="rest"):
                    return True, None
        if transfer_config.cf_jwks is not None:
            claims = cf_access.verified_claims(
                request.headers.get("cf-access-jwt-assertion"),
                jwks_client=transfer_config.cf_jwks,
                team_domain=transfer_config.cf_team,
                audience=transfer_config.cf_aud,
            )
            if claims is not None:
                subject = str(claims.get("sub") or claims.get("email") or "").strip()
                issuer = str(claims.get("iss") or "").strip()
                if subject and issuer:
                    digest = hashlib.sha256(f"{issuer}\0{subject}".encode()).hexdigest()
                    return True, f"cf-access:{digest}"
        return False, None

    def _rest_err(
        code: str, message: str, status: int, remediation: str | None = None
    ) -> JSONResponse:
        return RestJSONResponse(
            cli_ops.envelope(
                False, error={"code": code, "message": message, "remediation": remediation}
            ),
            status_code=status,
        )

    def _rest_gate(request: Request) -> tuple[JSONResponse | None, str | None]:
        if not rest_enabled:
            return (
                _rest_err(
                    "REST_DISABLED",
                    "REST API is off: set EXOMEM_REST_API_KEY to enable the /api/* facade",
                    503,
                ),
                None,
            )
        authorized, principal_scope = _rest_principal(request)
        if not authorized:
            return (
                _rest_err("UNAUTHORIZED", "missing or invalid REST API key", 401),
                None,
            )
        return None, principal_scope

    async def _rest_body(request: Request) -> dict | None:
        try:
            raw = await request.body()
        except Exception:  # noqa: BLE001
            return {}
        if not raw or not raw.strip():
            return {}
        try:
            data = json.loads(raw)
        except Exception:  # noqa: BLE001
            return None
        return data if isinstance(data, dict) else None

    def _register_rest(cmd: commands_module.Command) -> None:
        @mcp_app.custom_route(f"/api/{cmd.name}", methods=["POST"])
        async def _handler(
            request: Request, _cmd: commands_module.Command = cmd
        ) -> JSONResponse:
            gate, principal_scope = _rest_gate(request)
            if gate is not None:
                return gate
            body = await _rest_body(request)
            if body is None:
                return _rest_err("INVALID_BODY", "request body must be a JSON object", 400)
            try:
                kwargs = cli_ops.coerce(
                    _cmd.params, body, guarded_fields=_cmd.guarded_fields, tool=_cmd.name
                )
                injected = (vault_root, source_schema) if _cmd.needs_schema else (vault_root,)
                from .writer_lease import invoke_command

                result = await run_in_threadpool(
                    invoke_command,
                    _cmd,
                    *injected,
                    idempotency_key=request.headers.get("idempotency-key"),
                    idempotency_principal_scope=principal_scope,
                    mutation_request_id=canonical_request_id(
                        request.headers.get("x-exomem-request-id")
                    ),
                    **kwargs,
                )
            except (cli_ops.OpError, ValueError, TypeError) as exc:
                err = cli_ops.error_dict(exc)
                return RestJSONResponse(
                    cli_ops.envelope(False, error=err),
                    status_code=cli_ops.http_status_for(err["code"]),
                )
            return RestJSONResponse(cli_ops.envelope(True, data=result))

        _handler.__name__ = f"_api_{cmd.name}"

    for cmd in rest_commands:
        _register_rest(cmd)

    @mcp_app.custom_route("/api/openapi.json", methods=["GET"])
    async def _api_openapi(request: Request) -> JSONResponse:  # noqa: ARG001
        if not rest_enabled:
            return _rest_err("REST_DISABLED", "set EXOMEM_REST_API_KEY to enable", 503)
        paths: dict = {}
        response_schemas: dict[str, Any] = {}
        for cmd in rest_commands:
            properties: dict = {}
            required: list[str] = []
            for prm in cmd.params:
                schema_obj = dict(_OPENAPI_TYPES.get(prm.type, {}))
                if prm.help:
                    schema_obj["description"] = prm.help
                if prm.choices:
                    schema_obj["enum"] = list(prm.choices)
                properties[prm.name] = schema_obj
                if prm.required:
                    required.append(prm.name)
            request_schema: dict = {"type": "object", "properties": properties}
            if required:
                request_schema["required"] = required
            summary = (cmd.description or cmd.name).strip().splitlines()[0]
            success_schema, command_schemas = _openapi_success_schema(cmd)
            response_schemas.update(command_schemas)
            paths[f"/api/{cmd.name}"] = {
                "post": {
                    "operationId": cmd.name,
                    "summary": summary,
                    "security": [{"bearerAuth": []}],
                    "requestBody": {
                        "content": {"application/json": {"schema": request_schema}}
                    },
                    "responses": {
                        "200": {
                            "description": "{success: true, data: ...}",
                            "content": {
                                "application/json": {"schema": success_schema}
                            },
                        },
                        "400": _OPENAPI_ERROR_RESPONSE,
                        "409": _OPENAPI_ERROR_RESPONSE,
                        "401": {"description": "missing/invalid API key"},
                        "503": {"description": "REST API disabled"},
                    },
                }
            }
        return JSONResponse(
            {
                "openapi": "3.1.0",
                "info": {"title": "exomem personal REST facade", "version": "1.0.0"},
                "components": {
                    "securitySchemes": {
                        "bearerAuth": {"type": "http", "scheme": "bearer"}
                    },
                    "schemas": {
                        "Error": _OPENAPI_ERROR_SCHEMA,
                        "ErrorEnvelope": _OPENAPI_ERROR_ENVELOPE_SCHEMA,
                        **response_schemas,
                    },
                },
                "paths": paths,
            }
        )

    return expose_tier2
