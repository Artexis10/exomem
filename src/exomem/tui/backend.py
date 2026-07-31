"""Thin synchronous facade between TUI screens and Exomem's product services.

Every knowledge read/write goes through `product_invoke` — the same seam the
CLI drives — so TUI semantics cannot drift from CLI/MCP/REST. The handful of
non-registry calls here (doctor, resource status, readiness, install info,
hook checks, compute mode, pack selection) are existing supported service
functions; none of them writes vault content.

All methods are synchronous and safe to call from worker threads: the ambient
surface/principal bindings live inside the invocation seam itself. Failures
normalize to `BackendError`, carrying the shared envelope's code, message, and
remediation so screens render actionable errors instead of tracebacks.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import cli_ops

_LEAN_FALLBACK_VARS = (
    "EXOMEM_DISABLE_EMBEDDINGS",
    "EXOMEM_DISABLE_RANKING",
    "EXOMEM_DISABLE_CLIP",
)


class BackendError(Exception):
    """A structured service failure: stable code, message, remediation."""

    def __init__(self, code: str, message: str, remediation: str | None = None):
        self.code = code
        self.message = message
        self.remediation = remediation
        super().__init__(f"{code}: {message}")

    @classmethod
    def from_exception(cls, exc: Exception) -> BackendError:
        err = cli_ops.error_dict(exc)
        return cls(
            str(err.get("code") or "INTERNAL"),
            str(err.get("message") or exc),
            err.get("remediation"),
        )


@dataclass(frozen=True)
class VaultState:
    """Where this session points and whether it is a usable vault yet."""

    root: Path | None
    initialized: bool
    detail: str = ""


@dataclass
class AskOutcome:
    """Normalized retrieval outcome: hits plus honesty markers."""

    hits: list[dict] = field(default_factory=list)
    warming: dict | None = None
    degraded: list[str] = field(default_factory=list)
    pack: dict | None = None
    raw: Any = None


def ensure_semantic_unit(content: str, title: str) -> str:
    """Guarantee the governed minimum: one semantic unit per compiled note.

    A compiled note needs at least one unit (an `## Observations` bullet or a
    rich governed heading). Friendly capture must not require users to know
    that grammar, so when a draft has neither, one compact observation
    restating the title is appended — visible in the written file, never
    silently invented content.
    """
    lowered = content.lower()
    if "## observations" in lowered or lowered.lstrip().startswith("## "):
        return content
    if "\n## " in content:
        return content
    summary = title.strip().rstrip(".")
    return f"{content.rstrip()}\n\n## Observations\n- [insight] {summary}\n"


def _normalize_ask(result: Any) -> AskOutcome:
    if isinstance(result, list):
        return AskOutcome(hits=list(result), raw=result)
    if isinstance(result, dict):
        hits = result.get("hits")
        if hits is None:
            hits = result.get("result")
        degraded = result.get("degraded") or []
        if isinstance(degraded, str):
            degraded = [degraded]
        return AskOutcome(
            hits=list(hits or []),
            warming=result.get("warming"),
            degraded=[str(item) for item in degraded],
            pack=result.get("pack"),
            raw=result,
        )
    return AskOutcome(raw=result)


class ExomemBackend:
    """The real backend: one instance per TUI session."""

    def __init__(self, vault_override: str | None = None):
        self._vault_override = vault_override
        self._vault: Path | None = None

    # ------------------------------------------------------------------ #
    # Session / runtime
    # ------------------------------------------------------------------ #
    def resolve_vault(self) -> VaultState:
        """Resolve the session vault without raising; first-run detector."""
        from .. import vault as vault_module

        candidate = self._vault_override or os.environ.get("EXOMEM_VAULT_PATH")
        if not candidate:
            return VaultState(None, False, "no vault configured")
        root = Path(candidate).expanduser()
        if vault_module._is_vault(root):
            self._vault = root
            return VaultState(root, True)
        if root.is_dir():
            return VaultState(root, False, "folder exists but holds no Knowledge Base yet")
        return VaultState(root, False, "path does not exist")

    def adopt_vault_root(self, root: Path) -> VaultState:
        """Adopt a vault root chosen during onboarding for the whole process."""
        os.environ["EXOMEM_VAULT_PATH"] = str(root)
        self._vault_override = str(root)
        return self.resolve_vault()

    def start_runtime(self) -> None:
        """Lean-search fallback + background warm; both soft-fail."""
        self._apply_lean_fallback()
        if self._vault is None:
            return
        try:
            from .. import warmup

            warmup.start_background(self._vault)
        except Exception:  # noqa: BLE001 — warm-up must never block the UI
            pass

    @staticmethod
    def _apply_lean_fallback() -> None:
        import importlib.util

        def available(name: str) -> bool:
            try:
                return importlib.util.find_spec(name) is not None
            except (ImportError, ValueError):
                return False

        if not (available("torch") and available("sentence_transformers")):
            for name in _LEAN_FALLBACK_VARS:
                os.environ.setdefault(name, "1")

    def refresh_caches(self) -> None:
        """Drop the long-lived in-process caches (explicit Refresh action)."""
        from .. import activation_manifest, embeddings, find, semantic_contract

        find.clear_cache()
        embeddings.clear_embedding_indexes()
        semantic_contract.reset_corpus_context_cache()
        activation_manifest.reset_manifest_cache()

    # ------------------------------------------------------------------ #
    # Registry commands (via the shared seam)
    # ------------------------------------------------------------------ #
    def _call(self, op: str, raw: dict[str, Any]) -> Any:
        from .. import product_invoke

        try:
            return product_invoke.invoke_product(op, raw, vault_root=self._vault)
        except (cli_ops.OpError, ValueError, TypeError, RuntimeError) as exc:
            raise BackendError.from_exception(exc) from exc

    def ask(
        self,
        query: str,
        *,
        limit: int = 15,
        deep: bool = False,
        projects: list[str] | None = None,
        types: list[str] | None = None,
        prefer_active: bool = True,
    ) -> AskOutcome:
        raw: dict[str, Any] = {
            "query": query,
            "limit": limit,
            "prefer_active": prefer_active,
        }
        if deep:
            raw["deep"] = True
        if projects:
            raw["projects"] = projects
        if types:
            raw["types"] = types
        return _normalize_ask(self._call("ask_memory", raw))

    def read_page(self, path: str) -> dict:
        result = self._call("read_memory", {"path": path})
        return result if isinstance(result, dict) else {"body": str(result)}

    def capture_thought(
        self, content: str, title: str, *, source_type: str = "other"
    ) -> dict:
        return self._call(
            "capture_source",
            {"content": content, "title": title, "source_type": source_type},
        )

    def remember_note(
        self, content: str, title: str, *, note_type: str = "insight"
    ) -> dict:
        raw = {
            "content": ensure_semantic_unit(content, title),
            "title": title,
            "note_type": note_type,
        }
        try:
            return self._call("remember", raw)
        except BackendError as error:
            if error.code != "SEMANTIC_CONTRACT_BLOCKED":
                raise
        # Governed relation review: a new compiled page without a qualifying
        # typed relation needs an explicit disposition. Validate to obtain the
        # immutable draft identity, then commit it unchanged as reviewed-none —
        # the user's confirmation IS the review, and the reason lands in the
        # audit trail (the same proposal-first pattern the Studio uses).
        draft = self._call("remember", {**raw, "validate_only": True})
        if not isinstance(draft, dict) or not draft.get("draft_id"):
            raise BackendError(
                "SEMANTIC_CONTRACT_BLOCKED",
                "draft validation did not return a committable draft",
            )
        review_hash = draft.get("relation_review_hash") or draft.get("draft_hash")
        return self._call(
            "remember",
            {
                **raw,
                "draft_id": draft["draft_id"],
                "draft_hash": draft["draft_hash"],
                "draft_token": draft["draft_token"],
                "relation_disposition": "reviewed_none",
                "relation_review_hash": review_hash,
                "relation_review_reason": (
                    "tui write-back: user confirmed saving without selecting typed relations"
                ),
            },
        )

    def attention(self, *, limit: int = 25, state: str = "open") -> dict:
        result = self._call(
            "review_memory", {"mode": "attention", "limit": limit, "state": state}
        )
        return result if isinstance(result, dict) else {"items": []}

    def item_context(self, ref: str) -> dict:
        result = self._call("review_item_context", {"ref": ref})
        return result if isinstance(result, dict) else {}

    def triage(
        self,
        ref: str,
        action: str,
        *,
        until: str | None = None,
        why: str | None = None,
        expected_fingerprint: str | None = None,
    ) -> dict:
        raw: dict[str, Any] = {"ref": ref, "action": action}
        if until:
            raw["until"] = until
        if why:
            raw["why"] = why
        if expected_fingerprint:
            raw["expected_fingerprint"] = expected_fingerprint
        result = self._call("triage_memory", raw)
        return result if isinstance(result, dict) else {}

    def adopt_scan(self, folder: Path | str) -> dict:
        from .. import product_invoke

        try:
            result = product_invoke.invoke_product(
                "adopt_vault", {"mode": "scan-only"}, vault_root=Path(folder)
            )
        except (cli_ops.OpError, ValueError, TypeError, RuntimeError) as exc:
            raise BackendError.from_exception(exc) from exc
        return result if isinstance(result, dict) else {}

    def adopt_write(self, folder: Path | str, mode: str, **extra: Any) -> dict:
        if mode not in {"save-manifest", "copy-as-sources"}:
            raise BackendError(
                "UNSUPPORTED_ADOPT_MODE", f"adopt mode {mode!r} is not offered by the TUI"
            )
        from .. import product_invoke

        try:
            result = product_invoke.invoke_product(
                "adopt_vault", {"mode": mode, **extra}, vault_root=Path(folder)
            )
        except (cli_ops.OpError, ValueError, TypeError, RuntimeError) as exc:
            raise BackendError.from_exception(exc) from exc
        return result if isinstance(result, dict) else {}

    # ------------------------------------------------------------------ #
    # Supported non-registry services (read-only or config-only)
    # ------------------------------------------------------------------ #
    def overview(self) -> dict:
        """Home dashboard data; each section degrades independently."""
        sections: dict[str, Any] = {}

        def gather(name: str, fn) -> None:
            try:
                sections[name] = {"ok": True, "data": fn()}
            except BackendError as exc:
                sections[name] = {
                    "ok": False,
                    "error": {"code": exc.code, "message": exc.message},
                }
            except Exception as exc:  # noqa: BLE001 — one section must not kill Home
                sections[name] = {"ok": False, "error": {"code": "INTERNAL", "message": str(exc)}}

        gather("mode", self.mode)
        gather("packs", self.packs_state)
        gather("readiness", self.readiness)
        gather("hooks", self.hook_status)
        gather("attention", lambda: self.attention(limit=5))
        return sections

    def mode(self) -> dict:
        from .. import mode as mode_module

        policy = mode_module.resolved()
        policy["config_path"] = str(mode_module.config_path())
        return policy

    def set_mode(self, value: str) -> str:
        from .. import mode as mode_module

        try:
            path = mode_module.write_mode(value)
        except ValueError as exc:
            raise BackendError.from_exception(exc) from exc
        return str(path)

    def readiness(self) -> dict:
        from .. import readiness

        return readiness.snapshot()

    def packs_state(self) -> dict:
        from .. import knowledge_packs

        catalog = knowledge_packs.list_builtin_packs()
        root = self._vault
        if root is None:
            return {"catalog": catalog, "selected": [], "manifest_present": False}
        state = knowledge_packs.selected_pack_state(root)
        return {
            "catalog": catalog,
            "selected": list(state.get("selected_pack_ids") or []),
            "manifest_present": bool(state.get("manifest_present")),
            "source": state.get("source"),
            "warnings": list(state.get("warnings") or []),
        }

    def apply_packs(self, pack_ids: list[str]) -> dict:
        from .. import knowledge_packs

        if self._vault is None:
            raise BackendError(
                "KB_NOT_INITIALIZED", "choose or create a vault before selecting packs"
            )
        try:
            return knowledge_packs.write_selected_packs(
                self._vault, list(pack_ids), source="tui"
            )
        except (ValueError, RuntimeError, OSError) as exc:
            raise BackendError.from_exception(exc) from exc

    def doctor_report(self) -> dict:
        from .. import doctor as doctor_module

        report = doctor_module.doctor(
            vault=str(self._vault) if self._vault else None
        )
        payload = report.as_dict()
        payload["success"] = bool(report.success)
        return payload

    def install_report(self) -> dict:
        from .. import install_info

        return install_info.report()

    def resource_report(self) -> dict:
        from .. import resource_status

        return resource_status.collect(self._vault)

    def hook_status(self) -> dict:
        from .. import install_hook

        return install_hook.check_hooks(clients=install_hook.SUPPORTED_CLIENTS)

    def init_vault(self, folder: Path | str) -> dict:
        from .. import init as init_module

        try:
            return init_module.init_vault(Path(folder))
        except (FileExistsError, OSError, ValueError) as exc:
            raise BackendError.from_exception(exc) from exc

    def continuations(self) -> list[dict]:
        from .. import install_hook

        reader = getattr(install_hook, "list_continuation_checkpoints", None)
        if reader is None:
            return []
        try:
            return list(reader())
        except Exception as exc:  # noqa: BLE001 — diagnostics must not crash the screen
            raise BackendError("CONTINUATION_READ_FAILED", str(exc)) from exc
