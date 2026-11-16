"""Unit tests for Trade Republic timeline parser and Discord 2FA session manager."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from config.settings import CATEGORY_GROCERIES, CATEGORY_RESTAURANTS, Settings
from shared.mock_data import load_mock_tr_portfolio, load_mock_tr_timeline
from tr_worker.event_parser import (
    TRRecordKind,
    parse_tr_portfolio_value,
    parse_tr_timeline_events,
)
from tr_worker.session_manager import Discord2FAClient, TRSessionError, TRSessionManager


def test_parse_tr_timeline_events_classifies_all_event_types(
    test_settings: Settings,
) -> None:
    """Verify card debits, interest, Saveback, savings plan, dividend, and SEPA."""
    events = load_mock_tr_timeline()
    parsed = parse_tr_timeline_events(events, test_settings)

    # Expected records:
    # 1. tr-card-1001 (ACME SUPERMARKET, -25.00, after cutoff) -> CASH account, Groceries
    #    (tr-card-old-0999 is before 2026-11-01T09:00:00Z cutoff -> skipped!)
    # 2. tr-card-1002 (FAKE BURGER DINER, -10.00) -> CASH account, Restaurants
    # 3. tr-int-2026-11 (Interest, +100.00) -> CASH account, Interest
    # 4. tr-int-2026-04 (Interest, +80.00) -> CASH account, Interest (backfilled!)
    # 5. tr-saveback-2026-11 (Saveback, +15.00) -> WEALTH account, Interest/Bonus
    # 6. tr-sparplan-2026-10:cash (-50.00) -> CASH account, Fin. investments
    # 7. tr-sparplan-2026-10:wealth (-50.00) -> WEALTH account, Fin. investments
    # 8. tr-div-2026-10 (+10.00) -> WEALTH account, Interest/Dividends
    # 9. tr-sepa-2026-11 (+500.00) -> CASH account, Transfer
    assert len(parsed) == 9

    by_id = {item.external_id: item for item in parsed}
    assert "tr-card-old-0999" not in by_id

    grocery = by_id["tr-card-1001"]
    assert grocery.kind == TRRecordKind.CARD_EXPENSE
    assert grocery.payload.account_id == test_settings.bb_tr_cash_account_id
    assert grocery.payload.category_id == CATEGORY_GROCERIES
    assert grocery.amount == -25.00

    diner = by_id["tr-card-1002"]
    assert diner.payload.category_id == CATEGORY_RESTAURANTS
    assert diner.amount == -10.00

    interest = by_id["tr-int-2026-11"]
    assert interest.kind == TRRecordKind.INTEREST
    assert interest.payload.account_id == test_settings.bb_tr_cash_account_id
    assert interest.payload.category_id == test_settings.bb_category_interest_dividends
    assert interest.amount == 100.00

    saveback = by_id["tr-saveback-2026-11:saveback"]
    assert saveback.kind == TRRecordKind.SAVEBACK
    assert saveback.payload.account_id == test_settings.bb_tr_wealth_account_id
    assert saveback.amount == 15.00
    assert saveback.payload.note is not None
    assert "XF0000000001" in saveback.payload.note
    assert "0.1500 sh" in saveback.payload.note

    spar_cash = by_id["tr-sparplan-2026-10:cash"]
    assert spar_cash.kind == TRRecordKind.SAVINGS_PLAN_CASH
    assert spar_cash.payload.account_id == test_settings.bb_tr_cash_account_id
    assert spar_cash.amount == -50.00

    spar_buy = by_id["tr-sparplan-2026-10:wealth"]
    assert spar_buy.kind == TRRecordKind.SAVINGS_PLAN_BUY
    assert spar_buy.payload.account_id == test_settings.bb_tr_wealth_account_id
    assert spar_buy.amount == -50.00
    assert spar_buy.payload.note is not None
    assert spar_buy.payload.note == "BUY 0.5000 sh XF0000000001 (FAKE) @ 100.00 EUR"


def test_parse_tr_portfolio_value() -> None:
    """Verify portfolio valuation extraction from netValue and positions fallback."""
    portfolio = load_mock_tr_portfolio()
    assert parse_tr_portfolio_value(portfolio) == Decimal("1000.00")

    fallback = {
        "positions": [
            {"netSize": "10", "currentPrice": "100.00"},
            {"marketValue": 500.00},
        ]
    }
    assert parse_tr_portfolio_value(fallback) == Decimal("1500.00")


@respx.mock
def test_discord_2fa_flow_and_tr_session_manager(tmp_path: Path) -> None:
    """Verify Discord bot posts 2FA prompt, reads 4-digit PIN reply, and persists session."""
    channel_id = "000000000000000001"
    respx.post(f"https://discord.com/api/v10/channels/{channel_id}/messages").mock(
        side_effect=[
            httpx.Response(200, json={"id": "msg-prompt-1"}),
            httpx.Response(200, json={"id": "msg-confirm-2"}),
        ]
    )
    respx.get(f"https://discord.com/api/v10/channels/{channel_id}/messages").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": "msg-bot", "content": "Bot msg", "author": {"bot": True}},
                {"id": "msg-user", "content": "Code is 1111", "author": {"bot": False}},
            ],
        )
    )

    submitted_pins: list[str] = []

    class FakeTRApi:
        def __init__(self, **_: Any) -> None:
            self.initiated = False

        def resume_websession(self) -> bool:
            return False

        def initiate_weblogin(self) -> None:
            self.initiated = True

        def complete_weblogin(self, pin: str) -> None:
            submitted_pins.append(pin)

    discord_client = Discord2FAClient(
        bot_token="bot-token",
        channel_id=channel_id,
        timeout_seconds=5.0,
        poll_interval_seconds=0.0,
        sleep_fn=lambda _: None,
    )
    manager = TRSessionManager(
        phone_no="+49000000000",
        pin="0000",
        session_dir=tmp_path / "session",
        discord_client=discord_client,
        tr_api_factory=lambda **kw: FakeTRApi(**kw),
    )

    assert not manager.has_valid_session()
    manager.ensure_authenticated()
    assert submitted_pins == ["1111"]
    assert manager.has_valid_session()
    discord_client.close()


@respx.mock
def test_discord_2fa_timeout_raises_session_error() -> None:
    """Verify TRSessionError is raised if user does not reply in Discord before timeout."""
    channel_id = "000000000000000001"
    respx.post(f"https://discord.com/api/v10/channels/{channel_id}/messages").mock(
        return_value=httpx.Response(200, json={"id": "msg-prompt-1"})
    )
    respx.get(f"https://discord.com/api/v10/channels/{channel_id}/messages").mock(
        return_value=httpx.Response(200, json=[])
    )

    clock = iter([0.0, 1.0, 10.0])
    discord_client = Discord2FAClient(
        bot_token="bot-token",
        channel_id=channel_id,
        timeout_seconds=5.0,
        poll_interval_seconds=0.0,
        sleep_fn=lambda _: None,
        monotonic_fn=lambda: next(clock),
    )
    with pytest.raises(TRSessionError, match="Timed out"):
        discord_client.prompt_and_wait_for_pin()
    discord_client.close()


def test_parse_tr_timeline_detail_v2_nested_sections_and_saveback(
    test_settings: Settings,
) -> None:
    """Verify timelineDetailV2 nested sections, icon ISIN, and localized decimals."""
    events: list[dict[str, Any]] = [
        {
            "id": "tr-v2-sparplan-1",
            "timestamp": "2026-10-16T13:52:16.663Z",
            "eventType": "SAVINGS_PLAN_EXECUTED",
            "title": "S&P 500 Information Tech USD (Acc)",
            "subtitle": "Savings plan executed",
            "icon": "logos/XF0000000001/v2",
            "status": "EXECUTED",
            "amount": {"value": -50.00, "currency": "EUR"},
            "details": {
                "sections": [
                    {
                        "title": "Übersicht",
                        "data": [
                            {
                                "title": "Transaktion",
                                "detail": {
                                    "action": {
                                        "payload": {
                                            "sections": [
                                                {
                                                    "type": "table",
                                                    "data": [
                                                        {
                                                            "title": "Aktien",
                                                            "detail": {"text": "1,0870"},
                                                        },
                                                        {
                                                            "title": "Quotation",
                                                            "detail": {"text": "46,00 €"},
                                                        },
                                                    ],
                                                }
                                            ]
                                        }
                                    }
                                },
                            }
                        ],
                    }
                ]
            },
        },
        {
            "id": "tr-v2-saveback-1",
            "timestamp": "2026-11-02T14:56:53.151Z",
            "eventType": "BENEFITS_SAVEBACK_EXECUTION",
            "title": "S&P 500 Information Tech USD (Acc)",
            "subtitle": "Saveback",
            "status": "EXECUTED",
            "amount": {"value": 8.71, "currency": "EUR"},
            "details": {
                "sections": [
                    {
                        "type": "header",
                        "data": {"icon": {"asset": "logos/XF0000000001/v2"}},
                    },
                    {
                        "title": "Overview",
                        "data": [
                            {
                                "title": "Transaction",
                                "detail": {
                                    "displayValue": {"prefix": "0.1956 x "},
                                },
                            }
                        ],
                    },
                ]
            },
        },
    ]

    parsed = parse_tr_timeline_events(events, test_settings)
    assert len(parsed) == 3
    by_id = {item.external_id: item for item in parsed}

    buy_rec = by_id["tr-v2-sparplan-1:wealth"]
    assert (
        buy_rec.payload.note
        == "BUY 1.0870 sh XF0000000001 (S&P 500 Information Tech USD (Acc)) @ 46.00 EUR"
    )

    saveback_rec = by_id["tr-v2-saveback-1:saveback"]
    assert (
        saveback_rec.payload.note
        == "SAVEBACK 0.1956 sh XF0000000001 (S&P 500 Information Tech USD (Acc)) @ 44.53 EUR"
    )


@respx.mock
def test_discord_2fa_6_digit_authenticator_and_mobile_confirm(tmp_path: Path) -> None:
    """Verify 6-digit Authenticator code followed by mobile app confirmation."""
    channel_id = "000000000000000001"
    respx.post(f"https://discord.com/api/v10/channels/{channel_id}/messages").mock(
        side_effect=[
            httpx.Response(200, json={"id": "msg-prompt-1"}),
            httpx.Response(200, json={"id": "msg-confirm-2"}),
            httpx.Response(200, json={"id": "msg-step2-3"}),
            httpx.Response(200, json={"id": "msg-done-4"}),
        ]
    )
    respx.get(f"https://discord.com/api/v10/channels/{channel_id}/messages").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": "msg-user", "content": "000000", "author": {"bot": False}},
            ],
        )
    )

    events_called: list[str] = []

    class FakeV2TRApi:
        weblogin_needs_authenticator = True
        _required_action = "AUTHENTICATOR_VERIFICATION"

        def __init__(self, **_: Any) -> None:
            pass

        def resume_websession(self) -> bool:
            return False

        def initiate_weblogin(self) -> None:
            events_called.append("initiate")

        def complete_weblogin(self, code: str) -> None:
            events_called.append(f"complete:{code}")

        def _get_weblogin_process(self, quiet: bool = False) -> dict[str, Any]:
            assert quiet is True
            return {"status": "PENDING", "requiredAction": "CONFIRMATION"}

        def _await_weblogin_confirmation(self) -> None:
            events_called.append("await_confirm")

        def save_websession(self) -> None:
            events_called.append("save")

    discord_client = Discord2FAClient(
        bot_token="bot-token",
        channel_id=channel_id,
        timeout_seconds=5.0,
        poll_interval_seconds=0.0,
        sleep_fn=lambda _: None,
    )
    manager = TRSessionManager(
        phone_no="+49000000000",
        pin="0000",
        session_dir=tmp_path / "session",
        discord_client=discord_client,
        tr_api_factory=lambda **kw: FakeV2TRApi(**kw),
    )

    manager.ensure_authenticated()
    assert events_called == ["initiate", "complete:000000", "await_confirm", "save"]
    discord_client.close()
