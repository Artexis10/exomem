"""Caller-projected, content-free governance inspection handlers."""

from __future__ import annotations

import stat
from pathlib import Path
from typing import Any

from . import egress
from . import policy as policy_module
from .principal import OWNER_AUDIENCE, RequestPrincipal, effective_principal


class InspectionError(RuntimeError):
    def __init__(self, code: str, reason: str) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


def _canonical_vault_path(vault_root: Path, raw_path: object) -> str:
    """Return one existing, non-symlinked vault-relative identity."""
    if not isinstance(raw_path, str) or not raw_path or "\\" in raw_path:
        raise InspectionError("INVALID_INSPECTION_PATH", "path must be canonical")
    candidate = Path(raw_path)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise InspectionError("INVALID_INSPECTION_PATH", "path must be canonical")
    root = Path(vault_root)
    target = root / candidate
    current = root
    try:
        for part in candidate.parts:
            current = current / part
            mode = current.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise InspectionError("INVALID_INSPECTION_PATH", "path must be canonical")
        if not stat.S_ISREG(target.lstat().st_mode):
            raise InspectionError("INVALID_INSPECTION_PATH", "path must be canonical")
        target.resolve(strict=True).relative_to(root.resolve(strict=True))
    except InspectionError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise InspectionError("INVALID_INSPECTION_PATH", "path must be canonical") from None
    return candidate.as_posix()


def inspect_operation(
    vault_root: Path, operation: str, **kwargs: Any
) -> dict[str, Any]:
    pol = policy_module.load(vault_root)
    if pol.empty:
        return {"enabled": False, "rules": [], "scopes": [], "grants": []}
    principal = kwargs.get("principal")
    who = principal if isinstance(principal, RequestPrincipal) else effective_principal()
    owner = who.resolved and who.audience_id == OWNER_AUDIENCE
    requested_audience = str(kwargs.get("audience") or "").strip()
    if operation in {"explain", "simulate"} and not requested_audience:
        raise InspectionError("INVALID_INSPECTION", "audience is required")
    if requested_audience and not owner and requested_audience != who.audience_id:
        raise InspectionError(
            "UNSUPPORTED_INSPECTION_AUDIENCE",
            "non-owner inspection only evaluates the caller's audience",
        )
    audience = requested_audience or who.audience_id
    if operation == "list":
        return {
            "enabled": True,
            "fingerprint": pol.fingerprint if owner else None,
            "scopes": sorted(pol.scopes) if owner else [],
            "rules": sorted(rule.id for rule in pol.rules),
            "grants": sorted(
                [grant.id for grant in pol.grants]
                + [grant.id for grant in pol.release_grants]
            ),
        }
    if pol.blocked:
        raise InspectionError("GOVERNANCE_BLOCKED", "current policy cannot be evaluated")

    cross_audience = audience != who.audience_id
    purpose = (
        kwargs.get("purpose")
        if cross_audience
        else egress._declared_purpose(vault_root, who, kwargs.get("purpose"))
    )

    def projected(rel_path: str):
        return egress._decide_path(
            vault_root,
            rel_path,
            policy=pol,
            audience=audience,
            purpose=purpose,
            grants_hash=egress._grants_hash(pol),
            authorization_session=(None if cross_audience else who.authorization_session_id),
        )

    if operation == "explain":
        rel_path = _canonical_vault_path(vault_root, kwargs.get("path"))
        decision = projected(rel_path)
        level = policy_module.DISCLOSURE_MIN if decision is None else decision.level
        rule_ids = [] if decision is None else list(decision.rule_ids)
        return {
            "enabled": True,
            "effective_ceiling": level,
            "rule_ids": rule_ids,
            "participating_chain": rule_ids,
            **(
                {"release_reason": decision.release_reason}
                if decision is not None and decision.release_reason
                else {}
            ),
        }

    raw_paths = kwargs.get("paths")
    if not isinstance(raw_paths, list) or not all(
        isinstance(path, str) for path in raw_paths
    ):
        raise InspectionError("INVALID_INSPECTION", "paths must be a list of strings")
    canonical_paths = [_canonical_vault_path(vault_root, path) for path in raw_paths]
    decisions = [projected(path) for path in canonical_paths]
    withheld = sum(
        decision is None or decision.level < policy_module.DISCLOSURE_MAX
        for decision in decisions
    )
    return {
        "enabled": True,
        "evaluated_count": len(decisions),
        "withheld_count": withheld,
        "released_count": len(decisions) - withheld,
        "release_reasons": sorted(
            {
                decision.release_reason
                for decision in decisions
                if decision is not None and decision.release_reason
            }
        ),
    }
