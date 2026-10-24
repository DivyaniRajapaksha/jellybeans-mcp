from mcp.server import MCPServer

from jellybeans_mcp.tools import customers, operations, performance, revenue, visits


def register_all(mcp: MCPServer) -> None:
    for module in (revenue, performance, visits, customers, operations):
        module.register(mcp)
