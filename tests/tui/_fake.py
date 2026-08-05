"""Deterministic backend fake for TUI tests and snapshot goldens.

Synthetic, generic content only. Committed snapshot SVGs render from this data
and are scanned by the fail-closed public-artifact privacy gate, so nothing in
here may resemble a real vault, a host filesystem path, or a person.
"""

from __future__ import annotations

import threading
from pathlib import Path

from exomem.tui.backend import (
    AskOutcome,
    BackendError,
    RelationReviewRequired,
    VaultState,
)

SAMPLE_HITS: list[dict] = [
    {
        "path": "Knowledge Base/Notes/Insights/queue-backpressure-needs-explicit-limits.md",
        "type": "insight",
        "title": "Queue backpressure needs explicit limits",
        "updated": "2026-07-20",
    },
    {
        "path": "Knowledge Base/Notes/Research/sample-project/retry-budgets-beat-unbounded-retries.md",
        "type": "research-note",
        "title": "Retry budgets beat unbounded retries",
        "updated": "2026-07-18",
    },
    {
        "path": "Knowledge Base/Sources/2026/07/reading-notes-on-schedulers.md",
        "type": "source",
        "title": "Reading notes on schedulers",
        "updated": "2026-07-11",
    },
]

SAMPLE_PAGE = (
    "# Queue backpressure needs explicit limits\n\n"
    "A bounded queue with an explicit shed policy fails predictably under\n"
    "load; an unbounded queue fails everywhere at once, later.\n"
)

SAMPLE_ATTENTION = {
    "shown": 2,
    "total": 2,
    "all_total": 2,
    "state_summary": {"open": 2},
    "items": [
        {
            "ref": "exomem://review/0000000000000000000000aa",
            "path": "Knowledge Base/Notes/Insights/queue-backpressure-needs-explicit-limits.md",
            "severity": "warning",
            "categories": ["corpus_contradictions"],
            "reasons": [{"detail": "a newer note reaches the opposite conclusion"}],
            "fingerprint": "fp-aa",
        },
        {
            "ref": "exomem://review/0000000000000000000000bb",
            "path": "Knowledge Base/Sources/2026/07/reading-notes-on-schedulers.md",
            "severity": "info",
            "categories": ["unprocessed_source"],
            "reasons": [{"detail": "captured but never compiled into a conclusion"}],
            "fingerprint": "fp-bb",
        },
    ],
}


class FakeBackend:
    """Mirror of `ExomemBackend`'s surface with canned, instant answers."""

    def __init__(
        self,
        *,
        initialized: bool = True,
        warming: bool = False,
        attention: dict | None = None,
        hooks_ok: bool = True,
        checkpoints: list[dict] | None = None,
    ):
        self.initialized = initialized
        self.warming = warming
        self.hooks_ok = hooks_ok
        self.attention_payload = SAMPLE_ATTENTION if attention is None else attention
        self.checkpoints = list(checkpoints or [])
        self.runtime_started = False
        self.calls: list[tuple[str, dict]] = []
        self.remembered: list[dict] = []
        self.captured: list[dict] = []
        self.triaged: list[dict] = []
        self.selected_packs: list[str] = ["technical", "business"]
        self.existing_vaults: set[str] = set()
        #: When true, remember_note demands the governed relation review first.
        self.require_relation_review = False
        self._fail: dict[str, BackendError] = {}
        self._release: threading.Event | None = None

    @property
    def vault_root(self):
        return Path("/data/sample-vault") if self.initialized else None

    # -- test controls ------------------------------------------------- #
    def fail_next(self, method: str, code: str = "OP_ERROR", message: str = "it broke") -> None:
        self._fail[method] = BackendError(code, message, "try again with different input")

    def hold(self) -> threading.Event:
        """Make the next ask block until the returned event is set."""
        self._release = threading.Event()
        return self._release

    def _gate(self, method: str, **kwargs) -> None:
        self.calls.append((method, kwargs))
        error = self._fail.pop(method, None)
        if error is not None:
            raise error

    # -- session -------------------------------------------------------- #
    def resolve_vault(self) -> VaultState:
        self._gate("resolve_vault")
        if self.initialized:
            return VaultState(Path("/data/sample-vault"), True)
        return VaultState(None, False, "no vault configured")

    def adopt_vault_root(self, root: Path) -> VaultState:
        self._gate("adopt_vault_root", root=str(root))
        if str(root) in self.existing_vaults:
            self.initialized = True
            return VaultState(Path(root), True)
        return VaultState(Path(root), False, "folder holds no Knowledge Base yet")

    def start_runtime(self) -> None:
        self.runtime_started = True

    def refresh_caches(self) -> None:
        self._gate("refresh_caches")

    @staticmethod
    def scaffold_file_count() -> int:
        return 28

    def corpus_size(self) -> dict:
        self._gate("corpus_size")
        return {"notes": 132, "sources": 41, "known": self.initialized}

    # -- registry ------------------------------------------------------- #
    def ask(self, query: str, **kwargs) -> AskOutcome:
        self._gate("ask", query=query, **kwargs)
        if self._release is not None:
            released = self._release
            self._release = None
            released.wait(timeout=10)
        if "nothing" in query:
            return AskOutcome(hits=[])
        warming = {"components": ["embeddings", "reranker"], "since_s": 12} if self.warming else None
        return AskOutcome(
            hits=list(SAMPLE_HITS),
            warming=warming,
            pack={"claims": ["bounded queues shed load predictably"]},
        )

    def read_page(self, path: str) -> dict:
        self._gate("read_page", path=path)
        return {"path": path, "body": SAMPLE_PAGE}

    def capture_thought(self, content: str, title: str, *, source_type: str = "other") -> dict:
        self._gate("capture_thought", title=title)
        record = {"content": content, "title": title, "source_type": source_type}
        self.captured.append(record)
        return {"path": f"Knowledge Base/Sources/2026/07/{title.lower().replace(' ', '-')}.md"}

    def remember_note(self, content: str, title: str, *, note_type: str = "insight") -> dict:
        self._gate("remember_note", title=title)
        if self.require_relation_review and "[[" not in content:
            # Mirrors the real validate-first path: the draft exists, but the
            # governed relation review has to be answered by a human.
            raise RelationReviewRequired(
                {
                    "draft_id": "draft-1",
                    "draft_hash": "hash-1",
                    "draft_token": "token-1",
                    "_raw_args": {"content": content, "title": title, "note_type": note_type},
                }
            )
        record = {"content": content, "title": title, "note_type": note_type, "unlinked": False}
        self.remembered.append(record)
        return {"path": f"Knowledge Base/Notes/Insights/{title.lower().replace(' ', '-')}.md"}

    def attention(self, *, limit: int = 25, state: str = "open") -> dict:
        self._gate("attention", limit=limit, state=state)
        return dict(self.attention_payload)

    def item_context(self, ref: str) -> dict:
        # Mirrors the real review-context envelope: target page + related set.
        self._gate("item_context", ref=ref)
        return {
            "ref": ref,
            "target": {
                "path": SAMPLE_HITS[0]["path"],
                "title": SAMPLE_HITS[0]["title"],
                "body": SAMPLE_PAGE,
                "mtime": "2026-07-31",
            },
            "related": {"items": [{"path": SAMPLE_HITS[1]["path"]}]},
        }

    def triage(self, ref: str, action: str, **kwargs) -> dict:
        self._gate("triage", ref=ref, action=action, **kwargs)
        self.triaged.append({"ref": ref, "action": action, **kwargs})
        return {"state": f"{action}ed", "ref": ref}

    def adopt_scan(self, folder) -> dict:
        self._gate("adopt_scan", folder=str(folder))
        return {
            "mode": "scan-only",
            "summary": {"totals": {"files": 24, "markdown": 18, "dirs": 5}},
            "governance": {"kb_present": False},
            "pack_suggestions": [
                {"id": "technical", "name": "Technical", "score": 3, "matched_signals": ["code"]},
            ],
            "next_actions": [
                {"action": "save-manifest", "status": "available", "description": "record this scan"},
            ],
        }

    def adopt_write(self, folder, mode: str, **extra) -> dict:
        self._gate("adopt_write", folder=str(folder), mode=mode)
        return {"mode": mode, "summary": {"totals": {"files": 24}}}

    # -- supported non-registry services -------------------------------- #
    def overview(self) -> dict:
        self._gate("overview")
        readiness = {"warming": self.warming}
        if self.warming:
            readiness["pending"] = ["embeddings", "reranker"]
        return {
            "mode": {"ok": True, "data": {"mode": "normal", "config_path": "/data/config.json"}},
            "packs": {"ok": True, "data": {"selected": list(self.selected_packs)}},
            "readiness": {"ok": True, "data": readiness},
            "hooks": {"ok": True, "data": {"success": self.hooks_ok}},
            "attention": {"ok": True, "data": dict(self.attention_payload)},
            "corpus": {"ok": True, "data": self.corpus_size()},
        }

    def mode(self) -> dict:
        self._gate("mode")
        return {"mode": "normal", "config_path": "/data/config.json"}

    def set_mode(self, value: str) -> str:
        self._gate("set_mode", value=value)
        return "/data/config.json"

    def readiness(self) -> dict:
        self._gate("readiness")
        return {"warming": self.warming}

    def packs_state(self) -> dict:
        self._gate("packs_state")
        return {
            "catalog": [
                {"id": "technical", "name": "Technical", "description": "software and systems work"},
                {"id": "business", "name": "Business", "description": "commercial decisions and plans"},
                {"id": "creative", "name": "Creative", "description": "writing and making things"},
            ],
            "selected": list(self.selected_packs),
            "manifest_present": True,
        }

    def apply_packs(self, pack_ids: list[str]) -> dict:
        self._gate("apply_packs", pack_ids=list(pack_ids))
        self.selected_packs = list(pack_ids)
        return {"selected_pack_ids": list(pack_ids)}

    def doctor_report(self) -> dict:
        self._gate("doctor_report")
        return {
            "success": True,
            "checks": [
                {"name": "python", "status": "ok", "detail": "3.12"},
                {"name": "vault", "status": "ok", "detail": "/data/sample-vault"},
            ],
        }

    def install_report(self) -> dict:
        self._gate("install_report")
        return {"version": "0.0.0-test", "install_source": "test", "local_profile": "lean"}

    def resource_report(self) -> dict:
        self._gate("resource_report")
        return {"mode": "normal", "models": {"module_loaded": False}, "media": "off", "cuda": "none"}

    def hook_status(self) -> dict:
        self._gate("hook_status")
        return {"success": self.hooks_ok}

    def init_vault(self, folder) -> dict:
        self._gate("init_vault", folder=str(folder))
        self.existing_vaults.add(str(folder))
        self.initialized = True
        return {"kb": str(Path(folder) / "Knowledge Base"), "created": ["_Schema/SKILL.md"]}

    def continuations(self) -> list[dict]:
        self._gate("continuations")
        return list(self.checkpoints)

    def continuation_packet(self, entry: dict) -> str:
        self._gate("continuation_packet", session=entry.get("session"))
        return (
            "Continuation checkpoint (sample)\n"
            f"client: {entry.get('client')}\nsession: {entry.get('session')}\n"
            "Reopen cited artifacts and continue from evidence."
        )

    def commit_unlinked_note(self, draft: dict) -> dict:
        self._gate("commit_unlinked_note")
        raw = dict(draft.get("_raw_args") or {})
        self.remembered.append(
            {
                "content": raw.get("content", ""),
                "title": raw.get("title", "untitled"),
                "note_type": raw.get("note_type", "insight"),
                "unlinked": True,
            }
        )
        title = str(raw.get("title", "untitled"))
        return {"path": f"Knowledge Base/Notes/Insights/{title.lower().replace(' ', '-')}.md"}
