"""The Jellybeans MCP server definition (transport-agnostic)."""

from importlib.metadata import version

from mcp.server import MCPServer

from jellybeans_mcp import prompts, resources
from jellybeans_mcp.db import lifespan
from jellybeans_mcp.tools import register_all

INSTRUCTIONS = """\
Read-only analytics for Jellybeans, a kids play area in Sri Lanka.
- Amounts are LKR (rupees, 2 decimals). Dates are YYYY-MM-DD in Asia/Colombo time; ranges are
  inclusive, default to the last 30 days, and can span up to about 10 years.
- For monthly or yearly performance (calendar years, Jan-Dec) use get_period_performance: one call gives
  every month/year with % change vs the previous period and vs last year. Trends allow at most 400 points.
- Revenue = paid, non-test bills by payment date. Visits = non-voided play sessions by check-in;
  group bookings are counted separately.
- Start with get_business_info for packages and prices. Read the jellybeans://guide resource for
  exact definitions.
- Privacy: tools never return phone numbers, emails or customer notes. Customers are identified by
  name and an opaque customer_ref; look customers up by parent name. Never ask users for phone numbers.
  Parent and children's names are personal data: share them only with staff who need them.
"""


def create_server() -> MCPServer:
    mcp = MCPServer(
        "jellybeans",
        title="Jellybeans Play Area Analytics",
        instructions=INSTRUCTIONS,
        version=version("jellybeans-mcp"),
        lifespan=lifespan,
    )
    register_all(mcp)
    resources.register(mcp)
    prompts.register(mcp)
    return mcp


mcp = create_server()
