# Jellybeans MCP

Read-only [Model Context Protocol](https://modelcontextprotocol.io) server for Jellybeans kids play area
analytics, backed by MongoDB Atlas. Ask Claude things like *"How did last week compare with the week
before?"* or *"When are our busiest hours on Sundays?"*.

## Setup

```powershell
uv sync
copy .env.example .env   # then fill in MONGODB_URI (use a read-only Atlas user) and MONGODB_DB
uv run pytest            # unit tests + live read-only integration tests
```

## Run (stdio)

```powershell
uv run jellybeans-mcp                    # used by MCP clients; logs go to stderr
uv run mcp dev src/jellybeans_mcp/server.py   # MCP Inspector in the browser
```

**Claude Code**

```powershell
claude mcp add jellybeans -- uv --directory "/path/to/jellybeans-mcp" run jellybeans-mcp
```

**Claude Desktop** (`%APPDATA%\Claude\claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "jellybeans": {
      "command": "uv",
      "args": ["--directory", "/path/to/jellybeans-mcp", "run", "jellybeans-mcp"]
    }
  }
}
```

- Use the absolute project path, with forward slashes (`E:/...`) or escaped backslashes (`E:\\...`).
- If Claude can't find `uv`, put its full path in `command` (run `where uv` on Windows, `which uv` on macOS/Linux).
- Fully quit Claude Desktop (including the tray icon) and reopen it. The server shows up under
  **+ → Connectors → jellybeans**.

## Troubleshooting

- **Server not listed or failing in Claude Desktop:** check the logs in `%APPDATA%\Claude\logs\`
  (`mcp.log` and `mcp-server-jellybeans.log`). On macOS they're in `~/Library/Logs/Claude/`.
- **Test without Claude:** `uv run mcp dev src/jellybeans_mcp/server.py` opens the MCP Inspector, where you
  can list and call tools directly.
- **"MONGODB_URI field required":** `.env` is missing or empty. It's read from the project root, so the server
  works whichever directory the client launches it from.
- **"The database is temporarily unavailable":** Atlas couldn't be reached (network, IP allowlist, paused
  cluster). The details are in the server log. Each tool call logs one line like
  `tool=get_revenue_summary ok duration_ms=412`, and never logs arguments or results.
- **Never use `print()`** in this server: with stdio, stdout carries the protocol messages, so printing breaks
  the connection. Use `logging`, which writes to stderr.

## What it offers

| Tools | |
|---|---|
| Revenue | `get_revenue_summary`, `get_revenue_trend` (day/week/month/year), `get_revenue_breakdown`, `get_product_sales`, `get_cashier_performance` |
| Monthly / yearly | `get_period_performance`: one row per calendar month or year with revenue, bills, children, new customers and % change vs the previous period and vs last year |
| Visits | `get_visit_stats`, `get_peak_hours`, `get_package_performance`, `get_current_occupancy` |
| Customers | `get_customer_insights`, `get_top_customers`, `find_customer` |
| Operations | `get_business_info`, `get_exceptions` |

Resource `jellybeans://guide` explains the definitions. The prompts `daily_summary`, `weekly_report`,
`monthly_report`, `yearly_report` and `compare_periods` build common reports.

Conventions:
- Amounts are LKR, stored as cents and returned as rupees.
- Dates are `YYYY-MM-DD` in Asia/Colombo time. Ranges are inclusive, default to the last 30 days, and can
  span up to about 10 years.
- Years are calendar years (Jan–Dec). Trends return at most 400 points, so use month or year buckets for
  long ranges.
- Revenue counts paid, non-test bills by payment date.

## Data privacy

Everything a tool returns is sent to the LLM, so:

- **Never returned:** phone numbers, emails, customer notes, the business phone and receipt texts.
  Free-text reasons have any phone number or email masked (`redact()` in `tools/common.py`).
- **Phone lookup is disabled:** `find_customer` searches by parent name only, and its description doesn't
  mention phones, so the LLM never asks for one. Phone-number queries are refused without touching the
  database. Set `ENABLE_PHONE_LOOKUP=true` to re-enable it. Even then, numbers are search input only and
  are never returned, but whatever a user types still passes through the LLM.
- **Returned:** parent and children's names, identified by an opaque `customer_ref`; staff names.
- **Never read at all:** `users.passwordHash`, `refreshtokens`, logos.
- `tests/test_tools.py::test_no_contact_details_in_any_output` reads every phone number and email in the
  database and fails if any tool output contains one.

The HTTP transport stays disabled until OAuth 2.1 is in place.

## Layout

```
src/jellybeans_mcp/
  __init__.py   entry point (--transport)
  config.py     settings from .env
  db.py         AsyncMongoClient lifespan
  server.py     MCPServer definition
  tools/        revenue, visits, customers, operations (+ common helpers)
  resources.py  prompts.py
```
