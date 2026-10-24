"""End-to-end tests through an in-memory MCP client against the configured (read-only) database."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from mcp import Client
from pydantic import ValidationError

from jellybeans_mcp.config import get_settings

try:
    get_settings()
    configured = True
except ValidationError:
    configured = False

pytestmark = [pytest.mark.integration, pytest.mark.skipif(not configured, reason="MONGODB_URI not configured")]

PERIOD = {"start_date": "2026-10-01", "end_date": "2026-10-18"}


@asynccontextmanager
async def connect() -> AsyncIterator[Client]:
    # Opened inside each test (not a fixture): the client's anyio task group must be
    # entered and exited in the same task, which pytest-asyncio fixtures don't guarantee.
    from jellybeans_mcp.server import create_server

    async with Client(create_server()) as client:
        yield client


async def test_all_tools_are_read_only_with_output_schema():
    async with connect() as client:
        tools = (await client.list_tools()).tools
    assert len(tools) == 15
    for tool in tools:
        assert tool.annotations and tool.annotations.read_only_hint, tool.name
        assert tool.output_schema, tool.name
        assert tool.description, tool.name


async def test_every_tool_runs():
    args = {
        "get_top_customers": {"limit": 3},
        "find_customer": {"query": "07"},
        "get_business_info": {},
        "get_current_occupancy": {},
    }
    async with connect() as client:
        for tool in (await client.list_tools()).tools:
            result = await client.call_tool(tool.name, args.get(tool.name, PERIOD))
            assert not result.is_error, (tool.name, result.content)


async def test_revenue_figures_are_consistent():
    async with connect() as client:
        summary = (await client.call_tool("get_revenue_summary", PERIOD)).structured_content
        trend = (await client.call_tool("get_revenue_trend", PERIOD)).structured_content
    by_method = sum(m["amount"] for m in summary["by_payment_method"])
    by_day = sum(p["net_revenue"] for p in trend["points"])
    assert by_method == pytest.approx(summary["net_revenue"])
    assert by_day == pytest.approx(summary["net_revenue"])
    assert len(trend["points"]) == 18


async def test_monthly_performance_matches_other_tools():
    october = {"start_date": "2026-10-01", "end_date": "2026-10-31"}
    async with connect() as client:
        perf = (
            await client.call_tool("get_period_performance", {"granularity": "month", **october})
        ).structured_content
        summary = (await client.call_tool("get_revenue_summary", october)).structured_content
        visits = (await client.call_tool("get_visit_stats", october)).structured_content
    [row] = perf["rows"]
    assert row["label"] == "2026-10"
    assert row["net_revenue"] == pytest.approx(summary["net_revenue"])
    assert row["paid_bills"] == summary["paid_bills"]
    assert row["total_children"] == visits["total_children"]


async def test_yearly_performance_equals_sum_of_months():
    async with connect() as client:
        years = (await client.call_tool("get_period_performance", {"granularity": "year"})).structured_content
        months = (
            await client.call_tool(
                "get_period_performance",
                {"granularity": "month", "start_date": "2026-01-01", "end_date": "2026-12-31"},
            )
        ).structured_content
        trend = await client.call_tool("get_revenue_trend", {"granularity": "year", "start_date": "2020-01-01"})
        too_many = await client.call_tool("get_revenue_trend", {"granularity": "day", "start_date": "2023-01-01"})
    row_2026 = next(r for r in years["rows"] if r["label"] == "2026")
    assert len(months["rows"]) == 12
    assert row_2026["net_revenue"] == pytest.approx(sum(r["net_revenue"] for r in months["rows"]))
    assert not trend.is_error
    assert too_many.is_error and "coarser granularity" in too_many.content[0].text


async def test_invalid_period_error_reaches_client():
    async with connect() as client:
        result = await client.call_tool("get_revenue_summary", {"start_date": "2026-10-10", "end_date": "2026-10-01"})
    assert result.is_error
    assert "after end_date" in result.content[0].text


async def _contact_details() -> tuple[set[str], set[str]]:
    """Every phone number (raw and digits-only) and email stored in the database."""
    from pymongo import AsyncMongoClient

    settings = get_settings()
    mongo: AsyncMongoClient = AsyncMongoClient(settings.mongodb_uri.get_secret_value())
    db = mongo[settings.mongodb_db]
    phones: set[str] = set()
    emails: set[str] = set()
    try:
        for coll in ("customers", "playsessions", "bills", "businesssettings"):
            async for doc in db[coll].find({}, {"phoneNumber": 1, "email": 1}):
                if phone := (doc.get("phoneNumber") or "").strip():
                    phones.add(phone)
                    phones.add("".join(ch for ch in phone if ch.isdigit()))
                if email := (doc.get("email") or "").strip():
                    emails.add(email.lower())
    finally:
        await mongo.close()
    # Very short values (e.g. test data like "123456") would match amounts and dates by chance.
    return {p for p in phones if len(p) >= 7}, emails


async def test_no_contact_details_in_any_output(monkeypatch):
    # Worst case: phone lookup enabled, so a real number goes in and must not come back out.
    monkeypatch.setattr(get_settings(), "enable_phone_lookup", True)
    phones, emails = await _contact_details()
    assert phones, "expected phone numbers in the database to test against"
    sample_phone = max(phones, key=len)
    calls = {
        "get_top_customers": [{"limit": 50, "rank_by": rank} for rank in ("total_spent", "visit_count", "most_recent")],
        # A real phone number as input: the customer must be found, the number must not come back.
        "find_customer": [{"query": sample_phone, "recent_visits": 20}, {"query": "an", "recent_visits": 20}],
        "get_business_info": [{}],
        "get_current_occupancy": [{}],
    }
    async with connect() as client:
        for tool in (await client.list_tools()).tools:
            for args in calls.get(tool.name, [PERIOD]):
                result = await client.call_tool(tool.name, args)
                assert not result.is_error, (tool.name, result.content)
                text = str(result.structured_content) + " ".join(getattr(c, "text", "") for c in result.content)
                leaked_phones = [p for p in phones if p in text]
                leaked_emails = [e for e in emails if e in text.lower()]
                assert not leaked_phones and not leaked_emails, f"{tool.name} leaked contact details"
                if tool.name == "find_customer" and args["query"] == sample_phone:
                    assert result.structured_content["searched_by"] == "phone"


async def test_phone_lookup_disabled_by_default(monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_phone_lookup", False)
    async with connect() as client:
        tool = next(t for t in (await client.list_tools()).tools if t.name == "find_customer")
        result = await client.call_tool("find_customer", {"query": "0771234567"})
        by_name = await client.call_tool("find_customer", {"query": "an"})
    assert "phone" not in tool.description.lower()
    assert "phone" not in str(tool.input_schema).lower()
    assert result.is_error
    assert "disabled" in result.content[0].text
    assert "0771234567" not in result.content[0].text
    assert not by_name.is_error and by_name.structured_content["searched_by"] == "name"


async def test_guide_resource():
    async with connect() as client:
        result = await client.read_resource("jellybeans://guide")
    assert "Revenue (net)" in result.contents[0].text
