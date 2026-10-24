import inspect
import logging
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from pymongo.errors import ServerSelectionTimeoutError

from jellybeans_mcp.db import AppContext
from jellybeans_mcp.tools.common import (
    DB_UNAVAILABLE,
    DEFAULT_DAYS,
    Period,
    bucket_count,
    bucket_start,
    check_buckets,
    instrumented,
    lkr,
    redact,
    resolve_period,
    shift,
)

APP = AppContext(db=None, tz=ZoneInfo("Asia/Colombo"), currency="LKR", max_time_ms=1000)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Call mum on 077 123 4567 later", "Call mum on [phone removed] later"),
        ("parent +94771234567 asked", "parent [phone removed] asked"),
        ("email jane.doe@example.com re refund", "email [email removed] re refund"),
        ("Checkout undone at the till before payment", "Checkout undone at the till before payment"),
        ("Refund Rs 600 for 2 kids", "Refund Rs 600 for 2 kids"),
        (None, None),
    ],
)
def test_redact_masks_contact_details(text, expected):
    assert redact(text) == expected


def test_lkr_converts_cents():
    assert lkr(60000) == 600.0
    assert lkr(120050) == 1200.5
    assert lkr(None) == 0.0


def test_period_is_local_days_in_utc():
    period, rng = resolve_period(APP, date(2026, 10, 1), date(2026, 10, 18))
    assert (period.start, period.end) == (date(2026, 10, 1), date(2026, 10, 18))
    # Colombo is UTC+05:30, so local midnight is 18:30 UTC the day before.
    assert rng["$gte"] == datetime(2026, 9, 30, 18, 30, tzinfo=UTC)
    assert rng["$lt"] == datetime(2026, 10, 18, 18, 30, tzinfo=UTC)


def test_period_defaults_to_last_30_days():
    period, _ = resolve_period(APP, None, None)
    assert period.end == datetime.now(APP.tz).date()
    assert period.end - period.start == timedelta(days=DEFAULT_DAYS - 1)


@pytest.mark.parametrize(
    ("start", "end"), [(date(2026, 10, 10), date(2026, 10, 1)), (date(2015, 1, 1), date(2026, 1, 1))]
)
def test_invalid_periods_raise_tool_error(start, end):
    with pytest.raises(ToolError):
        resolve_period(APP, start, end)


async def test_instrumented_hides_database_errors(caplog):
    caplog.set_level(logging.INFO)

    @instrumented
    async def broken() -> None:
        raise ServerSelectionTimeoutError("cluster0.example.mongodb.net:27017: timed out")

    with pytest.raises(ToolError) as exc:
        await broken()
    assert str(exc.value) == DB_UNAVAILABLE
    assert "mongodb.net" not in str(exc.value)
    assert "tool=broken error" in caplog.text


async def test_instrumented_passes_tool_errors_and_keeps_signature(caplog):
    caplog.set_level(logging.INFO)

    async def tool(start: date | None = None, limit: int = 10) -> int:
        """Doc."""
        if limit < 0:
            raise ToolError("limit must be positive")
        return limit

    wrapped = instrumented(tool)
    assert inspect.signature(wrapped) == inspect.signature(tool)
    assert wrapped.__doc__ == "Doc." and wrapped.__name__ == "tool"
    assert await wrapped(limit=3) == 3
    with pytest.raises(ToolError, match="limit must be positive"):
        await wrapped(limit=-1)
    assert "tool=tool ok duration_ms=" in caplog.text


def test_long_periods_up_to_ten_years():
    period, _ = resolve_period(APP, date(2021, 1, 1), date(2025, 12, 31))
    assert period.start == date(2021, 1, 1)


@pytest.mark.parametrize(
    ("start", "end", "granularity", "expected"),
    [
        (date(2026, 1, 1), date(2026, 12, 31), "month", 12),
        (date(2025, 11, 15), date(2026, 2, 3), "month", 4),
        (date(2017, 6, 1), date(2026, 1, 1), "year", 10),
        (date(2026, 10, 1), date(2026, 10, 18), "day", 18),
        (date(2026, 10, 1), date(2026, 10, 18), "week", 3),
    ],
)
def test_bucket_count(start, end, granularity, expected):
    assert bucket_count(start, end, granularity) == expected


def test_check_buckets_limits_points():
    check_buckets(Period(start=date(2016, 1, 1), end=date(2025, 12, 31), timezone="Asia/Colombo"), "month")
    with pytest.raises(ToolError, match="coarser granularity"):
        check_buckets(Period(start=date(2024, 1, 1), end=date(2025, 12, 31), timezone="Asia/Colombo"), "day")


def test_shift_months_and_years():
    assert shift(date(2026, 1, 1), "month", -1) == date(2025, 12, 1)
    assert shift(date(2026, 10, 1), "month", -12) == date(2025, 10, 1)
    assert shift(date(2026, 1, 1), "year", -1) == date(2025, 1, 1)
    assert bucket_start(date(2026, 10, 24), "month") == date(2026, 10, 1)
    assert bucket_start(date(2026, 10, 24), "week") == date(2026, 10, 19)
