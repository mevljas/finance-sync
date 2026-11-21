"""Trade Republic pytr session handler with interactive Discord bot fallback for 2FA renewal."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import websockets.asyncio.client
import websockets.exceptions
from shared.discord import DISCORD_GATEWAY_INTENTS, DiscordAPIError, DiscordClient
from shared.mock_data import load_mock_tr_portfolio, load_mock_tr_timeline

logger = logging.getLogger(__name__)

PIN_REGEX = re.compile(r"\b(\d{4,6})\b")
FATAL_GATEWAY_CLOSE_CODES = frozenset({4004, 4010, 4011, 4012, 4013, 4014})
DEFAULT_2FA_PROMPT = (
    "🔐 **Trade Republic 2FA Required**\n"
    "Your Trade Republic session has expired.\n"
    "Whenever you are ready, reply in this channel with your "
    "**6-digit code from your Authenticator app** (no timeout) to start login."
)


class TRSessionError(RuntimeError):
    """Raised when Trade Republic authentication or 2FA renewal fails."""


class _GatewayReconnectSignal(Exception):
    """Internal control-flow signal requesting a Gateway reconnect."""

    def __init__(self, *, resumable: bool) -> None:
        super().__init__("Discord Gateway reconnect requested")
        self.resumable = resumable


def _close_code_and_reason(exc: websockets.exceptions.ConnectionClosed) -> tuple[int | None, str]:
    if exc.rcvd is not None:
        return int(exc.rcvd.code), str(exc.rcvd.reason or "")
    return None, ""


def _is_message_after(message_id: str, after_message_id: str) -> bool:
    if message_id.isdigit() and after_message_id.isdigit():
        return int(message_id) > int(after_message_id)
    return bool(message_id) and message_id != after_message_id


def _extract_pin_from_message(
    message: dict[str, Any],
    *,
    channel_id: str,
    after_message_id: str,
) -> str | None:
    if str(message.get("channel_id") or "") != channel_id:
        return None
    msg_id = str(message.get("id") or "")
    if not _is_message_after(msg_id, after_message_id):
        return None
    author: dict[str, Any] = message.get("author") or {}
    if author.get("bot"):
        return None
    content = str(message.get("content") or "").strip()
    match = PIN_REGEX.search(content)
    return match.group(1) if match else None


async def _heartbeat_loop(
    ws: Any,
    interval_seconds: float,
    seq_ref: list[int | None],
    ack_state: list[bool],
) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        if not ack_state[0]:
            logger.warning("Discord Gateway missed heartbeat ACK; closing connection to resume")
            await ws.close(code=4000, reason="Heartbeat ACK missed")
            return
        ack_state[0] = False
        await ws.send(json.dumps({"op": 1, "d": seq_ref[0]}))


class Discord2FAClient:
    """Interacts with Discord Gateway v10 & REST API v10 to prompt for and receive a 2FA code."""

    def __init__(
        self,
        bot_token: str,
        channel_id: str,
        *,
        http_client: httpx.Client | None = None,
        ws_connect: Callable[[str], Any] | None = None,
        reconnect_sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._bot_token = bot_token
        self.channel_id = channel_id
        self._ws_connect = ws_connect or websockets.asyncio.client.connect
        self._reconnect_sleep = reconnect_sleep_fn
        self._discord = DiscordClient(
            bot_token=bot_token,
            channel_id=channel_id,
            http_client=http_client,
        )

    def close(self) -> None:
        """Close the owned HTTP client."""
        self._discord.close()

    def send_message(self, content: str) -> str:
        """Post a message to the configured Discord channel and return its message ID."""
        try:
            return self._discord.send_message(content)
        except DiscordAPIError as exc:
            raise TRSessionError(str(exc)) from exc

    async def _send_identify_or_resume(
        self,
        ws: Any,
        *,
        session_id: str | None,
        seq: int | None,
    ) -> None:
        if session_id is not None and seq is not None:
            payload: dict[str, Any] = {
                "op": 6,
                "d": {
                    "token": self._bot_token,
                    "session_id": session_id,
                    "seq": seq,
                },
            }
        else:
            payload = {
                "op": 2,
                "d": {
                    "token": self._bot_token,
                    "intents": DISCORD_GATEWAY_INTENTS,
                    "properties": {
                        "os": "linux",
                        "browser": "finance-sync",
                        "device": "finance-sync",
                    },
                },
            }
        await ws.send(json.dumps(payload))

    async def prompt_and_wait_for_pin_async(self, prompt_message: str | None = None) -> str:
        """Connect to Discord Gateway, post prompt after READY, and wait indefinitely for a code."""
        try:
            initial_gateway_url = await asyncio.to_thread(self._discord.get_gateway_url)
        except DiscordAPIError as exc:
            raise TRSessionError(str(exc)) from exc

        active_prompt = prompt_message or DEFAULT_2FA_PROMPT
        prompt_id: str | None = None
        session_id: str | None = None
        resume_gateway_url: str | None = None
        seq_ref: list[int | None] = [None]
        backoff_attempt = 0

        while True:
            base_url = (
                resume_gateway_url if session_id and resume_gateway_url else initial_gateway_url
            )
            ws_url = f"{base_url.rstrip('/')}/?v=10&encoding=json"
            heartbeat_task: asyncio.Task[None] | None = None
            ack_state = [True]

            try:
                async with self._ws_connect(ws_url) as ws:
                    backoff_attempt = 0
                    while True:
                        raw_frame = await ws.recv()
                        payload: dict[str, Any] = json.loads(raw_frame)
                        if payload.get("s") is not None:
                            seq_ref[0] = int(payload["s"])

                        op = payload.get("op")
                        event_name = payload.get("t")
                        data = payload.get("d")

                        if op == 10 and isinstance(data, dict):
                            interval_s = max(
                                1.0, float(data.get("heartbeat_interval", 41250)) / 1000.0
                            )
                            if heartbeat_task is not None:
                                heartbeat_task.cancel()
                            heartbeat_task = asyncio.create_task(
                                _heartbeat_loop(ws, interval_s, seq_ref, ack_state)
                            )
                            await self._send_identify_or_resume(
                                ws,
                                session_id=session_id,
                                seq=seq_ref[0],
                            )
                        elif op == 1:
                            await ws.send(json.dumps({"op": 1, "d": seq_ref[0]}))
                        elif op == 11:
                            ack_state[0] = True
                        elif op == 7:
                            raise _GatewayReconnectSignal(resumable=True)
                        elif op == 9:
                            resumable = bool(data)
                            if not resumable:
                                session_id = None
                                resume_gateway_url = None
                                seq_ref[0] = None
                            raise _GatewayReconnectSignal(resumable=resumable)
                        elif op == 0 and event_name == "READY" and isinstance(data, dict):
                            session_id = str(data.get("session_id") or "") or None
                            raw_resume = (
                                str(data.get("resume_gateway_url") or "").strip().rstrip("/")
                            )
                            resume_gateway_url = raw_resume or None
                            if prompt_id is None:
                                prompt_id = await asyncio.to_thread(
                                    self.send_message, active_prompt
                                )
                        elif op == 0 and event_name == "RESUMED":
                            if prompt_id is None:
                                prompt_id = await asyncio.to_thread(
                                    self.send_message, active_prompt
                                )
                        elif (
                            op == 0
                            and event_name == "MESSAGE_CREATE"
                            and isinstance(data, dict)
                            and prompt_id is not None
                        ):
                            pin = _extract_pin_from_message(
                                data,
                                channel_id=self.channel_id,
                                after_message_id=prompt_id,
                            )
                            if pin is not None:
                                return pin
            except _GatewayReconnectSignal as sig:
                logger.info(
                    "Discord Gateway requested reconnect (resumable=%s); reconnecting",
                    sig.resumable,
                )
            except websockets.exceptions.ConnectionClosed as exc:
                code, reason = _close_code_and_reason(exc)
                if code in FATAL_GATEWAY_CLOSE_CODES:
                    detail = reason or "check token/intents"
                    raise TRSessionError(
                        f"Discord Gateway closed fatally ({code}): {detail}"
                    ) from exc
                logger.warning(
                    "Discord Gateway connection closed (code=%s, reason=%s); reconnecting",
                    code,
                    reason,
                )
            except OSError as exc:
                logger.warning("Discord Gateway socket error (%s); reconnecting", exc)
            finally:
                if heartbeat_task is not None:
                    heartbeat_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await heartbeat_task

            backoff_attempt += 1
            delay = min(60.0, float(2 ** min(backoff_attempt, 5)))
            await self._reconnect_sleep(delay)

    def prompt_and_wait_for_pin(self, prompt_message: str | None = None) -> str:
        """Prompt in Discord and wait indefinitely via Gateway events for a 4-6 digit code."""
        return asyncio.run(self.prompt_and_wait_for_pin_async(prompt_message))


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
        card_cutoff_date: datetime | None = None,
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
        self.card_cutoff_date = card_cutoff_date
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
            use_v2_login=True,
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

    def _perform_weblogin(self, code: str) -> None:
        """Initiate Trade Republic web login and verify using the provided 2FA code."""
        if self._tr_api is None:
            self._tr_api = self._create_tr_api()

        logger.info("Initiating Trade Republic weblogin after receiving 2FA code")
        initiate = getattr(self._tr_api, "initiate_weblogin", None)
        if callable(initiate):
            initiate()

        needs_auth_code = bool(getattr(self._tr_api, "weblogin_needs_authenticator", True))
        required_action = getattr(self._tr_api, "_required_action", None)
        logger.info(
            "Trade Republic login process initiated (requiredAction=%s, needs_authenticator=%s)",
            required_action,
            needs_auth_code,
        )
        complete = getattr(self._tr_api, "complete_weblogin", None)
        if needs_auth_code:
            if callable(complete):
                complete(code)
            get_proc = getattr(self._tr_api, "_get_weblogin_process", None)
            await_confirm = getattr(self._tr_api, "_await_weblogin_confirmation", None)
            save_sess = getattr(self._tr_api, "save_websession", None)
            if callable(get_proc) and callable(await_confirm):
                proc = get_proc(quiet=True)
                logger.info(
                    "Trade Republic post-authenticator process state: status=%s, requiredAction=%s",
                    proc.get("status"),
                    proc.get("requiredAction"),
                )
                if proc.get("status") == "PENDING":
                    self.discord_client.send_message(
                        "📲 **Step 2: Confirm in Trade Republic App**\n"
                        "Authenticator code accepted! Please open your "
                        "**Trade Republic mobile app** and tap **Confirm** to finish logging in."
                    )
                    await_confirm()
                    if callable(save_sess):
                        save_sess()
                    self.discord_client.send_message(
                        "✅ Trade Republic login confirmed in mobile app."
                    )
        else:
            self.discord_client.send_message(
                "📲 **Trade Republic Login Confirmation Required**\n"
                "Please open your **Trade Republic mobile app** and tap **Confirm** "
                "to approve the web login."
            )
            if callable(complete):
                complete()
            self.discord_client.send_message("✅ Trade Republic login confirmed in mobile app.")

    def ensure_authenticated(self) -> Any:
        """Ensure an authenticated pytr client is ready, renewing via Discord 2FA if needed."""
        if self._tr_api is None:
            self._tr_api = self._create_tr_api()

        if self.has_valid_session():
            resume = getattr(self._tr_api, "resume_websession", None)
            if callable(resume) and resume():
                return self._tr_api

        prompt_message: str | None = None
        while True:
            code = self.discord_client.prompt_and_wait_for_pin(prompt_message)
            self.discord_client.send_message(
                "🔄 Received verification code. Starting Trade Republic login..."
            )
            try:
                self._perform_weblogin(code)
                break
            except Exception as exc:
                raw_error = str(exc) or type(exc).__name__
                safe_error = (
                    raw_error.replace(self.pin, "***")
                    if self.pin and len(self.pin) >= 4
                    else raw_error
                )
                logger.warning(
                    "Trade Republic login attempt failed (%s); waiting for new 2FA code",
                    safe_error,
                )
                self._tr_api = None
                prompt_message = (
                    f"❌ **Trade Republic login failed:** {safe_error}\n"
                    "Whenever you are ready, please reply with a "
                    "**new 6-digit Authenticator code**."
                )

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
        try:
            after_cursor: str | None = None
            for _ in range(25):
                if after_cursor is None:
                    await api.timeline_transactions()
                else:
                    await api.timeline_transactions(after_cursor)
                _, _, response = await api.recv()
                if not isinstance(response, dict):
                    break
                items: list[dict[str, Any]] = response.get("items", [])
                if not items:
                    break
                timeline_events.extend(items)
                cursors: dict[str, Any] = response.get("cursors") or {}
                after_cursor = cursors.get("after")
                if not after_cursor:
                    break

            detail_v2_fn = getattr(api, "timeline_detail_v2", None)
            if callable(detail_v2_fn):
                for event in timeline_events:
                    status_u = str(event.get("status") or "EXECUTED").upper()
                    if status_u in ("CANCELED", "CANCELLED", "FAILED", "REJECTED", "PENDING"):
                        continue
                    subtitle_l = str(event.get("subtitle") or "").lower()
                    title_l = str(event.get("title") or "").lower()
                    ev_type_l = str(event.get("eventType") or "").lower()
                    combined = f"{ev_type_l} {title_l} {subtitle_l}"
                    is_card_ev = "card" in combined
                    if is_card_ev and self.card_cutoff_date is not None:
                        raw_ts = str(event.get("timestamp") or "").strip()
                        if raw_ts:
                            cleaned_ts = f"{raw_ts[:-1]}+00:00" if raw_ts.endswith("Z") else raw_ts
                            with contextlib.suppress(ValueError):
                                ev_dt = datetime.fromisoformat(cleaned_ts)
                                if ev_dt.tzinfo is None:
                                    ev_dt = ev_dt.replace(tzinfo=UTC)
                                if ev_dt < self.card_cutoff_date:
                                    continue
                    needs_detail = any(
                        kw in combined
                        for kw in (
                            "saveback",
                            "savings",
                            "sparplan",
                            "order",
                            "buy",
                            "dividend",
                            "card",
                        )
                    )
                    action: dict[str, Any] = event.get("action") or {}
                    ev_id = str(event.get("id") or "")
                    if (
                        needs_detail
                        and ev_id
                        and not event.get("details")
                        and action.get("type") in ("timelineDetail", "timelineDetailV2")
                    ):
                        await detail_v2_fn(ev_id)
                        _, _, detail_resp = await api.recv()
                        if isinstance(detail_resp, dict):
                            event["details"] = detail_resp

            await api.compact_portfolio()
            _, _, port_resp = await api.recv()
            if isinstance(port_resp, dict):
                positions: list[dict[str, Any]] = list(port_resp.get("positions", []))
                if not positions and "categories" in port_resp:
                    for cat in port_resp.get("categories", []):
                        if isinstance(cat, dict):
                            for pos in cat.get("positions", []):
                                if isinstance(pos, dict):
                                    if "isin" in pos and "instrumentId" not in pos:
                                        pos["instrumentId"] = pos["isin"]
                                    positions.append(pos)

                net_val = float(port_resp.get("netValue") or 0.0)
                if net_val == 0.0 and positions:
                    has_market_val = any(pos.get("marketValue") is not None for pos in positions)
                    if not has_market_val and hasattr(api, "instrument_details"):
                        from pytr.tickers import fetch_instrument_details, fetch_tickers

                        await fetch_instrument_details(api, positions)
                        await fetch_tickers(api, positions)
                        for pos in positions:
                            price = float(pos.get("price") or 0.0)
                            size = float(pos.get("netSize") or 0.0)
                            pos["marketValue"] = round(price * size, 2)
                            logger.info(
                                "TR position %s (%s): netSize=%s price=%s marketValue=%.2f",
                                pos.get("instrumentId"),
                                pos.get("name"),
                                pos.get("netSize"),
                                pos.get("price"),
                                pos["marketValue"],
                            )

                    for pos in positions:
                        net_val += float(pos.get("marketValue") or 0.0)
                portfolio_data = {"netValue": round(net_val, 2), "positions": positions}

            cash_fn = getattr(api, "cash", None)
            if callable(cash_fn):
                with contextlib.suppress(Exception):
                    await cash_fn()
                    _, _, cash_resp = await api.recv()
                    if isinstance(cash_resp, list) and cash_resp:
                        total_cash = sum(
                            float(item.get("amount") or 0.0)
                            for item in cash_resp
                            if isinstance(item, dict)
                        )
                        portfolio_data["cashBalance"] = round(total_cash, 2)
                    elif isinstance(cash_resp, dict) and "amount" in cash_resp:
                        portfolio_data["cashBalance"] = round(float(cash_resp["amount"]), 2)

            return timeline_events, portfolio_data
        finally:
            close_fn = getattr(api, "close", None)
            if callable(close_fn):
                res = close_fn()
                if asyncio.iscoroutine(res):
                    await res

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
