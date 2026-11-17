import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional
import aiohttp

logger = logging.getLogger("TornQuestBot.FlightClub")


@dataclass(frozen=True)
class TfcItem:
    item_id: int
    item_name: str
    fc_value: int
    torn_value: int
    category: str
    recipient: str
    contest_points: Optional[int] = None
    nfs_rank: Optional[int] = None


class FlightClubClient:
    """
    Async client for Torn Flight Club's public item pricing API
    (https://tornflight.club/api/items/).
    """

    API_URL = "https://tornflight.club/api/items/"
    RECIPIENT_MIA_CATEGORIES = {
        "Plushie",
        "Flower",
        "Arrowhead",
        "Fossil",
        "Amulet",
        "Vairocana",
        "Senet",
        "Shabti",
    }

    def __init__(
        self,
        cache_ttl_seconds: float = 300.0,
        session: Optional[aiohttp.ClientSession] = None,
    ):
        self.cache_ttl = cache_ttl_seconds
        self._external_session = session is not None
        self._session = session
        self._last_fetch: float = 0.0
        self._items_by_id: dict[int, TfcItem] = {}
        self._lock = asyncio.Lock()

    async def get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if not self._external_session and self._session and not self._session.closed:
            await self._session.close()

    async def get_item(self, item_id: int) -> Optional[TfcItem]:
        await self._ensure_cache_updated()
        return self._items_by_id.get(item_id)

    async def get_all_items(self) -> dict[int, TfcItem]:
        await self._ensure_cache_updated()
        return self._items_by_id

    async def find_item(self, query: str) -> Optional[TfcItem]:
        """Resolve an item by numeric ID string or case-insensitive name."""
        await self._ensure_cache_updated()
        q = query.strip()
        if q.isdigit():
            return self._items_by_id.get(int(q))

        q_lower = q.lower()
        # Exact match first
        for item in self._items_by_id.values():
            if item.item_name.lower() == q_lower:
                return item
        # Prefix / substring match
        for item in self._items_by_id.values():
            if q_lower in item.item_name.lower():
                return item
        return None

    async def search_items(self, query: str, limit: int = 25) -> list[TfcItem]:
        """Return matching TFC items for Discord slash command autocomplete."""
        await self._ensure_cache_updated()
        q = query.strip().lower()
        items = list(self._items_by_id.values())
        if not q:
            # Prioritize NFS items first, then alphabetically
            items.sort(key=lambda x: (x.nfs_rank is None, x.nfs_rank or 99, x.item_name))
            return items[:limit]

        matches = [
            it for it in items
            if q in it.item_name.lower() or q == str(it.item_id)
        ]
        matches.sort(key=lambda x: (not x.item_name.lower().startswith(q), x.item_name))
        return matches[:limit]

    async def _ensure_cache_updated(self) -> None:
        now = time.monotonic()
        if self._items_by_id and (now - self._last_fetch) < self.cache_ttl:
            return

        async with self._lock:
            now = time.monotonic()
            if self._items_by_id and (now - self._last_fetch) < self.cache_ttl:
                return

            try:
                session = await self.get_session()
                headers = {"User-Agent": "TornQuestBot/1.0"}
                async with session.get(self.API_URL, headers=headers, timeout=10.0) as resp:
                    if resp.status != 200:
                        logger.warning(f"Failed to fetch TFC items (HTTP {resp.status})")
                        return

                    data = await resp.json()
                    new_map: dict[int, TfcItem] = {}
                    for it in data.get("items", []):
                        item_id = it.get("item_id")
                        if not item_id:
                            continue
                        category = it.get("category", "") or ""
                        recipient = "MIA" if category in self.RECIPIENT_MIA_CATEGORIES else "GalloInfligo"
                        new_map[int(item_id)] = TfcItem(
                            item_id=int(item_id),
                            item_name=str(it.get("item_name", f"Item #{item_id}")),
                            fc_value=int(it.get("fc_value") or 0),
                            torn_value=int(it.get("torn_value") or 0),
                            category=category,
                            recipient=recipient,
                            contest_points=it.get("contest_points"),
                            nfs_rank=it.get("nfs_rank"),
                        )

                    self._items_by_id = new_map
                    self._last_fetch = time.monotonic()
                    logger.info(f"Updated TFC price cache ({len(new_map)} items)")
            except Exception as exc:
                logger.error(f"Error updating TFC price cache: {exc}")
