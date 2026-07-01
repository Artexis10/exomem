"""Live smoke test for the /upload route on the running service (localhost).

Proves the route is deployed and the bearer-token gate behaves correctly,
WITHOUT writing anything to the vault — the good-token case stops at the
"file is required" check (400), which is reached only *after* auth passes.

Run after restarting the service:

    uv run python scripts/smoke-upload.py
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import urllib.error
import urllib.request
from pathlib import Path

URL = "http://127.0.0.1:8765/upload"
ENV = Path(__file__).resolve().parents[1] / ".env"


def _token() -> str:
    for line in ENV.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("KB_MCP_UPLOAD_TOKEN="):
            tok = line.split("=", 1)[1].strip()
            if tok:
                return tok
    raise SystemExit("KB_MCP_UPLOAD_TOKEN not set in .env — run scripts/set-upload-token.py first")


def _post(headers: dict) -> int:
    req = urllib.request.Request(URL, data=b"", method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def _upload(token: str, *, scope: str, category: str, filename: str, data: bytes) -> tuple[int, str]:
    """Real multipart upload (stdlib only). Returns (status, body)."""
    b = "----kbmcpsmoke"
    parts = [
        f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n".encode() + data + b"\r\n",
        f'--{b}\r\nContent-Disposition: form-data; name="scope"\r\n\r\n{scope}\r\n'.encode(),
        f'--{b}\r\nContent-Disposition: form-data; name="category"\r\n\r\n{category}\r\n'.encode(),
        f"--{b}--\r\n".encode(),
    ]
    req = urllib.request.Request(
        URL,
        data=b"".join(parts),
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": f"multipart/form-data; boundary={b}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="do a REAL upload into Evidence/_smoketest/")
    ap.add_argument("--minted", action="store_true", help="verify a freshly-minted short-lived token is accepted (no write)")
    args = ap.parse_args()
    tok = _token()

    if args.minted:
        from kb_mcp import upload_tokens

        valid = _post({"Authorization": f"Bearer {upload_tokens.mint(tok)}"})
        expired = _post({"Authorization": f"Bearer {upload_tokens.mint(tok, ttl=-10)}"})
        ok = valid == 400 and expired == 401
        print(f"  [{'PASS' if valid == 400 else f'FAIL got {valid}'}] valid minted token   -> expect 400 (accepted, no file)")
        print(f"  [{'PASS' if expired == 401 else f'FAIL got {expired}'}] expired minted token -> expect 401 (rejected)")
        print("OK — live service accepts valid minted tokens, rejects expired ones." if ok else "FAILED.")
        raise SystemExit(0 if ok else 1)

    if args.write:
        from kb_mcp import upload_tokens

        ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        data = f"exomem /upload smoke test {ts}\n".encode()
        # Use a short-lived MINTED token — mirrors the hands-off flow exactly.
        status, body = _upload(
            upload_tokens.mint(tok),
            scope="_smoketest",
            category="_smoketest",
            filename=f"smoke-{ts}.txt",
            data=data,
        )
        print(f"POST /upload (real file) -> {status}")
        print(body)
        if status == 201:
            print("PATH=" + json.loads(body).get("path", ""))
        raise SystemExit(0 if status == 201 else 1)

    checks = [
        ("no auth                  -> expect 401", _post({}), 401),
        ("bad token                -> expect 401", _post({"Authorization": "Bearer wrong"}), 401),
        ("good token, no file part -> expect 400", _post({"Authorization": f"Bearer {tok}"}), 400),
    ]
    ok = True
    for label, got, want in checks:
        passed = got == want
        ok = ok and passed
        print(f"  [{'PASS' if passed else f'FAIL got {got}'}] {label}")
    print(
        "OK — route live, bearer gate enforced end-to-end, zero vault writes."
        if ok
        else "FAILED — see codes above."
    )
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
