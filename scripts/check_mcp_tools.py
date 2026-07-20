from __future__ import annotations

import argparse
import asyncio
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


EXPECTED_TOOLS = (
    "list_projects",
    "list_worktrees",
    "get_project_context",
    "get_review_manifest",
    "get_review_bundle_page",
    "get_review_bundle",
    "read_project_file",
    "search_project",
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    return parser.parse_args()


async def _list_tools(url: str) -> set[str]:
    async with streamable_http_client(url) as streams:
        read_stream, write_stream, _ = streams
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.list_tools()
    return {tool.name for tool in result.tools}


def main() -> int:
    args = _parse_args()
    try:
        found = asyncio.run(_list_tools(args.url))
    except Exception:
        print("MCP transport or protocol check failed", file=sys.stderr)
        return 1

    missing = [name for name in EXPECTED_TOOLS if name not in found]
    for name in missing:
        print(f"missing MCP tool: {name}", file=sys.stderr)
    if missing:
        return 1
    print(f"OK: all {len(EXPECTED_TOOLS)} expected tools found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
