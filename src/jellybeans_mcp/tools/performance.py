"""Monthly and yearly performance across bills, play sessions and customers (calendar months/years)."""

import asyncio
from datetime import date, datetime, timedelta
from typing import Annotated, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

from jellybeans_mcp.db import AppContext, Document
from jellybeans_mcp.tools.common import (
    CHILDREN_EXPR,
    ITEM_KIND_EXPR,
    READ_ONLY,
    Period,
    bucket_start,
    check_buckets,
    get_app,
    instrumented,
    lkr,
    local,
    paid_bills,
    shift,
    utc_range,
    valid_sessions,
)

MAX_BUCKETS = {"month": 120, "year": 10}


class PeriodRow(BaseModel):
    label: str = Field(description="'2026-10' for a month, '2026' for a year")
    start: date
    end: date
    partial: bool = Field(description="Period still in progress (to date); compare with care")
    net_revenue: float
    paid_bills: int
    average_bill: float
    play_sessions: int
    total_children: int = Field(description="Children in play sessions plus group bookings")
    new_customers: int
    revenue_change_vs_previous_pct: float | None = Field(description="vs the previous month/year; null if no base")
    revenue_change_vs_last_year_pct: float | None = Field(
        description="vs the same month last year (same as previous for yearly rows); null if no base"
    )


class PeriodTotals(BaseModel):
    net_revenue: float
    paid_bills: int
    average_bill: float
    play_sessions: int
    total_children: int
    new_customers: int


class PeriodPerformance(BaseModel):
    granularity: str
    currency: str
    period: Period
    rows: list[PeriodRow]
    totals: PeriodTotals
    note: str


def _pct(current: int, base: int | None) -> float | None:
    if not base:
        return None
    return round((current - base) / base * 100, 1)


async def _grouped(app: AppContext, collection: str, pipeline: list[Document]) -> dict[date, Document]:
    rows = await app.aggregate(collection, pipeline)
    return {local(app, r["_id"]).date(): r for r in rows}  # type: ignore[union-attr]


def register(mcp: MCPServer) -> None:
    @mcp.tool(title="Monthly/yearly performance", annotations=READ_ONLY)
    @instrumented
    async def get_period_performance(
        ctx: Context,
        granularity: Annotated[
            Literal["month", "year"], Field(description="One row per calendar month or per calendar year (Jan-Dec)")
        ] = "month",
        start_date: Annotated[
            date | None,
            Field(
                description="Any day in the first month/year (YYYY-MM-DD). Defaults to 11 months ago for months, "
                "or the first year with sales for years."
            ),
        ] = None,
        end_date: Annotated[
            date | None, Field(description="Any day in the last month/year (YYYY-MM-DD). Defaults to today.")
        ] = None,
    ) -> PeriodPerformance:
        """Monthly or yearly performance: one row per calendar month or year with revenue, bills, average bill,
        play sessions, children and new customers, plus % change vs the previous period and vs last year.

        Use this for questions like "how did each month go this year?" or "compare 2025 with 2026".
        """
        app = get_app(ctx)
        today = datetime.now(app.tz).date()
        last = bucket_start(end_date or today, granularity)
        if start_date:
            first = bucket_start(start_date, granularity)
        elif granularity == "month":
            first = shift(last, "month", -11)
        else:
            oldest = await app.db.bills.find_one(
                {"status": "PAID", "isTestBill": {"$ne": True}}, {"paidAt": 1}, sort=[("paidAt", 1)]
            )
            first = bucket_start(local(app, oldest["paidAt"]).date(), "year") if oldest else last  # type: ignore[union-attr]
        if first > last:
            raise ToolError("start_date is after end_date")
        period_end = shift(last, granularity, 1) - timedelta(days=1)
        period = Period(start=first, end=period_end, timezone=app.tz.key)
        check_buckets(period, granularity, MAX_BUCKETS[granularity])

        # Query one extra year back so every row has a previous-period and last-year comparison.
        query_start = shift(first, granularity, -12 if granularity == "month" else -1)
        rng = utc_range(app, query_start, period_end)

        def trunc(field: str) -> Document:
            return {"$dateTrunc": {"date": field, "unit": granularity, "timezone": app.tz.key}}

        bills, groups, sessions, customers = await asyncio.gather(
            _grouped(
                app,
                "bills",
                [
                    {"$match": paid_bills(rng)},
                    {"$group": {"_id": trunc("$paidAt"), "bills": {"$sum": 1}, "amount": {"$sum": "$grandTotal"}}},
                ],
            ),
            _grouped(
                app,
                "bills",
                [
                    {"$match": paid_bills(rng)},
                    {"$unwind": "$items"},
                    {"$match": {"$expr": {"$eq": [ITEM_KIND_EXPR, "GROUP"]}}},
                    {"$group": {"_id": trunc("$paidAt"), "children": {"$sum": "$items.quantity"}}},
                ],
            ),
            _grouped(
                app,
                "playsessions",
                [
                    {"$match": valid_sessions(rng)},
                    {
                        "$group": {
                            "_id": trunc("$checkInAt"),
                            "sessions": {"$sum": 1},
                            "children": {"$sum": CHILDREN_EXPR},
                        }
                    },
                ],
            ),
            _grouped(
                app,
                "customers",
                [
                    {"$match": {"firstVisitAt": rng}},
                    {"$group": {"_id": trunc("$firstVisitAt"), "n": {"$sum": 1}}},
                ],
            ),
        )

        def cents(d: date) -> int:
            return bills.get(d, {}).get("amount", 0)

        rows: list[PeriodRow] = []
        d = first
        while d <= last:
            end = shift(d, granularity, 1) - timedelta(days=1)
            n_bills = bills.get(d, {}).get("bills", 0)
            amount = cents(d)
            rows.append(
                PeriodRow(
                    label=d.strftime("%Y-%m" if granularity == "month" else "%Y"),
                    start=d,
                    end=end,
                    partial=end >= today >= d,
                    net_revenue=lkr(amount),
                    paid_bills=n_bills,
                    average_bill=lkr(amount / n_bills) if n_bills else 0.0,
                    play_sessions=sessions.get(d, {}).get("sessions", 0),
                    total_children=sessions.get(d, {}).get("children", 0) + groups.get(d, {}).get("children", 0),
                    new_customers=customers.get(d, {}).get("n", 0),
                    revenue_change_vs_previous_pct=_pct(amount, cents(shift(d, granularity, -1))),
                    revenue_change_vs_last_year_pct=_pct(
                        amount, cents(shift(d, granularity, -12 if granularity == "month" else -1))
                    ),
                )
            )
            d = shift(d, granularity, 1)

        total_cents = sum(cents(r.start) for r in rows)
        total_bills = sum(r.paid_bills for r in rows)
        return PeriodPerformance(
            granularity=granularity,
            currency=app.currency,
            period=period,
            rows=rows,
            totals=PeriodTotals(
                net_revenue=lkr(total_cents),
                paid_bills=total_bills,
                average_bill=lkr(total_cents / total_bills) if total_bills else 0.0,
                play_sessions=sum(r.play_sessions for r in rows),
                total_children=sum(r.total_children for r in rows),
                new_customers=sum(r.new_customers for r in rows),
            ),
            note="Dates are snapped to whole calendar months/years. Rows with partial=true are still in progress, "
            "so their % change compares a part-period with a full one.",
        )
