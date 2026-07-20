from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mcp_contract import EXPECTED_TOOLS, tool_contract_fingerprint


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
        return 1

    expected = set(EXPECTED_TOOLS)
    missing = [name for name in EXPECTED_TOOLS if name not in found]
    unexpected = sorted(found - expected)

    for name in missing:
        print(f"missing MCP tool: {name}", file=sys.stderr)
    for name in unexpected:
        print(f"unexpected MCP tool: {name}", file=sys.stderr)

    if missing or unexpected:
        return 1

    print(f"OK: exact {len(EXPECTED_TOOLS)}-tool contract found")
    print(f"tool_contract_sha256={tool_contract_fingerprint(found)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
