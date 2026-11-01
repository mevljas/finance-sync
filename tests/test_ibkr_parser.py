"""Unit tests for IBKR Flex Web Service client and XML parser."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
import respx
from ibkr_worker.flex_client import (
    FLEX_GET_STATEMENT_URL,
    FLEX_SEND_REQUEST_URL,
    FlexClient,
    FlexWebServiceError,
    parse_flex_xml,
)
from shared.mock_data import load_mock_ibkr_xml


def test_parse_flex_xml_extracts_trades_dividends_and_open_positions() -> None:
    """Verify ibflex2 parses FAKE ETF trades, dividends, OpenPositions, and skips deposits."""
    xml_bytes = load_mock_ibkr_xml()
    stmt = parse_flex_xml(xml_bytes)

    assert stmt.account_id == "DU0000000"
    assert len(stmt.trades) == 2

    t1 = stmt.trades[0]
    assert t1.trade_id == "TRD-1001"
    assert t1.symbol == "FAKE"
    assert t1.isin == "XF0000000001"
    assert t1.side == "BUY"
    assert t1.quantity == Decimal("5")
    assert t1.price == Decimal("100.00")
    assert t1.net_cash == Decimal("-501.00")
    assert t1.timestamp == "2026-10-15T12:00:00Z"

    # CashTransactions should include Dividends (DIV-2001) and skip Deposits/Withdrawals (DEP-3001)
    assert len(stmt.cash_events) == 1
    div = stmt.cash_events[0]
    assert div.transaction_id == "DIV-2001"
    assert div.amount == Decimal("25.00")
    assert div.isin == "XF0000000001"

    assert stmt.open_positions_value == Decimal("10000.00")
    assert stmt.ending_cash == Decimal("100.00")
    assert stmt.total_portfolio_value == Decimal("10100.00")


@respx.mock
def test_flex_client_two_step_polling_with_retry_on_1019() -> None:
    """Verify SendRequest -> wait 10s -> GetStatement two-step polling flow."""
    waits: list[float] = []
    send_xml = (
        "<FlexStatementResponse timestamp='01 November, 2026 12:00 PM EDT'>"
        "<Status>Success</Status>"
        "<ReferenceCode>REF-987654</ReferenceCode>"
        f"<Url>{FLEX_GET_STATEMENT_URL}</Url>"
        "</FlexStatementResponse>"
    )
    in_progress_xml = (
        b"<FlexStatementResponse>"
        b"<Status>Fail</Status>"
        b"<ErrorCode>1019</ErrorCode>"
        b"<ErrorMessage>Statement generation in progress. Please try again shortly.</ErrorMessage>"
        b"</FlexStatementResponse>"
    )

    respx.get(FLEX_SEND_REQUEST_URL).mock(return_value=httpx.Response(200, text=send_xml))
    respx.get(FLEX_GET_STATEMENT_URL).mock(
        side_effect=[
            httpx.Response(200, content=in_progress_xml),
            httpx.Response(200, content=load_mock_ibkr_xml()),
        ]
    )

    with FlexClient(
        token="flex-token",
        query_id="123456",
        wait_seconds=10.0,
        sleep_fn=waits.append,
    ) as client:
        data = client.fetch_and_parse()

    assert waits == [10.0, 10.0]
    assert len(data.trades) == 2
    assert data.total_portfolio_value == Decimal("10100.00")


@respx.mock
def test_flex_client_raises_on_error_status() -> None:
    """Verify FlexWebServiceError is raised when SendRequest fails."""
    fail_xml = (
        "<FlexStatementResponse>"
        "<Status>Fail</Status>"
        "<ErrorCode>1012</ErrorCode>"
        "<ErrorMessage>Token has expired.</ErrorMessage>"
        "</FlexStatementResponse>"
    )
    respx.get(FLEX_SEND_REQUEST_URL).mock(return_value=httpx.Response(200, text=fail_xml))

    with (
        FlexClient(token="expired", query_id="123", wait_seconds=0.0) as client,
        pytest.raises(FlexWebServiceError, match="1012"),
    ):
        client.send_request()
