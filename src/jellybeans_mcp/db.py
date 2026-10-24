"""MongoDB connection lifecycle and query helpers."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

from jellybeans_mcp.config import Settings, get_settings

logger = logging.getLogger(__name__)

Document = dict[str, Any]


@dataclass(frozen=True)
class AppContext:
    db: AsyncDatabase[Document]
    tz: ZoneInfo
    currency: str
    max_time_ms: int

    async def aggregate(self, collection: str, pipeline: list[Document]) -> list[Document]:
        cursor = await self.db[collection].aggregate(pipeline, maxTimeMS=self.max_time_ms)
        return await cursor.to_list()


async def _business_defaults(db: AsyncDatabase[Document], settings: Settings) -> tuple[ZoneInfo, str]:
    doc = await db.businesssettings.find_one({}, {"timezone": 1, "currency": 1})
    tz_name = (doc or {}).get("timezone") or settings.timezone
    currency = (doc or {}).get("currency") or "LKR"
    return ZoneInfo(tz_name), currency


@asynccontextmanager
async def lifespan(_server: object) -> AsyncIterator[AppContext]:
    settings = get_settings()
    client: AsyncMongoClient[Document] = AsyncMongoClient(
        settings.mongodb_uri.get_secret_value(),
        appname="jellybeans-mcp",
        serverSelectionTimeoutMS=10_000,
        tz_aware=True,
    )
    try:
        await client.admin.command("ping")
        db = client[settings.mongodb_db]
        tz, currency = await _business_defaults(db, settings)
        logger.info("Connected to MongoDB database %r (timezone %s)", settings.mongodb_db, tz.key)
        yield AppContext(db=db, tz=tz, currency=currency, max_time_ms=settings.mongodb_max_time_ms)
    finally:
        await client.close()
