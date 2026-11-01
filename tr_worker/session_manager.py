"""Trade Republic pytr session handler with interactive Discord bot fallback for 2FA renewal."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from shared.mock_data import load_mock_tr_portfolio, load_mock_tr_timeline

logger = logging.getLogger(__name__)

DISCORD_API_BASE = "https://discord.com/api/v10"
PIN_REGEX = re.compile(r"\b(\d{4})\b")


class TRSessionError(RuntimeError):
    """Raised when Trade Republic authentication or 2FA renewal fails."""


class Discord2FAClient:
    """Interacts with Discord REST API v10 to prompt for and retrieve a 4-digit 2FA PIN."""

    def __init__(
        self,
        bot_token: str,
        channel_id: str,
        *,
        timeout_seconds: float = 300.0,
        poll_interval_seconds: float = 3.0,
        http_client: httpx.Client | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        monotonic_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self.channel_id = channel_id
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self._sleep = sleep_fn
        self._monotonic = monotonic_fn
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(
            timeout=15.0,
            headers={
                "Authorization": f"Bot {bot_token}",
                "Content-Type": "application/json",
            },
        )
        if http_client is not None:
            self._client.headers.setdefault("Authorization", f"Bot {bot_token}")

    def close(self) -> None:
        """Close the owned HTTP client."""
        if self._owns_client:
            self._client.close()

    def send_message(self, content: str) -> str:
        """Post a message to the configured Discord channel and return its message ID."""
        url = f"{DISCORD_API_BASE}/channels/{self.channel_id}/messages"
        resp = self._client.post(url, json={"content": content})
        if resp.status_code >= 400:
            raise TRSessionError(f"Discord send_message failed ({resp.status_code}): {resp.text}")
        data: dict[str, Any] = resp.json()
        msg_id = str(data.get("id") or "")
        if not msg_id:
            raise TRSessionError("Discord response missing message id")
        return msg_id

    def wait_for_pin(self, after_message_id: str) -> str:
        """Poll Discord channel messages after `after_message_id` for a 4-digit PIN reply."""
        url = f"{DISCORD_API_BASE}/channels/{self.channel_id}/messages"
        deadline = self._monotonic() + self.timeout_seconds

        while self._monotonic() < deadline:
            self._sleep(self.poll_interval_seconds)
            resp = self._client.get(
                url,
                params={"after": after_message_id, "limit": 10},
            )
            if resp.status_code >= 400:
                raise TRSessionError(
                    f"Discord get_messages failed ({resp.status_code}): {resp.text}"
                )
            messages: list[dict[str, Any]] = resp.json()
            for msg in reversed(messages):
                author: dict[str, Any] = msg.get("author") or {}
                if author.get("bot"):
                    continue
                content = str(msg.get("content") or "").strip()
                match = PIN_REGEX.search(content)
                if match:
                    return match.group(1)

        raise TRSessionError("Timed out waiting for 4-digit Trade Republic 2FA PIN in Discord")

    def prompt_and_wait_for_pin(self) -> str:
        """Send 2FA prompt to Discord and return the user's 4-digit verification code."""
        prompt_id = self.send_message(
            "🔐 **Trade Republic 2FA Required**\n"
            "Your Trade Republic session has expired. Please reply in this channel "
            "with the **4-digit verification code** sent to your Trade Republic app/SMS."
        )
        pin = self.wait_for_pin(prompt_id)
        self.send_message("✅ Received 4-digit code. Completing Trade Republic authentication...")
        return pin


class TRSessionManager:
    """Manages persistent pytr web session in ./data with Discord 2FA fallback."""

    def __init__(
        self,
        phone_no: str,
        pin: str,
        session_dir: Path,
        discord_client: Discord2FAClient,
        *,
        use_mock_data: bool = False,
        tr_api_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.phone_no = phone_no
        self.pin = pin
        self.session_dir = Path(session_dir)
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.cookies_file = self.session_dir / "tr_cookies.txt"
        self.session_meta_file = self.session_dir / "tr_session.json"
        self.discord_client = discord_client
        self.use_mock_data = use_mock_data
        self._tr_api_factory = tr_api_factory
        self._tr_api: Any = None

    def _create_tr_api(self) -> Any:
        if self._tr_api_factory is not None:
            return self._tr_api_factory(
                phone_no=self.phone_no,
                pin=self.pin,
                cookies_file=str(self.cookies_file),
            )
        from pytr.api import TradeRepublicApi

        return TradeRepublicApi(
            phone_no=self.phone_no,
            pin=self.pin,
            save_cookies=True,
            cookies_file=str(self.cookies_file),
        )

    def has_valid_session(self) -> bool:
        """Return True if a persisted session file exists and is marked authenticated."""
        if not self.cookies_file.exists() or not self.session_meta_file.exists():
            return False
        try:
            meta: dict[str, Any] = json.loads(self.session_meta_file.read_text(encoding="utf-8"))
            return bool(meta.get("authenticated", False))
        except Exception:
            return False

    def mark_session_invalid(self) -> None:
        """Mark the current persisted session as invalid so next poll triggers 2FA."""
        self.session_meta_file.write_text(
            json.dumps({"authenticated": False, "updated_at": time.time()}),
            encoding="utf-8",
        )

    def ensure_authenticated(self) -> Any:
        """Ensure an authenticated pytr client is ready, renewing via Discord 2FA if needed."""
        if self._tr_api is None:
            self._tr_api = self._create_tr_api()

        if self.has_valid_session():
            resume = getattr(self._tr_api, "resume_websession", None)
            if callable(resume) and resume():
                return self._tr_api

        logger.info("Initiating Trade Republic weblogin and requesting 2FA via Discord")
        initiate = getattr(self._tr_api, "initiate_weblogin", None)
        if callable(initiate):
            initiate()

        code = self.discord_client.prompt_and_wait_for_pin()
        complete = getattr(self._tr_api, "complete_weblogin", None)
        if callable(complete):
            complete(code)

        if not self.cookies_file.exists():
            self.cookies_file.write_text("# pytr cookies\n", encoding="utf-8")
        self.session_meta_file.write_text(
            json.dumps({"authenticated": True, "updated_at": time.time()}),
            encoding="utf-8",
        )
        return self._tr_api

    async def _fetch_ws_data_async(self, api: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        timeline_events: list[dict[str, Any]] = []
        portfolio_data: dict[str, Any] = {"netValue": 0.0, "positions": []}

        await api.timeline_transactions()
        _, _, response = await api.recv()
        items: list[dict[str, Any]] = (
            response.get("items", []) if isinstance(response, dict) else []
        )
        timeline_events.extend(items)

        await api.compact_portfolio()
        _, _, port_resp = await api.recv()
        if isinstance(port_resp, dict):
            positions: list[dict[str, Any]] = port_resp.get("positions", [])
            net_val = float(port_resp.get("netValue") or 0.0)
            if net_val == 0.0 and positions:
                for pos in positions:
                    net_val += float(pos.get("marketValue") or 0.0)
            portfolio_data = {"netValue": net_val, "positions": positions}

        return timeline_events, portfolio_data

    def fetch_timeline_and_portfolio(
        self,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Fetch Trade Republic timeline events and portfolio valuation."""
        if self.use_mock_data:
            logger.info("Loading synthetic Trade Republic payloads (USE_MOCK_DATA=true)")
            return load_mock_tr_timeline(), load_mock_tr_portfolio()

        api = self.ensure_authenticated()
        fetch_sync = getattr(api, "fetch_timeline_and_portfolio", None)
        if callable(fetch_sync):
            result: tuple[list[dict[str, Any]], dict[str, Any]] = fetch_sync()
            return result

        try:
            return asyncio.run(self._fetch_ws_data_async(api))
        except Exception as exc:
            logger.warning("TR websocket fetch failed (%s); marking session invalid", exc)
            self.mark_session_invalid()
            raise TRSessionError(f"Failed to fetch Trade Republic data: {exc}") from exc
