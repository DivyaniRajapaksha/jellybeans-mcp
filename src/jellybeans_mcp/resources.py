"""Static context for clients: what the data means and how figures are calculated."""

from mcp.server import MCPServer

DATA_GUIDE = """\
# Jellybeans data guide

Jellybeans is a kids play area. Staff check children in at a cashier POS, children play on a
timed package, and the parent pays a bill at check-out.

## Definitions used by every tool
- **Currency**: LKR. All amounts returned by tools are rupees with 2 decimals.
- **Dates**: calendar days in the business timezone (Asia/Colombo). Periods are inclusive,
  default to the last 30 days ending today, and can span up to about 10 years.
- **Months and years**: calendar months and calendar years (January-December). get_period_performance
  snaps dates to whole months/years and flags the current one as `partial`.
- **Revenue (net)**: grand total of bills with status PAID that are not test bills, by payment date.
- **Gross sales**: bill subtotals before discounts. Line totals per package/product are also
  before bill-level discounts.
- **Visits**: play sessions (excluding VOIDED and test sessions) by check-in time. One session can
  cover several children (siblings). Group bookings (schools, parties) are billed as GROUP lines
  and have no play session; get_visit_stats adds them separately.
- **Walk-in**: a session with no linked customer record.
- **Bill change**: `balance` on a bill is change handed back, not money owed.

## Privacy
- Phone numbers, emails and customer notes are never returned by any tool. Free-text reasons
  (cancellations, voids, write-offs) have any phone number or email masked.
- Customers are identified by parent name plus `customer_ref` (an opaque ID); search by parent name.
- Parent and children's names are personal data; keep them out of reports unless needed.

## Collections (MongoDB)
- bills: one per payment; items[] lines of kind PLAY, GROUP or PRODUCT.
- playsessions: one per check-in; check-in/out times, package, billed minutes, children.
- customers: one per parent phone number; visit count, lifetime spend.
- playpackages / products: catalogue and prices.
- businesssettings: currency, timezone, tax, multi-child discount tiers, loyalty interval.
- auditlogs: staff actions (cancellations, voids, corrections, settings changes).

## Pricing modes
- BLOCK_WITH_GRACE: fixed price per block (e.g. 60 min) plus grace minutes.
- TIERED_HOURLY: a price per hour that can change by hour number; overtime per minute.
- PRORATA: charged in proportion to minutes played.
"""


def register(mcp: MCPServer) -> None:
    @mcp.resource(
        "jellybeans://guide",
        name="data_guide",
        title="Jellybeans data guide",
        description="Business definitions behind the analytics tools (revenue, visits, currency, timezone).",
        mime_type="text/markdown",
    )
    def data_guide() -> str:
        return DATA_GUIDE
