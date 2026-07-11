"""Acceptance smoke for an installed Exomem wheel (run with that environment's Python)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from importlib.resources import files
from pathlib import Path


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="exomem-studio-wheel-") as raw:
        vault = Path(raw) / "vault"
        subprocess.run(
            [sys.executable, "-m", "exomem", "init", "--vault", str(vault)],
            check=True,
            capture_output=True,
            text=True,
        )
        os.environ.update(
            {
                "EXOMEM_VAULT_PATH": str(vault),
                "EXOMEM_REST_API_KEY": "wheel-acceptance-key",
                "EXOMEM_DISABLE_EMBEDDINGS": "1",
                "EXOMEM_DISABLE_WATCHER": "1",
                "EXOMEM_LOG_DIR": str(Path(raw) / "logs"),
            }
        )
        from starlette.testclient import TestClient

        from exomem import server

        server.load_dotenv = lambda *args, **kwargs: None
        client = TestClient(server.build_server(require_auth=False).http_app())
        auth = {"Authorization": "Bearer wheel-acceptance-key"}
        shell = client.get("/studio/")
        script = client.get("/studio/assets/app.v1.js")
        assert shell.status_code == 200 and script.status_code == 200
        assert client.post("/api/review_memory", json={"mode": "attention"}).status_code == 401

        created = client.post(
            "/api/remember",
            headers=auth,
            json={
                "title": "Installed wheel review target",
                "note_type": "insight",
                "content": "# Installed wheel review target\n\n## Claim\n\nMeasured conclusion.\n",
                "suggestions": False,
            },
        )
        assert created.status_code == 200, created.text
        created_path = created.json()["data"]["path"]
        worklist = client.post(
            "/api/review_memory",
            headers=auth,
            json={"mode": "activation", "limit": 0},
        )
        assert worklist.status_code == 200, worklist.text
        item = next(
            row for row in worklist.json()["data"]["items"] if row["path"] == created_path
        )
        context = client.post(
            "/api/review_item_context",
            headers=auth,
            json={"ref": item["ref"], "expected_fingerprint": item["fingerprint"]},
        )
        assert context.status_code == 200, context.text
        assert context.json()["data"]["target"]["path"] == created_path
        triage = client.post(
            "/api/triage_memory",
            headers=auth,
            json={
                "ref": item["ref"],
                "action": "dismiss",
                "why": "wheel acceptance",
                "expected_fingerprint": item["fingerprint"],
            },
        )
        assert triage.status_code == 200, triage.text
        assert triage.json()["data"]["state"] == "dismissed"

        manifest = json.loads(
            files("exomem").joinpath("studio/manifest.json").read_text(encoding="utf-8")
        )
        print(
            json.dumps(
                {
                    "installed_from": str(files("exomem")),
                    "studio_assets": len(manifest["assets"]),
                    "created_path": created_path,
                    "triage_state": "dismissed",
                }
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
