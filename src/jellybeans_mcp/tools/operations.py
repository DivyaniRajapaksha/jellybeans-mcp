"""Business configuration and operational exceptions (settings, catalogue, audit trail)."""

from datetime import datetime

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from pydantic import BaseModel, Field

from jellybeans_mcp.tools.common import (
    READ_ONLY,
    EndDate,
    Period,
    StartDate,
    get_app,
    instrumented,
    lkr,
    local,
    redact,
    resolve_period,
)

AUDIT_ACTIONS = [
    "SESSION_FORCE_CLOSED",
    "BILL_CORRECTED",
    "BILL_PAYMENT_RECORDED_BY_OTHER",
    "BILL_GROUP_RATE_ENTERED",
    "PACKAGE_UPDATED",
    "PACKAGE_STATUS_CHANGED",
    "SETTINGS_UPDATED",
    "USER_CREATED",
]
EXCEPTION_LIMIT = 100


class DiscountTier(BaseModel):
    min_children: int
    percentage: float


class Package(BaseModel):
    name: str
    description: str | None
    is_active: bool
    price: float
    duration_minutes: int
    grace_minutes: int
    pricing_mode: str = Field(description="BLOCK_WITH_GRACE, TIERED_HOURLY or PRORATA")
    hourly_rates: list[float] | None = Field(description="TIERED_HOURLY: price for hour 1, 2, 3, 4...")


class Product(BaseModel):
    name: str
    description: str | None
    is_active: bool
    price: float


class BusinessInfo(BaseModel):
    business_name: str | None
    address: str | None
    currency: str
    timezone: str
    tax_enabled: bool
    tax_percentage: float
    minimum_billable_minutes: int | None
    maximum_session_hours: int | None
    maximum_cashier_discount_percentage: float | None
    loyalty_visit_interval: int | None
    multi_child_discount_tiers: list[DiscountTier]
    packages: list[Package]
    products: list[Product]


class CancelledBill(BaseModel):
    bill_number: str | None
    cancelled_at: datetime | None
    amount: float
    reason: str | None
    cancelled_by: str | None
    cashier: str | None


class VoidedSession(BaseModel):
    ticket_code: str
    voided_at: datetime | None
    package_name: str | None
    children: int
    reason: str | None
    voided_by: str | None


class WriteOff(BaseModel):
    bill_number: str | None
    paid_at: datetime | None
    amount: float
    reason: str | None
    cashier: str | None


class AuditEvent(BaseModel):
    action: str
    at: datetime | None
    user: str | None
    entity_type: str | None
    reason: str | None


class Exceptions(BaseModel):
    period: Period
    currency: str
    cancelled_bills: list[CancelledBill]
    voided_sessions: list[VoidedSession]
    write_offs: list[WriteOff]
    audit_events: list[AuditEvent] = Field(description="Force-closes, bill corrections, price and settings changes")
    truncated: bool = Field(description=f"True if any list hit the {EXCEPTION_LIMIT}-row limit")


def register(mcp: MCPServer) -> None:
    @mcp.tool(title="Business info", annotations=READ_ONLY)
    @instrumented
    async def get_business_info(ctx: Context) -> BusinessInfo:
        """Business setup: name, currency, timezone, tax, discount rules, play packages and products with prices."""
        app = get_app(ctx)
        s = (
            await app.db.businesssettings.find_one(
                # Allowlist: contact details and receipt texts (which may contain them) are never read.
                {},
                {
                    "businessName": 1,
                    "address": 1,
                    "taxEnabled": 1,
                    "taxPercentage": 1,
                    "minimumBillableMinutes": 1,
                    "maximumSessionHours": 1,
                    "maximumCashierDiscountPercentage": 1,
                    "loyaltyVisitInterval": 1,
                    "multiChildDiscountTiers": 1,
                },
            )
            or {}
        )
        packages = await app.db.playpackages.find({}).sort([("isActive", -1), ("sortOrder", 1)]).to_list()
        products = await app.db.products.find({}).sort([("isActive", -1), ("sortOrder", 1)]).to_list()
        return BusinessInfo(
            business_name=s.get("businessName"),
            address=s.get("address"),
            currency=app.currency,
            timezone=app.tz.key,
            tax_enabled=bool(s.get("taxEnabled")),
            tax_percentage=s.get("taxPercentage") or 0,
            minimum_billable_minutes=s.get("minimumBillableMinutes"),
            maximum_session_hours=s.get("maximumSessionHours"),
            maximum_cashier_discount_percentage=s.get("maximumCashierDiscountPercentage"),
            loyalty_visit_interval=s.get("loyaltyVisitInterval"),
            multi_child_discount_tiers=[
                DiscountTier(min_children=t["minChildren"], percentage=t["percentage"])
                for t in s.get("multiChildDiscountTiers") or []
            ],
            packages=[
                Package(
                    name=p["name"],
                    description=p.get("description") or None,
                    is_active=p.get("isActive", False),
                    price=lkr(p.get("price")),
                    duration_minutes=p.get("durationMinutes", 0),
                    grace_minutes=p.get("graceMinutes", 0),
                    pricing_mode=p.get("pricingMode", ""),
                    hourly_rates=[lkr(r) for r in (p.get("tieredPricing") or {}).get("hourlyRates", [])] or None,
                )
                for p in packages
            ],
            products=[
                Product(
                    name=p["name"],
                    description=p.get("description") or None,
                    is_active=p.get("isActive", False),
                    price=lkr(p.get("price")),
                )
                for p in products
            ],
        )

    @mcp.tool(title="Exceptions to review", annotations=READ_ONLY)
    @instrumented
    async def get_exceptions(ctx: Context, start_date: StartDate = None, end_date: EndDate = None) -> Exceptions:
        """Things a manager should review: cancelled bills, voided sessions, write-offs and sensitive staff actions."""
        app = get_app(ctx)
        period, rng = resolve_period(app, start_date, end_date)
        users = {u["_id"]: u["name"] for u in await app.db.users.find({}, {"name": 1}).to_list()}
        not_test = {"isTestBill": {"$ne": True}}

        cancelled = (
            await app.db.bills.find(
                {"status": "CANCELLED", "cancelledAt": rng, **not_test},
                {
                    "billNumber": 1,
                    "cancelledAt": 1,
                    "grandTotal": 1,
                    "cancellationReason": 1,
                    "cancelledBy": 1,
                    "cashierName": 1,
                },
            )
            .sort("cancelledAt", -1)
            .limit(EXCEPTION_LIMIT)
            .to_list()
        )
        voided = (
            await app.db.playsessions.find(
                {"status": "VOIDED", "voidedAt": rng, **not_test},
                {
                    "ticketCode": 1,
                    "voidedAt": 1,
                    "packageName": 1,
                    "childCount": 1,
                    "childNames": 1,
                    "voidReason": 1,
                    "voidedBy": 1,
                },
            )
            .sort("voidedAt", -1)
            .limit(EXCEPTION_LIMIT)
            .to_list()
        )
        write_offs = (
            await app.db.bills.find(
                {"writeOff": {"$gt": 0}, "paidAt": rng, **not_test},
                {"billNumber": 1, "paidAt": 1, "writeOff": 1, "writeOffReason": 1, "cashierName": 1},
            )
            .sort("paidAt", -1)
            .limit(EXCEPTION_LIMIT)
            .to_list()
        )
        audits = (
            await app.db.auditlogs.find(
                {"action": {"$in": AUDIT_ACTIONS}, "createdAt": rng},
                {"action": 1, "createdAt": 1, "userName": 1, "entityType": 1, "metadata.reason": 1},
            )
            .sort("createdAt", -1)
            .limit(EXCEPTION_LIMIT)
            .to_list()
        )

        return Exceptions(
            period=period,
            currency=app.currency,
            cancelled_bills=[
                CancelledBill(
                    bill_number=b.get("billNumber"),
                    cancelled_at=local(app, b.get("cancelledAt")),
                    amount=lkr(b.get("grandTotal")),
                    reason=redact(b.get("cancellationReason")),
                    cancelled_by=users.get(b.get("cancelledBy")),
                    cashier=b.get("cashierName"),
                )
                for b in cancelled
            ],
            voided_sessions=[
                VoidedSession(
                    ticket_code=s["ticketCode"],
                    voided_at=local(app, s.get("voidedAt")),
                    package_name=s.get("packageName"),
                    children=s.get("childCount") or len(s.get("childNames") or []) or 1,
                    reason=redact(s.get("voidReason")),
                    voided_by=users.get(s.get("voidedBy")),
                )
                for s in voided
            ],
            write_offs=[
                WriteOff(
                    bill_number=b.get("billNumber"),
                    paid_at=local(app, b.get("paidAt")),
                    amount=lkr(b.get("writeOff")),
                    reason=redact(b.get("writeOffReason")),
                    cashier=b.get("cashierName"),
                )
                for b in write_offs
            ],
            audit_events=[
                AuditEvent(
                    action=a["action"],
                    at=local(app, a.get("createdAt")),
                    user=a.get("userName"),
                    entity_type=a.get("entityType"),
                    reason=redact((a.get("metadata") or {}).get("reason")),
                )
                for a in audits
            ],
            truncated=any(len(x) >= EXCEPTION_LIMIT for x in (cancelled, voided, write_offs, audits)),
        )
