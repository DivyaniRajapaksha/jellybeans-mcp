"""Revenue, sales and cashier analytics (source: bills)."""

from datetime import date, timedelta
from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from pydantic import BaseModel, Field

from jellybeans_mcp.tools.common import (
    ITEM_KIND_EXPR,
    READ_ONLY,
    EndDate,
    Granularity,
    Period,
    StartDate,
    check_buckets,
    get_app,
    instrumented,
    lkr,
    local,
    paid_bills,
    resolve_period,
)


class PaymentMethodTotal(BaseModel):
    payment_method: str
    bills: int
    amount: float


class RevenueSummary(BaseModel):
    period: Period
    currency: str
    paid_bills: int
    gross_sales: float = Field(description="Sum of bill subtotals before discounts")
    discounts: float = Field(description="Cashier discounts")
    multi_child_discounts: float
    tax: float
    net_revenue: float = Field(description="Sum of grand totals of paid bills")
    write_offs: float
    average_bill: float
    by_payment_method: list[PaymentMethodTotal]
    cancelled_bills: int
    cancelled_value: float


class TrendPoint(BaseModel):
    bucket_start: date
    bills: int
    net_revenue: float


class RevenueTrend(BaseModel):
    period: Period
    currency: str
    granularity: str
    points: list[TrendPoint]


class KindTotal(BaseModel):
    kind: str = Field(description="PLAY (timed play), GROUP (group booking) or PRODUCT (add-on sale)")
    lines: int
    quantity: int
    line_total: float


class PackageTotal(BaseModel):
    package_name: str
    kind: str
    lines: int
    quantity: int
    line_total: float


class RevenueBreakdown(BaseModel):
    period: Period
    currency: str
    note: str
    by_kind: list[KindTotal]
    by_package: list[PackageTotal]


class ProductSale(BaseModel):
    product_name: str
    units_sold: int
    revenue: float
    is_active: bool | None


class ProductSales(BaseModel):
    period: Period
    currency: str
    products: list[ProductSale]


class CashierStats(BaseModel):
    cashier_name: str
    paid_bills: int
    net_revenue: float
    discounts_given: float
    bills_with_discount: int
    bills_cancelled: int
    sessions_voided: int


class CashierPerformance(BaseModel):
    period: Period
    currency: str
    cashiers: list[CashierStats]


def register(mcp: MCPServer) -> None:
    @mcp.tool(title="Revenue summary", annotations=READ_ONLY)
    @instrumented
    async def get_revenue_summary(
        ctx: Context, start_date: StartDate = None, end_date: EndDate = None
    ) -> RevenueSummary:
        """Headline revenue for a period: net revenue, discounts, average bill, payment-method split and cancellations.

        Only paid, non-test bills count as revenue (by payment date).
        """
        app = get_app(ctx)
        period, paid_at = resolve_period(app, start_date, end_date)
        [facets] = await app.aggregate(
            "bills",
            [
                {"$match": paid_bills(paid_at)},
                {
                    "$facet": {
                        "totals": [
                            {
                                "$group": {
                                    "_id": None,
                                    "bills": {"$sum": 1},
                                    "subtotal": {"$sum": "$subtotal"},
                                    "discount": {"$sum": "$discount"},
                                    "multi_child": {"$sum": {"$ifNull": ["$multiChildDiscount", 0]}},
                                    "tax": {"$sum": "$tax"},
                                    "grand_total": {"$sum": "$grandTotal"},
                                    "write_off": {"$sum": {"$ifNull": ["$writeOff", 0]}},
                                }
                            }
                        ],
                        "methods": [
                            {
                                "$group": {
                                    "_id": "$paymentMethod",
                                    "bills": {"$sum": 1},
                                    "amount": {"$sum": "$grandTotal"},
                                }
                            },
                            {"$sort": {"amount": -1}},
                        ],
                    }
                },
            ],
        )
        cancelled = await app.aggregate(
            "bills",
            [
                {"$match": {"status": "CANCELLED", "isTestBill": {"$ne": True}, "createdAt": paid_at}},
                {"$group": {"_id": None, "bills": {"$sum": 1}, "amount": {"$sum": "$grandTotal"}}},
            ],
        )
        t = facets["totals"][0] if facets["totals"] else {}
        bills = t.get("bills", 0)
        return RevenueSummary(
            period=period,
            currency=app.currency,
            paid_bills=bills,
            gross_sales=lkr(t.get("subtotal")),
            discounts=lkr(t.get("discount")),
            multi_child_discounts=lkr(t.get("multi_child")),
            tax=lkr(t.get("tax")),
            net_revenue=lkr(t.get("grand_total")),
            write_offs=lkr(t.get("write_off")),
            average_bill=lkr(t["grand_total"] / bills) if bills else 0.0,
            by_payment_method=[
                PaymentMethodTotal(payment_method=m["_id"] or "UNKNOWN", bills=m["bills"], amount=lkr(m["amount"]))
                for m in facets["methods"]
            ],
            cancelled_bills=cancelled[0]["bills"] if cancelled else 0,
            cancelled_value=lkr(cancelled[0]["amount"]) if cancelled else 0.0,
        )

    @mcp.tool(title="Revenue trend", annotations=READ_ONLY)
    @instrumented
    async def get_revenue_trend(
        ctx: Context,
        start_date: StartDate = None,
        end_date: EndDate = None,
        granularity: Annotated[
            Granularity, Field(description="Bucket size; weeks start on Monday, years are calendar years")
        ] = "day",
    ) -> RevenueTrend:
        """Net revenue and bill count over time, bucketed by day, week, month or year (business local time).

        Ranges can span up to about 10 years, with at most 400 points (use month or year for long ranges).
        """
        app = get_app(ctx)
        period, paid_at = resolve_period(app, start_date, end_date)
        check_buckets(period, granularity)
        rows = await app.aggregate(
            "bills",
            [
                {"$match": paid_bills(paid_at)},
                {
                    "$group": {
                        "_id": {
                            "$dateTrunc": {
                                "date": "$paidAt",
                                "unit": granularity,
                                "timezone": app.tz.key,
                                "startOfWeek": "monday",
                            }
                        },
                        "bills": {"$sum": 1},
                        "amount": {"$sum": "$grandTotal"},
                    }
                },
                {"$sort": {"_id": 1}},
            ],
        )
        by_day = {local(app, r["_id"]).date(): r for r in rows}  # type: ignore[union-attr]
        if granularity == "day":
            # Include days with no sales so gaps are visible.
            days = [period.start + timedelta(days=i) for i in range((period.end - period.start).days + 1)]
        else:
            days = sorted(by_day)
        points = [
            TrendPoint(
                bucket_start=d,
                bills=by_day.get(d, {}).get("bills", 0),
                net_revenue=lkr(by_day.get(d, {}).get("amount")),
            )
            for d in days
        ]
        return RevenueTrend(period=period, currency=app.currency, granularity=granularity, points=points)

    @mcp.tool(title="Revenue breakdown", annotations=READ_ONLY)
    @instrumented
    async def get_revenue_breakdown(
        ctx: Context, start_date: StartDate = None, end_date: EndDate = None
    ) -> RevenueBreakdown:
        """Split sales by line type (play, group booking, product) and by package/product name.

        Amounts are bill line totals before bill-level discounts.
        """
        app = get_app(ctx)
        period, paid_at = resolve_period(app, start_date, end_date)
        [facets] = await app.aggregate(
            "bills",
            [
                {"$match": paid_bills(paid_at)},
                {"$unwind": "$items"},
                {
                    "$set": {
                        "kind": ITEM_KIND_EXPR,
                        "name": {"$ifNull": ["$items.chosenPackageName", "$items.packageName"]},
                    }
                },
                {
                    "$facet": {
                        "kinds": [
                            {
                                "$group": {
                                    "_id": "$kind",
                                    "lines": {"$sum": 1},
                                    "quantity": {"$sum": "$items.quantity"},
                                    "total": {"$sum": "$items.lineTotal"},
                                }
                            },
                            {"$sort": {"total": -1}},
                        ],
                        "packages": [
                            {
                                "$group": {
                                    "_id": {"name": "$name", "kind": "$kind"},
                                    "lines": {"$sum": 1},
                                    "quantity": {"$sum": "$items.quantity"},
                                    "total": {"$sum": "$items.lineTotal"},
                                }
                            },
                            {"$sort": {"total": -1}},
                        ],
                    }
                },
            ],
        )
        return RevenueBreakdown(
            period=period,
            currency=app.currency,
            note="Line totals are before bill-level discounts; see get_revenue_summary for net revenue.",
            by_kind=[
                KindTotal(kind=k["_id"], lines=k["lines"], quantity=k["quantity"], line_total=lkr(k["total"]))
                for k in facets["kinds"]
            ],
            by_package=[
                PackageTotal(
                    package_name=p["_id"]["name"] or "UNKNOWN",
                    kind=p["_id"]["kind"],
                    lines=p["lines"],
                    quantity=p["quantity"],
                    line_total=lkr(p["total"]),
                )
                for p in facets["packages"]
            ],
        )

    @mcp.tool(title="Product sales", annotations=READ_ONLY)
    @instrumented
    async def get_product_sales(ctx: Context, start_date: StartDate = None, end_date: EndDate = None) -> ProductSales:
        """Units sold and revenue for each add-on product (snacks, socks, etc.) on paid bills."""
        app = get_app(ctx)
        period, paid_at = resolve_period(app, start_date, end_date)
        rows = await app.aggregate(
            "bills",
            [
                {"$match": paid_bills(paid_at)},
                {"$unwind": "$items"},
                {"$match": {"$expr": {"$eq": [ITEM_KIND_EXPR, "PRODUCT"]}}},
                {
                    "$group": {
                        "_id": "$items.productId",
                        "name": {"$last": "$items.packageName"},
                        "units": {"$sum": "$items.quantity"},
                        "total": {"$sum": "$items.lineTotal"},
                    }
                },
                {
                    "$lookup": {
                        "from": "products",
                        "localField": "_id",
                        "foreignField": "_id",
                        "as": "product",
                        "pipeline": [{"$project": {"name": 1, "isActive": 1}}],
                    }
                },
                {"$sort": {"total": -1}},
            ],
        )
        products = []
        for r in rows:
            product = r["product"][0] if r["product"] else {}
            products.append(
                ProductSale(
                    product_name=product.get("name") or r["name"] or "UNKNOWN",
                    units_sold=r["units"],
                    revenue=lkr(r["total"]),
                    is_active=product.get("isActive"),
                )
            )
        return ProductSales(period=period, currency=app.currency, products=products)

    @mcp.tool(title="Cashier performance", annotations=READ_ONLY)
    @instrumented
    async def get_cashier_performance(
        ctx: Context, start_date: StartDate = None, end_date: EndDate = None
    ) -> CashierPerformance:
        """Per-cashier activity: bills handled, revenue, discounts given, bills cancelled and sessions voided."""
        app = get_app(ctx)
        period, rng = resolve_period(app, start_date, end_date)
        sales = await app.aggregate(
            "bills",
            [
                {"$match": paid_bills(rng)},
                {
                    "$group": {
                        "_id": "$cashierId",
                        "name": {"$last": "$cashierName"},
                        "bills": {"$sum": 1},
                        "amount": {"$sum": "$grandTotal"},
                        "discount": {"$sum": "$discount"},
                        "discounted": {"$sum": {"$cond": [{"$gt": ["$discount", 0]}, 1, 0]}},
                    }
                },
            ],
        )
        cancels = await app.aggregate(
            "bills",
            [
                {"$match": {"status": "CANCELLED", "isTestBill": {"$ne": True}, "cancelledAt": rng}},
                {"$group": {"_id": "$cancelledBy", "n": {"$sum": 1}}},
            ],
        )
        voids = await app.aggregate(
            "playsessions",
            [
                {"$match": {"status": "VOIDED", "isTestBill": {"$ne": True}, "voidedAt": rng}},
                {"$group": {"_id": "$voidedBy", "n": {"$sum": 1}}},
            ],
        )
        user_ids = {r["_id"] for r in sales + cancels + voids if r["_id"] is not None}
        users = await app.db.users.find({"_id": {"$in": list(user_ids)}}, {"name": 1}).to_list()
        names = {u["_id"]: u["name"] for u in users} | {r["_id"]: r["name"] for r in sales if r.get("name")}
        by_id = {r["_id"]: r for r in sales}
        cancel_n = {r["_id"]: r["n"] for r in cancels}
        void_n = {r["_id"]: r["n"] for r in voids}
        cashiers = [
            CashierStats(
                cashier_name=names.get(uid, "UNKNOWN"),
                paid_bills=by_id.get(uid, {}).get("bills", 0),
                net_revenue=lkr(by_id.get(uid, {}).get("amount")),
                discounts_given=lkr(by_id.get(uid, {}).get("discount")),
                bills_with_discount=by_id.get(uid, {}).get("discounted", 0),
                bills_cancelled=cancel_n.get(uid, 0),
                sessions_voided=void_n.get(uid, 0),
            )
            for uid in user_ids
        ]
        cashiers.sort(key=lambda c: c.net_revenue, reverse=True)
        return CashierPerformance(period=period, currency=app.currency, cashiers=cashiers)
