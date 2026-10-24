import re
from datetime import datetime
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

from jellybeans_mcp.config import get_settings
from jellybeans_mcp.db import AppContext, Document
from jellybeans_mcp.tools.common import (
    READ_ONLY,
    EndDate,
    Period,
    StartDate,
    get_app,
    instrumented,
    lkr,
    local,
    resolve_period,
)

VISIT_BANDS = [0, 1, 2, 4, 10]
# phoneNumber is fetched only to join sessions; it is never copied into a response model.
CUSTOMER_FIELDS = {
    "parentName": 1,
    "phoneNumber": 1,
    "visitCount": 1,
    "totalSpent": 1,
    "firstVisitAt": 1,
    "lastVisitAt": 1,
}


class FrequencyBand(BaseModel):
    visits: str
    customers: int


class CustomerInsights(BaseModel):
    period: Period
    currency: str
    total_customers: int
    new_customers: int = Field(description="First visit falls in the period")
    returning_customers: int = Field(description="Visited in the period, first visit before it")
    average_visits_per_customer: float
    average_lifetime_spend: float
    visit_frequency: list[FrequencyBand]
    loyalty_visit_interval: int = Field(description="Every Nth visit is rewarded; 0 means loyalty is off")
    customers_due_loyalty_reward: int | None = Field(description="Customers whose next visit earns the reward")


class Visit(BaseModel):
    checked_in_at: datetime | None
    package_name: str | None
    children: list[str]
    billed_minutes: int | None
    charged_amount: float | None
    status: str | None


class CustomerProfile(BaseModel):
    customer_ref: str = Field(description="Opaque customer reference, to tell apart customers with the same name")
    parent_name: str | None
    children: list[str]
    visit_count: int
    total_spent: float
    first_visit_at: datetime | None
    last_visit_at: datetime | None
    recent_visits: list[Visit] | None = None


def _dedupe(names: list[Any]) -> list[str]:
    seen: dict[str, str] = {}
    for n in names:
        if isinstance(n, str) and n.strip():
            seen.setdefault(n.strip().casefold(), n.strip())
    return sorted(seen.values(), key=str.casefold)


async def _children_by_phone(app: AppContext, phones: list[str]) -> dict[str, list[str]]:
    rows = await app.aggregate(
        "playsessions",
        [
            {"$match": {"phoneNumber": {"$in": phones}, "isTestBill": {"$ne": True}}},
            {
                "$group": {
                    "_id": "$phoneNumber",
                    "single": {"$addToSet": "$childName"},
                    "lists": {"$addToSet": "$childNames"},
                }
            },
        ],
    )
    return {
        r["_id"]: _dedupe(r["single"] + [n for lst in r["lists"] if isinstance(lst, list) for n in lst]) for r in rows
    }


def _profile(app: AppContext, c: Document, children: list[str]) -> CustomerProfile:
    return CustomerProfile(
        customer_ref=f"CUST-{c['_id']}",
        parent_name=c.get("parentName"),
        children=children,
        visit_count=c.get("visitCount", 0),
        total_spent=lkr(c.get("totalSpent")),
        first_visit_at=local(app, c.get("firstVisitAt")),
        last_visit_at=local(app, c.get("lastVisitAt")),
    )


class TopCustomers(BaseModel):
    currency: str
    ranked_by: str
    customers: list[CustomerProfile]


SORT_FIELDS = {"total_spent": "totalSpent", "visit_count": "visitCount", "most_recent": "lastVisitAt"}


class CustomerSearch(BaseModel):
    # The search text is deliberately not echoed back: it may be a phone number.
    currency: str
    searched_by: Literal["phone", "name"]
    matches: list[CustomerProfile]


def register(mcp: MCPServer) -> None:
    @mcp.tool(title="Customer insights", annotations=READ_ONLY)
    @instrumented
    async def get_customer_insights(
        ctx: Context, start_date: StartDate = None, end_date: EndDate = None
    ) -> CustomerInsights:
        """Customer base health: new vs returning customers in a period, visit frequency and loyalty status."""
        app = get_app(ctx)
        period, rng = resolve_period(app, start_date, end_date)
        [facets] = await app.aggregate(
            "customers",
            [
                {
                    "$facet": {
                        "totals": [
                            {
                                "$group": {
                                    "_id": None,
                                    "n": {"$sum": 1},
                                    "visits": {"$avg": "$visitCount"},
                                    "spent": {"$avg": "$totalSpent"},
                                }
                            }
                        ],
                        "new": [{"$match": {"firstVisitAt": rng}}, {"$count": "n"}],
                        "returning": [
                            {"$match": {"lastVisitAt": rng, "firstVisitAt": {"$lt": rng["$gte"]}}},
                            {"$count": "n"},
                        ],
                        "bands": [
                            {
                                "$bucket": {
                                    "groupBy": "$visitCount",
                                    "boundaries": VISIT_BANDS,
                                    "default": "10+",
                                    "output": {"n": {"$sum": 1}},
                                }
                            }
                        ],
                    }
                },
            ],
        )
        settings = await app.db.businesssettings.find_one({}, {"loyaltyVisitInterval": 1}) or {}
        interval = settings.get("loyaltyVisitInterval") or 0
        due = None
        if interval > 0:
            due = await app.db.customers.count_documents(
                {"$expr": {"$eq": [{"$mod": ["$visitCount", interval]}, interval - 1]}}
            )
        t = facets["totals"][0] if facets["totals"] else {}
        bands = []
        for b in facets["bands"]:
            if b["_id"] == "10+":
                label = "10+"
            else:
                upper = VISIT_BANDS[VISIT_BANDS.index(b["_id"]) + 1] - 1
                label = str(b["_id"]) if upper == b["_id"] else f"{b['_id']}-{upper}"
            bands.append(FrequencyBand(visits=label, customers=b["n"]))
        return CustomerInsights(
            period=period,
            currency=app.currency,
            total_customers=t.get("n", 0),
            new_customers=facets["new"][0]["n"] if facets["new"] else 0,
            returning_customers=facets["returning"][0]["n"] if facets["returning"] else 0,
            average_visits_per_customer=round(t.get("visits") or 0, 2),
            average_lifetime_spend=lkr(t.get("spent")),
            visit_frequency=bands,
            loyalty_visit_interval=interval,
            customers_due_loyalty_reward=due,
        )

    @mcp.tool(title="Top customers", annotations=READ_ONLY)
    @instrumented
    async def get_top_customers(
        ctx: Context,
        rank_by: Annotated[
            Literal["total_spent", "visit_count", "most_recent"], Field(description="Ranking (all-time figures)")
        ] = "total_spent",
        limit: Annotated[int, Field(ge=1, le=50)] = 10,
    ) -> TopCustomers:
        """Best customers (all time) with parent name and children, ranked by spend, visits or recency.

        Contact details (phone, email) are never returned.
        """
        app = get_app(ctx)
        field = SORT_FIELDS[rank_by]
        docs = await app.db.customers.find({}, CUSTOMER_FIELDS).sort(field, -1).limit(limit).to_list()
        children = await _children_by_phone(app, [d["phoneNumber"] for d in docs if d.get("phoneNumber")])
        return TopCustomers(
            currency=app.currency,
            ranked_by=rank_by,
            customers=[_profile(app, d, children.get(d.get("phoneNumber", ""), [])) for d in docs],
        )

    # Phone lookup is off by default (ENABLE_PHONE_LOOKUP) so the LLM is never invited to handle
    # phone numbers. When off, the tool's description doesn't mention phones and phone queries are refused.
    phone_lookup = get_settings().enable_phone_lookup
    if phone_lookup:
        query_help = "Phone number (or its last digits) or part of the parent's name"
        find_description = (
            "Look up customers by phone number or parent name, with their children, spend and recent visits. "
            "A phone number is accepted as search input only; it is never returned."
        )
    else:
        query_help = "Part of the parent's name"
        find_description = "Look up customers by parent name, with their children, spend and recent visits."

    @mcp.tool(title="Find customer", description=find_description, annotations=READ_ONLY)
    @instrumented
    async def find_customer(
        ctx: Context,
        query: Annotated[str, Field(min_length=2, max_length=50, description=query_help)],
        recent_visits: Annotated[int, Field(ge=0, le=20, description="Recent visits to include per match")] = 5,
    ) -> CustomerSearch:
        app = get_app(ctx)
        digits = re.sub(r"\D", "", query)
        searched_by: Literal["phone", "name"]
        if len(digits) >= 3 and len(digits) >= len(query.replace(" ", "")) - 2:
            if not phone_lookup:
                # Refuse without querying or echoing the number.
                raise ToolError("Searching by phone number is disabled. Search by the parent's name instead.")
            # Numbers are stored both as 07X... and +947X...; match either form.
            patterns = [digits] + (["94" + digits[1:]] if digits.startswith("0") else [])
            flt: Document = {"phoneNumber": {"$regex": "|".join(map(re.escape, patterns))}}
            searched_by = "phone"
        else:
            flt = {"parentName": {"$regex": re.escape(query.strip()), "$options": "i"}}
            searched_by = "name"
        docs = await app.db.customers.find(flt, CUSTOMER_FIELDS).sort("lastVisitAt", -1).limit(10).to_list()
        phones = [d["phoneNumber"] for d in docs if d.get("phoneNumber")]
        children = await _children_by_phone(app, phones)
        matches = []
        for d in docs:
            profile = _profile(app, d, children.get(d.get("phoneNumber", ""), []))
            if recent_visits:
                sessions = (
                    await app.db.playsessions.find(
                        {"phoneNumber": d.get("phoneNumber"), "isTestBill": {"$ne": True}},
                        {
                            "checkInAt": 1,
                            "packageName": 1,
                            "childName": 1,
                            "childNames": 1,
                            "billedMinutes": 1,
                            "chargedAmount": 1,
                            "status": 1,
                        },
                    )
                    .sort("checkInAt", -1)
                    .limit(recent_visits)
                    .to_list()
                )
                profile.recent_visits = [
                    Visit(
                        checked_in_at=local(app, s.get("checkInAt")),
                        package_name=s.get("packageName"),
                        children=s.get("childNames") or [s.get("childName", "")],
                        billed_minutes=s.get("billedMinutes"),
                        charged_amount=lkr(s["chargedAmount"]) if s.get("chargedAmount") is not None else None,
                        status=s.get("status"),
                    )
                    for s in sessions
                ]
            matches.append(profile)
        return CustomerSearch(currency=app.currency, searched_by=searched_by, matches=matches)
