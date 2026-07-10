#!/usr/bin/env python
"""Run the installed Exomem HTTP app without OAuth for transport E2E.

REST remains bearer-authenticated through EXOMEM_REST_API_KEY. OAuth itself is
covered separately; this runner isolates MCP initialization and app lifespan
from external GitHub authorization.
"""

from __future__ import annotations

import argparse

from exomem import server


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    mcp = server.build_server(require_auth=False)
    mcp.run(transport="http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
