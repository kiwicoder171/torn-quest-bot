import asyncio
import logging
from typing import Any, Optional
import aiohttp

from key_pool import KeyEntry

logger = logging.getLogger("TornQuestBot.TornAPI")


class TornApiException(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(f"Torn API Error [{code}]: {message}")
        self.code = code
        self.message = message


class TornApiClient:
    """
    Async Torn API v2 client supporting pooled per-user keys and automatic
    rate-limit backoff + invalid/paused key auto-deactivation.
    """

    BASE_URL = "https://api.torn.com/v2"
    INVALID_KEY_CODES = {1, 2, 10, 18}  # Empty, Incorrect, Key owner fedded, API key paused
    RATE_LIMIT_CODE = 5

    def __init__(
        self,
        session: Optional[aiohttp.ClientSession] = None,
        timeout_seconds: float = 10.0,
    ):
        self._external_session = session is not None
        self._session = session
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    async def get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self._session

    async def close(self) -> None:
        if not self._external_session and self._session and not self._session.closed:
            await self._session.close()

    async def verify_key(self, api_key: str) -> tuple[bool, str, Optional[dict[str, Any]]]:
        """Verify an API key directly (single call) and return (ok, message, profile)."""
        url = f"{self.BASE_URL}/user/?selections=basic&key={api_key}"
        session = await self.get_session()
        try:
            async with session.get(url) as resp:
                data = await resp.json()
                if "error" in data:
                    err = data["error"]
                    return False, f"Torn API Error [{err.get('code')}]: {err.get('error')}", None
                profile = data.get("profile", {})
                name = profile.get("name", "Unknown")
                player_id = profile.get("id", 0)
                return True, f"Verified for {name} [{player_id}]", {"name": name, "id": player_id}
        except Exception as exc:
            return False, f"Network error: {exc}", None

    async def fetch_item_market(self, item_id: int, key_entry: KeyEntry) -> dict[str, Any]:
        """
        Fetch active Item Market listings for `item_id` using the provided KeyEntry.
        Automatically throttles on HTTP 429 / Error 5 and pauses key on invalid/paused key errors.
        """
        url = f"{self.BASE_URL}/market/{item_id}/itemmarket?key={key_entry.api_key}"
        session = await self.get_session()

        for attempt in range(2):
            async with session.get(url) as resp:
                if resp.status == 429:
                    self._throttle_key(key_entry, "HTTP 429")
                    await asyncio.sleep(15.0 * (attempt + 1))
                    continue

                if resp.status >= 500:
                    await asyncio.sleep(2.0 * (attempt + 1))
                    continue

                data = await resp.json()
                if "error" in data:
                    err = data["error"]
                    code = int(err.get("code", 0))
                    msg = str(err.get("error", "Unknown error"))
                    if code == self.RATE_LIMIT_CODE:
                        self._throttle_key(key_entry, f"Torn Error {code}")
                        await asyncio.sleep(15.0 * (attempt + 1))
                        continue
                    if code in self.INVALID_KEY_CODES:
                        key_entry.active = False
                        key_entry.paused_reason = f"Auto-paused: Torn Error [{code}] {msg}"
                        logger.warning(f"Auto-paused key for {key_entry.torn_name}: {msg}")
                    raise TornApiException(code, msg)

                return data

        raise TornApiException(429, "Rate limited after retries")

    @staticmethod
    def _throttle_key(key_entry: KeyEntry, source: str) -> None:
        current = key_entry.limiter.max_calls
        new_rate = max(10, current - 10)
        key_entry.limiter.set_rate(new_rate)
        logger.warning(
            f"{source} on {key_entry.torn_name}'s key — throttled ceiling from {current} to {new_rate} req/min"
        )
