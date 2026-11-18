import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from flight_club import FlightClubClient, TfcItem
from key_pool import KeyPool
from torn_api import TornApiClient, TornApiException

logger = logging.getLogger("TornQuestBot.Watcher")


@dataclass(frozen=True)
class QuestDeal:
    item_id: int
    item_name: str
    category: str
    recipient: str
    listed_price: int
    amount: int
    fc_value: int
    target_price: int
    profit_per_unit: int
    total_profit: int
    market_url: str
    nfs_rank: Optional[int] = None
    contest_points: Optional[int] = None


@dataclass
class QuestItem:
    item_id: int
    item_name: str
    max_overpay_pct: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "item_name": self.item_name,
            "max_overpay_pct": self.max_overpay_pct,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "QuestItem":
        return cls(
            item_id=int(data["item_id"]),
            item_name=str(data["item_name"]),
            max_overpay_pct=float(data.get("max_overpay_pct", 0.0)),
        )


class TTLCache:
    """In-memory deduplication cache so identical market listings aren't re-pinged."""

    def __init__(self, ttl_seconds: float = 120.0):
        self.ttl_seconds = ttl_seconds
        self._store: dict[str, float] = {}

    def seen_recently(self, key: str) -> bool:
        now = time.monotonic()
        self._purge(now)
        return key in self._store

    def mark(self, key: str) -> None:
        now = time.monotonic()
        self._purge(now)
        self._store[key] = now

    def _purge(self, now: float) -> None:
        expired = [k for k, ts in self._store.items() if (now - ts) >= self.ttl_seconds]
        for k in expired:
            del self._store[k]


def evaluate_listings(
    tfc_item: TfcItem,
    market_payload: dict[str, Any],
    max_overpay_pct: float = 0.0,
) -> list[QuestDeal]:
    """
    Compare Torn Item Market listings against TFC's `fc_value`.
    Buyer pays 0% fee on Item Market, so any listing <= target_price is a match.
    """
    if tfc_item.fc_value <= 0:
        return []

    target_price = int(tfc_item.fc_value * (1.0 + max_overpay_pct / 100.0))
    listings = (market_payload.get("itemmarket") or {}).get("listings") or []
    deals: list[QuestDeal] = []

    for listing in listings:
        price = int(listing.get("price") or 0)
        amount = int(listing.get("amount") or 0)
        if price <= 0 or amount <= 0:
            continue
        if price <= target_price:
            profit_per_unit = tfc_item.fc_value - price
            deals.append(
                QuestDeal(
                    item_id=tfc_item.item_id,
                    item_name=tfc_item.item_name,
                    category=tfc_item.category,
                    recipient=tfc_item.recipient,
                    listed_price=price,
                    amount=amount,
                    fc_value=tfc_item.fc_value,
                    target_price=target_price,
                    profit_per_unit=profit_per_unit,
                    total_profit=profit_per_unit * amount,
                    market_url=f"https://www.torn.com/page.php?sid=ItemMarket#/market/view=search&itemID={tfc_item.item_id}",
                    nfs_rank=tfc_item.nfs_rank,
                    contest_points=tfc_item.contest_points,
                )
            )

    deals.sort(key=lambda d: d.listed_price)
    return deals


class QuestWatcher:
    """
    Background market watcher coordinating KeyPool, FlightClubClient, and TornApiClient.
    """

    def __init__(
        self,
        key_pool: KeyPool,
        flight_club: FlightClubClient,
        torn_api: TornApiClient,
        storage_path: Optional[Path] = None,
        scan_interval: float = 3.0,
        dedup_ttl: float = 120.0,
        on_deal: Optional[Callable[[QuestDeal], Awaitable[None]]] = None,
        on_notice: Optional[Callable[[str], Awaitable[None]]] = None,
    ):
        self.key_pool = key_pool
        self.flight_club = flight_club
        self.torn_api = torn_api
        self.storage_path = storage_path
        self.scan_interval = scan_interval
        self.dedup_cache = TTLCache(ttl_seconds=dedup_ttl)
        self.on_deal = on_deal
        self.on_notice = on_notice

        self.watching: bool = False
        self.alert_channel_id: Optional[int] = None
        self.quest_items: dict[int, QuestItem] = {}
        self.subscribers: set[int] = set()
        # Tracks active alerted price tiers per item: {item_id: {listed_price: total_amount}}
        self._active_prices: dict[int, dict[int, int]] = {}
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

        if self.storage_path and self.storage_path.exists():
            self._load()

    def add_quest_item(self, item_id: int, item_name: str, max_overpay_pct: float = 0.0) -> QuestItem:
        qi = QuestItem(item_id=item_id, item_name=item_name, max_overpay_pct=max_overpay_pct)
        self.quest_items[item_id] = qi
        self._save()
        return qi

    def remove_quest_item(self, item_id: int) -> bool:
        if item_id not in self.quest_items:
            return False
        del self.quest_items[item_id]
        self._save()
        return True

    def clear_quest_items(self) -> int:
        count = len(self.quest_items)
        self.quest_items.clear()
        self._save()
        return count

    def set_subscriber(self, discord_id: int, enabled: bool) -> None:
        if enabled:
            self.subscribers.add(discord_id)
        else:
            self.subscribers.discard(discord_id)
        self._save()

    def start(self, channel_id: int) -> None:
        self.alert_channel_id = channel_id
        self.watching = True
        self._save()
        if self._task is None or self._task.done():
            self._stop_event.clear()
            self._task = asyncio.create_task(self._run_loop())

    def stop(self) -> None:
        self.watching = False
        self._save()
        self._stop_event.set()

    async def scan_once(self) -> list[QuestDeal]:
        """Run one scan pass across all active quest items."""
        if not self.quest_items:
            return []
        if self.key_pool.active_count() == 0:
            return []

        found_deals: list[QuestDeal] = []
        for q_item in list(self.quest_items.values()):
            if not self.watching and self._stop_event.is_set():
                break

            tfc_item = await self.flight_club.get_item(q_item.item_id)
            if not tfc_item or tfc_item.fc_value <= 0:
                continue

            key_entry = await self.key_pool.acquire_next_key()
            if not key_entry:
                break

            try:
                payload = await self.torn_api.fetch_item_market(q_item.item_id, key_entry)
            except TornApiException as exc:
                if exc.code in self.torn_api.INVALID_KEY_CODES and self.on_notice:
                    await self.on_notice(
                        f"⚠️ Auto-paused <@{key_entry.discord_id}>'s API key (`{key_entry.torn_name}`): {exc.message}"
                    )
                continue
            except Exception as exc:
                logger.warning(f"Market fetch failed for item {q_item.item_id}: {exc}")
                continue

            deals = evaluate_listings(tfc_item, payload, q_item.max_overpay_pct)
            prev_prices = self._active_prices.get(q_item.item_id, {})
            current_prices: dict[int, int] = {}
            for deal in deals:
                current_prices[deal.listed_price] = current_prices.get(deal.listed_price, 0) + deal.amount

            alerted_prices_this_scan: set[int] = set()
            for deal in deals:
                price = deal.listed_price
                if price in alerted_prices_this_scan:
                    continue
                prev_amount = prev_prices.get(price)
                # Ping only if this price wasn't already on the market, or if new stock was added at this price
                if prev_amount is None or current_prices[price] > prev_amount:
                    alerted_prices_this_scan.add(price)
                    found_deals.append(deal)
                    if self.on_deal:
                        await self.on_deal(deal)

            self._active_prices[q_item.item_id] = current_prices

        return found_deals

    async def _run_loop(self) -> None:
        logger.info("QuestWatcher loop started.")
        while self.watching and not self._stop_event.is_set():
            try:
                await self.scan_once()
            except Exception as exc:
                logger.error(f"Unexpected error in QuestWatcher loop: {exc}")

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.scan_interval)
            except asyncio.TimeoutError:
                pass
        logger.info("QuestWatcher loop stopped.")

    def _load(self) -> None:
        if not self.storage_path or not self.storage_path.exists():
            return
        try:
            raw = json.loads(self.storage_path.read_text(encoding="utf-8"))
            self.alert_channel_id = raw.get("alert_channel_id")
            self.watching = bool(raw.get("watching", False))
            self.subscribers = {int(uid) for uid in raw.get("subscribers", [])}
            for item in raw.get("quest_items", []):
                qi = QuestItem.from_dict(item)
                self.quest_items[qi.item_id] = qi
        except Exception:
            pass

    def _save(self) -> None:
        if not self.storage_path:
            return
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "alert_channel_id": self.alert_channel_id,
            "watching": self.watching,
            "subscribers": sorted(self.subscribers),
            "quest_items": [qi.to_dict() for qi in self.quest_items.values()],
        }
        self.storage_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
