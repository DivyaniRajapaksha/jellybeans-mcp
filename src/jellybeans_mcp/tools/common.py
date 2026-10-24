import functools
import logging
import re
import time as clock
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field
from pymongo.errors import PyMongoError

from jellybeans_mcp.db import AppContext, Document

READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)

DB_UNAVAILABLE = "The database is temporarily unavailable; please try again shortly."


def instrumented[**P, R](fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Log each tool call's outcome and duration, and turn database failures into a safe ToolError.

    Arguments and results are never logged: they can contain customer names or a typed phone number.
    """

    log = logging.getLogger(fn.__module__)  # one logger per tool module

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        started = clock.perf_counter()
        outcome = "error"
        try:
            result = await fn(*args, **kwargs)
            outcome = "ok"
            return result
        except PyMongoError:
            # Full details (hosts, codes) stay in the server log; the LLM gets a generic message.
            log.exception("tool=%s database error", fn.__name__)
            raise ToolError(DB_UNAVAILABLE) from None
        finally:
            log.info("tool=%s %s duration_ms=%d", fn.__name__, outcome, (clock.perf_counter() - started) * 1000)

    return wrapper


DEFAULT_DAYS = 30
MAX_DAYS = 3660  # about 10 years
MAX_POINTS = 400  # largest trend/table returned to the LLM

Granularity = Literal["day", "week", "month", "year"]

StartDate = Annotated[
    date | None,
    Field(
        description="First day of the period (inclusive, YYYY-MM-DD, business local time). "
        f"Defaults to {DEFAULT_DAYS - 1} days before end_date."
    ),
]
EndDate = Annotated[
    date | None,
    Field(description="Last day of the period (inclusive, YYYY-MM-DD, business local time). Defaults to today."),
]

# Number of children in a play session; older sessions only have childName.
CHILDREN_EXPR: Document = {
    "$ifNull": ["$childCount", {"$cond": [{"$isArray": "$childNames"}, {"$size": "$childNames"}, 1]}]
}

# Bill line type; older items have no `kind`, so infer it from productId.
ITEM_KIND_EXPR: Document = {
    "$ifNull": ["$items.kind", {"$cond": [{"$gt": ["$items.productId", None]}, "PRODUCT", "PLAY"]}]
}


class Period(BaseModel):
    start: date
    end: date
    timezone: str


def get_app(ctx: Context) -> AppContext:
    return ctx.request_context.lifespan_context


def lkr(cents: Any) -> float:
    """Convert a sum of integer cents to rupees, rounded to 2 decimals."""
    return round(float(cents or 0) / 100, 2)


def local(app: AppContext, value: datetime | None) -> datetime | None:
    """Render a stored UTC datetime in the business timezone."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(app.tz)


EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
# 7+ digits, optionally starting with + and separated by spaces, dots or dashes.
PHONE_RE = re.compile(r"\+?\d(?:[\s.-]?\d){6,}")


def redact(text: str | None) -> str | None:
    """Mask emails and phone numbers in free text typed by staff (reasons, notes)."""
    if not text:
        return text
    return PHONE_RE.sub("[phone removed]", EMAIL_RE.sub("[email removed]", text))


def resolve_period(
    app: AppContext, start_date: date | None, end_date: date | None, default_days: int = DEFAULT_DAYS
) -> tuple[Period, Document]:
    """Turn local calendar dates into a UTC range filter: {"$gte": start, "$lt": day after end}."""
    end = end_date or datetime.now(app.tz).date()
    start = start_date or end - timedelta(days=default_days - 1)
    if start > end:
        raise ToolError(f"start_date {start} is after end_date {end}")
    if (end - start).days + 1 > MAX_DAYS:
        raise ToolError(f"Period is longer than {MAX_DAYS} days (about 10 years); narrow the date range")
    return Period(start=start, end=end, timezone=app.tz.key), utc_range(app, start, end)


def utc_range(app: AppContext, start: date, end: date) -> Document:
    """Inclusive local calendar days -> UTC filter {"$gte": start midnight, "$lt": midnight after end}."""
    start_utc = datetime.combine(start, time.min, app.tz).astimezone(UTC)
    end_utc = datetime.combine(end + timedelta(days=1), time.min, app.tz).astimezone(UTC)
    return {"$gte": start_utc, "$lt": end_utc}


def bucket_start(d: date, granularity: Granularity) -> date:
    if granularity == "week":
        return d - timedelta(days=d.weekday())
    if granularity == "month":
        return d.replace(day=1)
    if granularity == "year":
        return d.replace(month=1, day=1)
    return d


def shift(d: date, granularity: Granularity, n: int) -> date:
    """Move a bucket start by n buckets (negative goes back)."""
    if granularity == "day":
        return d + timedelta(days=n)
    if granularity == "week":
        return d + timedelta(weeks=n)
    if granularity == "month":
        months = d.year * 12 + d.month - 1 + n
        return date(months // 12, months % 12 + 1, 1)
    return date(d.year + n, 1, 1)


def bucket_count(start: date, end: date, granularity: Granularity) -> int:
    first, last = bucket_start(start, granularity), bucket_start(end, granularity)
    if granularity == "day":
        return (last - first).days + 1
    if granularity == "week":
        return (last - first).days // 7 + 1
    if granularity == "month":
        return (last.year - first.year) * 12 + last.month - first.month + 1
    return last.year - first.year + 1


def check_buckets(period: Period, granularity: Granularity, limit: int = MAX_POINTS) -> None:
    n = bucket_count(period.start, period.end, granularity)
    if n > limit:
        raise ToolError(
            f"That range gives {n} {granularity} points (max {limit}); "
            "use a coarser granularity (week/month/year) or a shorter range"
        )


def paid_bills(paid_at: Document) -> Document:
    return {"status": "PAID", "isTestBill": {"$ne": True}, "paidAt": paid_at}


def valid_sessions(check_in_at: Document) -> Document:
    return {"status": {"$ne": "VOIDED"}, "isTestBill": {"$ne": True}, "checkInAt": check_in_at}
