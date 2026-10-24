"""Jellybeans MCP server entry point."""

import argparse
import logging
import sys


def main() -> None:
    parser = argparse.ArgumentParser(prog="jellybeans-mcp", description="Jellybeans analytics MCP server")
    parser.add_argument("--transport", choices=["stdio", "http"], help="Overrides MCP_TRANSPORT")
    args = parser.parse_args()

    from jellybeans_mcp.config import get_settings
    from jellybeans_mcp.server import mcp

    settings = get_settings()
    # stdout carries the stdio protocol, so all logging goes to stderr.
    logging.basicConfig(
        stream=sys.stderr, level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    transport = args.transport or settings.mcp_transport
    if transport == "stdio":
        mcp.run("stdio")
    else:
        # Customer tools expose personal data, so HTTP stays off until OAuth is wired in (phase 3).
        sys.exit("HTTP transport is not enabled yet: it will be added together with OAuth.")
