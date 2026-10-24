"""Visit, occupancy and package analytics (source: playsessions, plus GROUP bill lines)."""

from datetime import UTC, datetime

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from pydantic import BaseModel, Field

from jellybeans_mcp.tools.common import (
    CHILDREN_EXPR,
    ITEM_KIND_EXPR,
    READ_ONLY,
    EndDate,
    Period,
    StartDate,
    get_app,
    instrumented,
    lkr,
    local,
    paid_bills,
    resolve_period,
    valid_sessions,
)

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
DURATION_BOUNDS = [0, 30, 60, 90, 120, 180]


class DurationBand(BaseModel):
    band: str = Field(description="Billed play time in minutes, e.g. '30-59'")
    sessions: int


class VisitStats(BaseModel):
    period: Period
    play_sessions: int
    children_in_sessions: int
    group_booking_children: int = Field(description="Children billed through GROUP bookings (no play session)")
    total_children: int
    average_children_per_day: float
    average_billed_minutes: float | None
    longest_billed_minutes: int | None
    registered_customer_sessions: int
    walk_in_sessions: int = Field(description="Sessions not linked to a customer record")
    voided_sessions: int
    duration_bands: list[DurationBand]


class Slot(BaseModel):
    weekday: str
    hour: int = Field(description="Hour of day 0-23, business local time")
    sessions: int
    children: int


class Total(BaseModel):
    label: str
    sessions: int
    children: int


class PeakHours(BaseModel):
    period: Period
    busiest_slots: list[Slot]
    by_weekday: list[Total]
    by_hour: list[Total]
    all_slots: list[Slot]


class PackageStats(BaseModel):
    package_name: str
    is_active: bool | None
    current_price: float | None
    duration_minutes: int | None
    pricing_mode: str | None
    sessions: int
    children: int
    average_billed_minutes: float | None
    charged_amount: float = Field(description="Session charges before bill-level discounts")


class PackagePerformance(BaseModel):
    period: Period
    currency: str
    packages: list[PackageStats]


class ActiveSession(BaseModel):
    ticket_code: str
    children: list[str]
    parent_name: str | None
    package_name: str
    checked_in_at: datetime
    minutes_in_play: int
    included_minutes: int = Field(description="Package duration plus grace minutes")
    overtime_minutes: int


class CurrentOccupancy(BaseModel):
    as_of: datetime
    active_sessions: int
    children_in_play: int
    sessions: list[ActiveSession]


def register(mcp: MCPServer) -> None:
    @mcp.tool(title="Visit statistics", annotations=READ_ONLY)
    @instrumented
    async def get_visit_stats(ctx: Context, start_date: StartDate = None, end_date: EndDate = None) -> VisitStats:
        """Footfall for a period: play sessions, children (including group bookings), play duration and voids."""
        app = get_app(ctx)
        period, rng = resolve_period(app, start_date, end_date)
        [facets] = await app.aggregate(
            "playsessions",
            [
                {"$match": valid_sessions(rng)},
                {
                    "$facet": {
                        "totals": [
                            {
                                "$group": {
                                    "_id": None,
                                    "sessions": {"$sum": 1},
                                    "children": {"$sum": CHILDREN_EXPR},
                                    "avg_minutes": {"$avg": "$billedMinutes"},
                                    "max_minutes": {"$max": "$billedMinutes"},
                                    "registered": {"$sum": {"$cond": [{"$gt": ["$customerId", None]}, 1, 0]}},
                                }
                            }
                        ],
                        "bands": [
                            {"$match": {"billedMinutes": {"$type": "number"}}},
                            {
                                "$bucket": {
                                    "groupBy": "$billedMinutes",
                                    "boundaries": DURATION_BOUNDS,
                                    "default": "180+",
                                    "output": {"sessions": {"$sum": 1}},
                                }
                            },
                        ],
                    }
                },
            ],
        )
        groups = await app.aggregate(
            "bills",
            [
                {"$match": paid_bills(rng)},
                {"$unwind": "$items"},
                {"$match": {"$expr": {"$eq": [ITEM_KIND_EXPR, "GROUP"]}}},
                {"$group": {"_id": None, "children": {"$sum": "$items.quantity"}}},
            ],
        )
        voided = await app.db.playsessions.count_documents(
            {"status": "VOIDED", "isTestBill": {"$ne": True}, "checkInAt": rng}
        )
        t = facets["totals"][0] if facets["totals"] else {}
        session_children = t.get("children", 0)
        group_children = groups[0]["children"] if groups else 0
        days = (period.end - period.start).days + 1
        bands = []
        for b in facets["bands"]:
            if b["_id"] == "180+":
                label = "180+"
            else:
                upper = DURATION_BOUNDS[DURATION_BOUNDS.index(b["_id"]) + 1]
                label = f"{b['_id']}-{upper - 1}"
            bands.append(DurationBand(band=label, sessions=b["sessions"]))
        avg = t.get("avg_minutes")
        return VisitStats(
            period=period,
            play_sessions=t.get("sessions", 0),
            children_in_sessions=session_children,
            group_booking_children=group_children,
            total_children=session_children + group_children,
            average_children_per_day=round((session_children + group_children) / days, 2),
            average_billed_minutes=round(avg, 1) if avg is not None else None,
            longest_billed_minutes=t.get("max_minutes"),
            registered_customer_sessions=t.get("registered", 0),
            walk_in_sessions=t.get("sessions", 0) - t.get("registered", 0),
            voided_sessions=voided,
            duration_bands=bands,
        )

    @mcp.tool(title="Peak hours", annotations=READ_ONLY)
    @instrumented
    async def get_peak_hours(ctx: Context, start_date: StartDate = None, end_date: EndDate = None) -> PeakHours:
        """When children arrive: check-ins by weekday and hour of day (business local time), with the busiest slots."""
        app = get_app(ctx)
        period, rng = resolve_period(app, start_date, end_date)
        tz = app.tz.key
        rows = await app.aggregate(
            "playsessions",
            [
                {"$match": valid_sessions(rng)},
                {
                    "$group": {
                        "_id": {
                            "dow": {"$isoDayOfWeek": {"date": "$checkInAt", "timezone": tz}},
                            "hour": {"$hour": {"date": "$checkInAt", "timezone": tz}},
                        },
                        "sessions": {"$sum": 1},
                        "children": {"$sum": CHILDREN_EXPR},
                    }
                },
                {"$sort": {"_id.dow": 1, "_id.hour": 1}},
            ],
        )
        slots = [
            Slot(
                weekday=WEEKDAYS[r["_id"]["dow"] - 1],
                hour=r["_id"]["hour"],
                sessions=r["sessions"],
                children=r["children"],
            )
            for r in rows
        ]

        def totals(key: str) -> list[Total]:
            acc: dict[str, list[int]] = {}
            for s in slots:
                label = s.weekday if key == "weekday" else f"{s.hour:02d}:00"
                acc.setdefault(label, [0, 0])
                acc[label][0] += s.sessions
                acc[label][1] += s.children
            return [Total(label=k, sessions=v[0], children=v[1]) for k, v in acc.items()]

        by_hour = sorted(totals("hour"), key=lambda t: t.label)
        return PeakHours(
            period=period,
            busiest_slots=sorted(slots, key=lambda s: (s.children, s.sessions), reverse=True)[:5],
            by_weekday=totals("weekday"),
            by_hour=by_hour,
            all_slots=slots,
        )

    @mcp.tool(title="Package performance", annotations=READ_ONLY)
    @instrumented
    async def get_package_performance(
        ctx: Context, start_date: StartDate = None, end_date: EndDate = None
    ) -> PackagePerformance:
        """How each play package performs: sessions, children, average play time and charges, with current price."""
        app = get_app(ctx)
        period, rng = resolve_period(app, start_date, end_date)
        rows = await app.aggregate(
            "playsessions",
            [
                {"$match": valid_sessions(rng)},
                {
                    "$group": {
                        "_id": "$playPackageId",
                        "name": {"$last": "$packageName"},
                        "sessions": {"$sum": 1},
                        "children": {"$sum": CHILDREN_EXPR},
                        "avg_minutes": {"$avg": "$billedMinutes"},
                        "charged": {"$sum": {"$ifNull": ["$chargedAmount", 0]}},
                    }
                },
                {
                    "$lookup": {
                        "from": "playpackages",
                        "localField": "_id",
                        "foreignField": "_id",
                        "as": "pkg",
                        "pipeline": [
                            {"$project": {"name": 1, "isActive": 1, "price": 1, "durationMinutes": 1, "pricingMode": 1}}
                        ],
                    }
                },
                {"$sort": {"charged": -1}},
            ],
        )
        packages = []
        for r in rows:
            pkg = r["pkg"][0] if r["pkg"] else {}
            avg = r.get("avg_minutes")
            packages.append(
                PackageStats(
                    package_name=pkg.get("name") or r["name"] or "UNKNOWN",
                    is_active=pkg.get("isActive"),
                    current_price=lkr(pkg["price"]) if "price" in pkg else None,
                    duration_minutes=pkg.get("durationMinutes"),
                    pricing_mode=pkg.get("pricingMode"),
                    sessions=r["sessions"],
                    children=r["children"],
                    average_billed_minutes=round(avg, 1) if avg is not None else None,
                    charged_amount=lkr(r["charged"]),
                )
            )
        return PackagePerformance(period=period, currency=app.currency, packages=packages)

    @mcp.tool(title="Current occupancy", annotations=READ_ONLY)
    @instrumented
    async def get_current_occupancy(ctx: Context) -> CurrentOccupancy:
        """Who is in the play area right now: open sessions with children, time played and any overtime."""
        app = get_app(ctx)
        now = datetime.now(UTC)
        docs = (
            await app.db.playsessions.find(
                {"checkOutAt": None, "status": {"$nin": ["VOIDED", "CLOSED"]}, "isTestBill": {"$ne": True}},
                {
                    "ticketCode": 1,
                    "childName": 1,
                    "childNames": 1,
                    "parentName": 1,
                    "packageName": 1,
                    "checkInAt": 1,
                    "rateDurationMinutes": 1,
                    "graceMinutes": 1,
                },
            )
            .sort("checkInAt", 1)
            .to_list()
        )
        sessions = []
        for d in docs:
            children = d.get("childNames") or [d.get("childName", "")]
            minutes = int((now - d["checkInAt"]).total_seconds() // 60)
            included = (d.get("rateDurationMinutes") or 0) + (d.get("graceMinutes") or 0)
            sessions.append(
                ActiveSession(
                    ticket_code=d["ticketCode"],
                    children=children,
                    parent_name=d.get("parentName"),
                    package_name=d.get("packageName", ""),
                    checked_in_at=local(app, d["checkInAt"]),  # type: ignore[arg-type]
                    minutes_in_play=minutes,
                    included_minutes=included,
                    overtime_minutes=max(0, minutes - included) if included else 0,
                )
            )
        return CurrentOccupancy(
            as_of=now.astimezone(app.tz),
            active_sessions=len(sessions),
            children_in_play=sum(len(s.children) for s in sessions),
            sessions=sessions,
        )
