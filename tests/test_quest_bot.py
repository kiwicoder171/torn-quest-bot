import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flight_club import TfcItem
from key_pool import KeyPool, SAFE_MAX_RPM, SAFE_MIN_RPM
from torn_api import TornApiException
from watcher import QuestWatcher, evaluate_listings


def test_key_pool_clamps_rpm_and_toggles(tmp_path):
    storage = tmp_path / "keys.json"
    pool = KeyPool(storage_path=storage)

    # Attempt to set unsafe 120 rpm -> clamped to SAFE_MAX_RPM (50)
    e1 = pool.set_key(101, "Alice", 1001, "KEY_A", max_rpm=120)
    assert e1.max_rpm == SAFE_MAX_RPM
    assert e1.active is True

    # Attempt to set 1 rpm -> clamped to SAFE_MIN_RPM (5)
    e2 = pool.set_key(202, "Bob", 1002, "KEY_B", max_rpm=1)
    assert e2.max_rpm == SAFE_MIN_RPM

    # Pause Alice's key
    pool.pause_key(101)
    assert pool.active_count() == 1
    assert [e.discord_id for e in pool.active_entries()] == [202]

    # Reload from disk and verify state persisted
    reloaded = KeyPool(storage_path=storage)
    assert reloaded.get_entry(101).active is False
    assert reloaded.get_entry(202).active is True

    # Resume Alice and remove Bob
    reloaded.resume_key(101)
    assert reloaded.active_count() == 2
    assert reloaded.remove_key(202) is True
    assert reloaded.active_count() == 1


def test_key_pool_load_balances_across_active_keys():
    async def run():
        pool = KeyPool()
        pool.set_key(1, "Alice", 11, "KEY_1", max_rpm=30)
        pool.set_key(2, "Bob", 22, "KEY_2", max_rpm=30)

        k1 = await pool.acquire_next_key()
        k2 = await pool.acquire_next_key()
        assert k1 is not None and k2 is not None
        assert {k1.discord_id, k2.discord_id} == {1, 2}

        # Pause user 1 -> only user 2 should be selected
        pool.pause_key(1)
        k3 = await pool.acquire_next_key()
        assert k3 is not None and k3.discord_id == 2

    asyncio.run(run())


def test_evaluate_listings_filters_by_tfc_price_and_overpay():
    item = TfcItem(
        item_id=258,
        item_name="Jaguar Plushie",
        fc_value=13677,
        torn_value=14397,
        category="Plushie",
        recipient="MIA",
        contest_points=45,
        nfs_rank=1,
    )
    payload = {
        "itemmarket": {
            "listings": [
                {"price": 14500, "amount": 5},   # Above TFC
                {"price": 13677, "amount": 10},  # Exact TFC (0 profit, free quest pts)
                {"price": 13000, "amount": 3},   # Profitable
            ]
        }
    }

    deals = evaluate_listings(item, payload, max_overpay_pct=0.0)
    assert [d.listed_price for d in deals] == [13000, 13677]
    assert deals[0].profit_per_unit == 677
    assert deals[0].total_profit == 677 * 3
    assert deals[1].profit_per_unit == 0

    # With 10% overpay allowed, 14500 (<= 15044) is also included
    deals_overpay = evaluate_listings(item, payload, max_overpay_pct=10.0)
    assert [d.listed_price for d in deals_overpay] == [13000, 13677, 14500]


class FakeFlightClub:
    def __init__(self, item: TfcItem):
        self.item = item

    async def get_item(self, item_id: int):
        return self.item if item_id == self.item.item_id else None


class FakeTornApi:
    INVALID_KEY_CODES = {1, 2, 10, 18}

    def __init__(self, payload=None, error=None):
        self.payload = payload or {}
        self.error = error

    async def fetch_item_market(self, item_id: int, key_entry):
        if self.error:
            if self.error.code in self.INVALID_KEY_CODES:
                key_entry.active = False
            raise self.error
        return self.payload


def test_watcher_scan_once_deduplicates_and_notifies():
    async def run():
        pool = KeyPool()
        pool.set_key(1, "Alice", 11, "KEY_1", max_rpm=35)
        tfc_item = TfcItem(
            item_id=385,
            item_name="Tribulus Omanense",
            fc_value=61249,
            torn_value=64473,
            category="Flower",
            recipient="MIA",
            contest_points=204,
            nfs_rank=1,
        )
        payload = {"itemmarket": {"listings": [{"price": 60000, "amount": 4}]}}
        deals_sent = []

        async def on_deal(deal):
            deals_sent.append(deal)

        watcher = QuestWatcher(
            key_pool=pool,
            flight_club=FakeFlightClub(tfc_item),
            torn_api=FakeTornApi(payload=payload),
            on_deal=on_deal,
        )
        watcher.add_quest_item(385, "Tribulus Omanense")

        # First scan alerts
        first = await watcher.scan_once()
        assert len(first) == 1
        assert len(deals_sent) == 1
        assert deals_sent[0].listed_price == 60000

        # Second scan with identical listing does not re-alert
        second = await watcher.scan_once()
        assert len(second) == 0
        assert len(deals_sent) == 1

        # Partial buy (amount drops from 4 -> 2 at same price) does not re-alert
        watcher.torn_api.payload = {"itemmarket": {"listings": [{"price": 60000, "amount": 2}]}}
        third = await watcher.scan_once()
        assert len(third) == 0
        assert len(deals_sent) == 1

        # Item sells out completely, then a brand-new listing at 60000 appears -> alerts once
        watcher.torn_api.payload = {"itemmarket": {"listings": []}}
        await watcher.scan_once()
        watcher.torn_api.payload = {"itemmarket": {"listings": [{"price": 60000, "amount": 1}]}}
        fourth = await watcher.scan_once()
        assert len(fourth) == 1
        assert len(deals_sent) == 2

    asyncio.run(run())


def test_watcher_auto_pauses_invalid_key_and_notifies():
    async def run():
        pool = KeyPool()
        pool.set_key(1, "Alice", 11, "BAD_KEY", max_rpm=35)
        tfc_item = TfcItem(
            item_id=385,
            item_name="Tribulus Omanense",
            fc_value=61249,
            torn_value=64473,
            category="Flower",
            recipient="MIA",
        )
        notices = []

        async def on_notice(msg: str):
            notices.append(msg)

        watcher = QuestWatcher(
            key_pool=pool,
            flight_club=FakeFlightClub(tfc_item),
            torn_api=FakeTornApi(error=TornApiException(2, "Incorrect key")),
            on_notice=on_notice,
        )
        watcher.add_quest_item(385, "Tribulus Omanense")

        await watcher.scan_once()
        assert pool.active_count() == 0
        assert len(notices) == 1
        assert "Auto-paused" in notices[0]

    asyncio.run(run())


def test_add_nfs_items_from_tfc_list():
    pool = KeyPool()
    items = [
        TfcItem(item_id=385, item_name="Tribulus Omanense", fc_value=61249, torn_value=64473, category="Flower", recipient="MIA", nfs_rank=1),
        TfcItem(item_id=258, item_name="Jaguar Plushie", fc_value=13677, torn_value=14397, category="Plushie", recipient="MIA", nfs_rank=1),
        TfcItem(item_id=273, item_name="Chamois Plushie", fc_value=14165, torn_value=14911, category="Plushie", recipient="MIA", nfs_rank=None),
    ]
    watcher = QuestWatcher(
        key_pool=pool,
        flight_club=FakeFlightClub(items[0]),
        torn_api=FakeTornApi(),
    )
    nfs_only = [it for it in items if it.nfs_rank is not None]
    for it in nfs_only:
        watcher.add_quest_item(it.item_id, it.item_name)

    assert set(watcher.quest_items.keys()) == {385, 258}
