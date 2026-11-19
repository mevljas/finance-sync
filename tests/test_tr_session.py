"""Unit tests for Trade Republic ready-first Discord Gateway 2FA renewal."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
import websockets.exceptions
from config.settings import Settings
from shared.discord import DISCORD_API_BASE, DISCORD_GATEWAY_INTENTS
from tr_worker.session_manager import Discord2FAClient, TRSessionError, TRSessionManager
from websockets.frames import Close


class FakeGatewayConnection:
    """In-memory fake Discord Gateway WebSocket emitting scripted server frames."""

    def __init__(self, script: list[dict[str, Any] | Exception]) -> None:
        self._incoming: asyncio.Queue[dict[str, Any] | Exception] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        for item in script:
            self._incoming.put_nowait(item)
        self.sent_payloads: list[dict[str, Any]] = []
        self.closed = False

    def push_event(self, payload: dict[str, Any] | Exception) -> None:
        """Enqueue a server frame or exception safely from any thread."""
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._incoming.put_nowait, payload)
        else:
            self._incoming.put_nowait(payload)

    async def recv(self) -> str:
        """Return next serialized JSON frame or raise scripted exception."""
        self._loop = asyncio.get_running_loop()
        item = await self._incoming.get()
        if isinstance(item, Exception):
            raise item
        return json.dumps(item)

    async def send(self, data: str) -> None:
        """Record an outgoing client JSON payload."""
        self.sent_payloads.append(json.loads(data))

    async def close(self) -> None:
        """Mark the fake WebSocket closed."""
        self.closed = True


class FakeGatewayScriptedTransport:
    """Factory managing multiple sequential Gateway connection attempts in tests."""

    def __init__(
        self,
        connections: list[FakeGatewayConnection],
        *,
        on_connect: Any | None = None,
    ) -> None:
        self.connections = connections
        self.connect_urls: list[str] = []
        self._on_connect = on_connect

    @asynccontextmanager
    async def connect(self, url: str) -> AsyncIterator[FakeGatewayConnection]:
        """Yield the next scripted FakeGatewayConnection for `url`."""
        idx = len(self.connect_urls)
        self.connect_urls.append(url)
        conn = self.connections[idx]
        if self._on_connect is not None:
            self._on_connect(idx, url, conn)
        try:
            yield conn
        finally:
            await conn.close()


@respx.mock
def test_discord_2fa_waits_for_ready_before_posting_prompt_and_filters_messages() -> None:
    """Verify Gateway connects and becomes READY before posting prompt, and filters replies."""
    channel_id = "000000000000000001"
    events: list[str] = []

    respx.get(f"{DISCORD_API_BASE}/gateway/bot").mock(
        return_value=httpx.Response(200, json={"url": "wss://gateway.discord.gg"})
    )

    conn = FakeGatewayConnection(
        [
            {"op": 10, "d": {"heartbeat_interval": 41250}},
            {
                "op": 0,
                "t": "READY",
                "s": 1,
                "d": {"session_id": "sess-0001", "resume_gateway_url": "wss://resume.discord.gg"},
            },
        ]
    )

    def post_message(request: httpx.Request) -> httpx.Response:
        events.append("post_prompt")
        assert any(p.get("op") == 2 for p in conn.sent_payloads), "Must Identify before prompt!"
        # Deliver filtered noise before the valid user reply
        conn.push_event(
            {
                "op": 0,
                "t": "MESSAGE_CREATE",
                "s": 2,
                "d": {
                    "id": "000000000000000099",
                    "channel_id": channel_id,
                    "content": "111111",
                    "author": {"bot": False},
                },
            }
        )
        conn.push_event(
            {
                "op": 0,
                "t": "MESSAGE_CREATE",
                "s": 3,
                "d": {
                    "id": "000000000000000101",
                    "channel_id": channel_id,
                    "content": "999999",
                    "author": {"bot": True},
                },
            }
        )
        conn.push_event(
            {
                "op": 0,
                "t": "MESSAGE_CREATE",
                "s": 4,
                "d": {
                    "id": "000000000000000102",
                    "channel_id": "000000000000000002",
                    "content": "888888",
                    "author": {"bot": False},
                },
            }
        )
        conn.push_event(
            {
                "op": 0,
                "t": "MESSAGE_CREATE",
                "s": 5,
                "d": {
                    "id": "000000000000000103",
                    "channel_id": channel_id,
                    "content": "ready, no digits yet",
                    "author": {"bot": False},
                },
            }
        )
        conn.push_event(
            {
                "op": 0,
                "t": "MESSAGE_CREATE",
                "s": 6,
                "d": {
                    "id": "000000000000000104",
                    "channel_id": channel_id,
                    "content": "654321",
                    "author": {"bot": False},
                },
            }
        )
        return httpx.Response(200, json={"id": "000000000000000100"})

    respx.post(f"{DISCORD_API_BASE}/channels/{channel_id}/messages").mock(side_effect=post_message)

    transport = FakeGatewayScriptedTransport([conn])
    client = Discord2FAClient(
        bot_token="test-bot-token",
        channel_id=channel_id,
        ws_connect=transport.connect,
    )
    try:
        pin = client.prompt_and_wait_for_pin()
    finally:
        client.close()

    assert pin == "654321"
    assert events == ["post_prompt"]
    assert transport.connect_urls == ["wss://gateway.discord.gg/?v=10&encoding=json"]
    identify_payload = next(p for p in conn.sent_payloads if p.get("op") == 2)
    assert identify_payload["d"]["intents"] == DISCORD_GATEWAY_INTENTS


@respx.mock
def test_discord_2fa_resumes_on_reconnect_without_reposting_prompt() -> None:
    """Verify OP 7 Reconnect / transient disconnect resumes via OP 6 without duplicate prompt."""
    channel_id = "000000000000000001"
    respx.get(f"{DISCORD_API_BASE}/gateway/bot").mock(
        return_value=httpx.Response(200, json={"url": "wss://gateway.discord.gg"})
    )
    post_route = respx.post(f"{DISCORD_API_BASE}/channels/{channel_id}/messages").mock(
        return_value=httpx.Response(200, json={"id": "000000000000000100"})
    )

    conn1 = FakeGatewayConnection(
        [
            {"op": 10, "d": {"heartbeat_interval": 41250}},
            {
                "op": 0,
                "t": "READY",
                "s": 7,
                "d": {
                    "session_id": "sess-0001",
                    "resume_gateway_url": "wss://resume.discord.gg",
                },
            },
            {"op": 1, "d": None},  # server asks for immediate heartbeat
            {"op": 7, "d": None},  # server asks client to reconnect and resume
        ]
    )
    conn2 = FakeGatewayConnection(
        [
            {"op": 10, "d": {"heartbeat_interval": 41250}},
            {
                "op": 0,
                "t": "MESSAGE_CREATE",
                "s": 8,
                "d": {
                    "id": "000000000000000105",
                    "channel_id": channel_id,
                    "content": "246810",
                    "author": {"bot": False},
                },
            },
        ]
    )

    sleep_calls: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    transport = FakeGatewayScriptedTransport([conn1, conn2])
    client = Discord2FAClient(
        bot_token="test-bot-token",
        channel_id=channel_id,
        ws_connect=transport.connect,
        reconnect_sleep_fn=fake_sleep,
    )
    try:
        pin = client.prompt_and_wait_for_pin()
    finally:
        client.close()

    assert pin == "246810"
    # Prompt posted only once despite reconnect
    assert post_route.call_count == 1
    assert any(p.get("op") == 1 for p in conn1.sent_payloads)
    assert transport.connect_urls == [
        "wss://gateway.discord.gg/?v=10&encoding=json",
        "wss://resume.discord.gg/?v=10&encoding=json",
    ]
    resume_payload = next(p for p in conn2.sent_payloads if p.get("op") == 6)
    assert resume_payload["d"] == {
        "token": "test-bot-token",
        "session_id": "sess-0001",
        "seq": 7,
    }
    assert sleep_calls == [2.0]


@respx.mock
@pytest.mark.parametrize("close_code", [4004, 4014])
def test_discord_2fa_raises_immediately_on_fatal_gateway_close_code(close_code: int) -> None:
    """Verify fatal close codes (4004 auth, 4014 disallowed intents) fail immediately."""
    channel_id = "000000000000000001"
    respx.get(f"{DISCORD_API_BASE}/gateway/bot").mock(
        return_value=httpx.Response(200, json={"url": "wss://gateway.discord.gg"})
    )

    conn = FakeGatewayConnection(
        [
            {"op": 10, "d": {"heartbeat_interval": 41250}},
            websockets.exceptions.ConnectionClosed(
                rcvd=Close(close_code, "Disallowed intent(s)"),
                sent=None,
            ),
        ]
    )
    transport = FakeGatewayScriptedTransport([conn])
    client = Discord2FAClient(
        bot_token="test-bot-token",
        channel_id=channel_id,
        ws_connect=transport.connect,
    )
    try:
        with pytest.raises(TRSessionError, match=str(close_code)):
            client.prompt_and_wait_for_pin()
    finally:
        client.close()


def test_tr_session_manager_ready_first_and_retries_indefinitely_on_login_failures(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Verify TRSessionManager prompts before initiate_weblogin and retries on failures."""
    call_order: list[str] = []
    submitted_codes: list[str] = []

    class FakeTRApi:
        def __init__(self, attempt: int) -> None:
            self.attempt = attempt
            self.weblogin_needs_authenticator = True
            self._required_action = "AUTHENTICATOR_VERIFICATION"

        def initiate_weblogin(self) -> int:
            call_order.append(f"initiate_{self.attempt}")
            if self.attempt == 1:
                raise ValueError("Temporary Trade Republic error")
            return 120

        def complete_weblogin(self, code: str | None = None) -> None:
            call_order.append(f"complete_{self.attempt}:{code}")
            submitted_codes.append(str(code))
            if self.attempt == 2:
                raise ValueError("Invalid verification code")

        def _get_weblogin_process(self, *, quiet: bool = True) -> dict[str, Any]:
            assert quiet is True
            return {"status": "PENDING", "requiredAction": None}

        def _await_weblogin_confirmation(self) -> None:
            call_order.append(f"confirm_{self.attempt}")
            if self.attempt == 3:
                raise TimeoutError("The login was not confirmed in time.")

        def save_websession(self) -> None:
            call_order.append(f"save_{self.attempt}")

    attempts = 0

    def api_factory(**_kwargs: Any) -> FakeTRApi:
        nonlocal attempts
        attempts += 1
        return FakeTRApi(attempts)

    class StubDiscord2FAClient:
        def __init__(self) -> None:
            self.prompts: list[str] = []
            self.messages: list[str] = []
            self._codes = iter(["111111", "222222", "333333", "444444"])

        def prompt_and_wait_for_pin(self, prompt_message: str | None = None) -> str:
            call_order.append("prompt")
            self.prompts.append(prompt_message or "")
            return next(self._codes)

        def send_message(self, content: str) -> str:
            self.messages.append(content)
            return f"msg-{len(self.messages)}"

        def close(self) -> None:
            pass

    discord_stub = StubDiscord2FAClient()
    manager = TRSessionManager(
        phone_no="+49000000000",
        pin="0000",
        session_dir=tmp_path,
        discord_client=discord_stub,  # type: ignore[arg-type]
        tr_api_factory=api_factory,
    )

    with caplog.at_level(logging.WARNING, logger="tr_worker.session_manager"):
        api = manager.ensure_authenticated()

    assert api.attempt == 4
    assert submitted_codes == ["222222", "333333", "444444"]
    # Every attempt prompted for code BEFORE initiate_weblogin was called
    assert call_order == [
        "prompt",
        "initiate_1",
        "prompt",
        "initiate_2",
        "complete_2:222222",
        "prompt",
        "initiate_3",
        "complete_3:333333",
        "confirm_3",
        "prompt",
        "initiate_4",
        "complete_4:444444",
        "confirm_4",
        "save_4",
    ]
    assert len(discord_stub.prompts) == 4
    assert "Temporary Trade Republic error" in discord_stub.prompts[1]
    assert "Invalid verification code" in discord_stub.prompts[2]
    assert "not confirmed in time" in discord_stub.prompts[3]
    # Login retries log WARNING (not ERROR) so DiscordErrorHandler doesn't double-alert
    assert all(rec.levelno == logging.WARNING for rec in caplog.records)
    assert manager.has_valid_session() is True


def test_tr_session_manager_resumes_existing_valid_session_without_discord(
    tmp_path: Path,
) -> None:
    """Verify valid persisted session resumes without touching Discord."""
    (tmp_path / "tr_cookies.txt").write_text("# cookies\n", encoding="utf-8")
    (tmp_path / "tr_session.json").write_text(
        json.dumps({"authenticated": True, "updated_at": 1.0}),
        encoding="utf-8",
    )

    class ResumeApi:
        def resume_websession(self) -> bool:
            return True

    class NeverCalledDiscord:
        def prompt_and_wait_for_pin(self, _prompt_message: str | None = None) -> str:
            raise AssertionError("Discord should not be called when session resumes")

        def send_message(self, _content: str) -> str:
            raise AssertionError("Discord should not be called when session resumes")

    manager = TRSessionManager(
        phone_no="+49000000000",
        pin="0000",
        session_dir=tmp_path,
        discord_client=NeverCalledDiscord(),  # type: ignore[arg-type]
        tr_api_factory=lambda **_kw: ResumeApi(),
    )
    api = manager.ensure_authenticated()
    assert isinstance(api, ResumeApi)


def test_settings_removed_discord_2fa_timeout_and_poll_fields() -> None:
    """Verify deprecated 2FA timeout/polling settings are removed from Settings."""
    settings = Settings()
    assert not hasattr(settings, "discord_2fa_timeout_seconds")
    assert not hasattr(settings, "discord_2fa_poll_interval_seconds")
