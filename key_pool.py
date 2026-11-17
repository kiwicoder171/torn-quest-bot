import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from rate_limiter import SlidingWindowRateLimiter


SAFE_DEFAULT_MAX_RPM = 35
SAFE_MIN_RPM = 5
SAFE_MAX_RPM = 50  # Hard safety ceiling: leaves >=50 req/min headroom under Torn's 100 req/min limit


@dataclass
class KeyEntry:
    discord_id: int
    torn_name: str
    torn_id: int
    api_key: str
    active: bool = True
    max_rpm: int = SAFE_DEFAULT_MAX_RPM
    calls_made: int = 0
    paused_reason: Optional[str] = None
    added_at: float = field(default_factory=time.time)
    limiter: SlidingWindowRateLimiter = field(init=False, repr=False)

    def __post_init__(self) -> None:
        clamped = max(SAFE_MIN_RPM, min(SAFE_MAX_RPM, int(self.max_rpm)))
        self.max_rpm = clamped
        self.limiter = SlidingWindowRateLimiter(
            max_calls=clamped,
            period_seconds=60.0,
            smooth_spacing=True,
        )

    def to_dict(self) -> dict:
        return {
            "discord_id": self.discord_id,
            "torn_name": self.torn_name,
            "torn_id": self.torn_id,
            "api_key": self.api_key,
            "active": self.active,
            "max_rpm": self.max_rpm,
            "calls_made": self.calls_made,
            "paused_reason": self.paused_reason,
            "added_at": self.added_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "KeyEntry":
        return cls(
            discord_id=int(data["discord_id"]),
            torn_name=str(data.get("torn_name", "Unknown")),
            torn_id=int(data.get("torn_id", 0)),
            api_key=str(data["api_key"]),
            active=bool(data.get("active", True)),
            max_rpm=int(data.get("max_rpm", SAFE_DEFAULT_MAX_RPM)),
            calls_made=int(data.get("calls_made", 0)),
            paused_reason=data.get("paused_reason"),
            added_at=float(data.get("added_at", time.time())),
        )


class KeyPool:
    """
    Manages team member Torn API keys with per-user activation toggles,
    strict per-key rate-limit ceilings, and load-balanced round-robin selection.
    """

    def __init__(self, storage_path: Optional[Path] = None):
        self.storage_path = storage_path
        self._entries: dict[int, KeyEntry] = {}
        self._rr_cursor: int = 0
        self._lock = asyncio.Lock()
        if self.storage_path and self.storage_path.exists():
            self._load()

    def set_key(
        self,
        discord_id: int,
        torn_name: str,
        torn_id: int,
        api_key: str,
        max_rpm: int = SAFE_DEFAULT_MAX_RPM,
    ) -> KeyEntry:
        entry = KeyEntry(
            discord_id=discord_id,
            torn_name=torn_name,
            torn_id=torn_id,
            api_key=api_key,
            active=True,
            max_rpm=max_rpm,
        )
        self._entries[discord_id] = entry
        self._save()
        return entry

    def pause_key(self, discord_id: int, reason: Optional[str] = "Paused by user") -> Optional[KeyEntry]:
        entry = self._entries.get(discord_id)
        if not entry:
            return None
        entry.active = False
        entry.paused_reason = reason
        self._save()
        return entry

    def resume_key(self, discord_id: int) -> Optional[KeyEntry]:
        entry = self._entries.get(discord_id)
        if not entry:
            return None
        entry.active = True
        entry.paused_reason = None
        self._save()
        return entry

    def remove_key(self, discord_id: int) -> bool:
        if discord_id not in self._entries:
            return False
        del self._entries[discord_id]
        self._save()
        return True

    def get_entry(self, discord_id: int) -> Optional[KeyEntry]:
        return self._entries.get(discord_id)

    def list_entries(self) -> list[KeyEntry]:
        return list(self._entries.values())

    def active_entries(self) -> list[KeyEntry]:
        return [e for e in self._entries.values() if e.active]

    def active_count(self) -> int:
        return sum(1 for e in self._entries.values() if e.active)

    def total_rpm_capacity(self) -> int:
        return sum(e.limiter.max_calls for e in self._entries.values() if e.active)

    async def acquire_next_key(self) -> Optional[KeyEntry]:
        """
        Select an active key, preferring the key with the most remaining calls
        in its current 60s window (breaking ties via round-robin), then wait for
        its rate-limiter slot.
        """
        async with self._lock:
            active = self.active_entries()
            if not active:
                return None

            n = len(active)
            ordered = [active[(self._rr_cursor + i) % n] for i in range(n)]
            chosen = max(ordered, key=lambda e: e.limiter.remaining_calls())
            self._rr_cursor = (active.index(chosen) + 1) % n

        await chosen.limiter.acquire()
        if not chosen.active:
            return None
        chosen.calls_made += 1
        return chosen

    def _load(self) -> None:
        if not self.storage_path or not self.storage_path.exists():
            return
        try:
            raw = json.loads(self.storage_path.read_text(encoding="utf-8"))
            for item in raw.get("keys", []):
                entry = KeyEntry.from_dict(item)
                self._entries[entry.discord_id] = entry
        except Exception:
            pass

    def _save(self) -> None:
        if not self.storage_path:
            return
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"keys": [e.to_dict() for e in self._entries.values()]}
        self.storage_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        try:
            os.chmod(self.storage_path, 0o600)
        except OSError:
            pass
