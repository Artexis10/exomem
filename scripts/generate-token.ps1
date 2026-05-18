# Mint a long random bearer token for KB_MCP_BEARER_TOKEN.
# Usage:
#   pwsh -File scripts/generate-token.ps1
# Store the printed value in your password manager AND paste it into both:
#   - the kb-mcp .env file as KB_MCP_BEARER_TOKEN=...
#   - the claude.ai connector config under the Authorization header as
#     "Bearer <token>".

$python = if (Test-Path .venv/Scripts/python.exe) {
    ".\.venv\Scripts\python.exe"
} else {
    "python"
}

& $python -c "import secrets; print(secrets.token_urlsafe(32))"
