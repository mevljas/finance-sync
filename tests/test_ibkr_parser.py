"""Unit tests for IBKR Flex Web Service client and XML parser."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx
from config.settings import Settings
from ibkr_worker.flex_client import (
    FLEX_GET_STATEMENT_URL,
    FLEX_SEND_REQUEST_URL,
    FlexClient,
    FlexWebServiceError,
    merge_statement_data,
    parse_flex_xml,
)
from ibkr_worker.worker import IBKRWorker, main
from shared.budgetbakers import BudgetBakersClient
from shared.database import DeduplicationStore
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


def test_parse_flex_xml_converts_non_eur_open_positions_via_fx_rate_to_base() -> None:
    """Verify OpenPositions multiplies foreign positionValue by fxRateToBase."""
    xml_bytes = (
        b'<FlexQueryResponse queryName="ActivityFlexQuery" type="AF">\n'
        b'  <FlexStatements count="1">\n'
        b'    <FlexStatement accountId="DU0000000" fromDate="2026-01-01"\n'
        b'                   toDate="2026-11-15" period="Last365CalendarDays"\n'
        b'                   whenGenerated="2026-11-15;12:00:00">\n'
        b"      <CashReport>\n"
        b'        <CashReportCurrency accountId="DU0000000" currency="BASE_SUMMARY"\n'
        b'                            endingCash="150.00" endingSettledCash="150.00" />\n'
        b"      </CashReport>\n"
        b"      <OpenPositions>\n"
        b'        <OpenPosition accountId="DU0000000" assetCategory="STK" symbol="FAKEEUR"\n'
        b'                      isin="XF0000000001" currency="EUR" fxRateToBase="1"\n'
        b'                      position="10" markPrice="100.00" positionValue="1000.00"\n'
        b'                      side="Long" />\n'
        b'        <OpenPosition accountId="DU0000000" assetCategory="STK" symbol="FAKEUSD"\n'
        b'                      isin="US0000000001" currency="USD" fxRateToBase="0.92"\n'
        b'                      position="10" markPrice="200.00" positionValue="2000.00"\n'
        b'                      side="Long" />\n'
        b'        <OpenPosition accountId="DU0000000" assetCategory="STK" symbol="FAKEGBP"\n'
        b'                      isin="GB0000000001" currency="GBP" fxRateToBase="1.20"\n'
        b'                      position="5" markPrice="100.00" side="Long" />\n'
        b"      </OpenPositions>\n"
        b"    </FlexStatement>\n"
        b"  </FlexStatements>\n"
        b"</FlexQueryResponse>"
    )

    stmt = parse_flex_xml(xml_bytes)

    # 1000*1 + 2000*0.92 + (5*100)*1.20 = 1000.00 + 1840.00 + 600.00 = 3440.00
    assert stmt.open_positions_value == Decimal("3440.00")
    assert stmt.ending_cash == Decimal("150.00")
    assert stmt.total_portfolio_value == Decimal("3590.00")


def test_parse_flex_xml_skips_cash_fx_trades_and_quantizes_ending_cash() -> None:
    """Verify parse_flex_xml skips assetCategory=CASH FX trades and rounds ending_cash."""
    xml_bytes = (
        b'<FlexQueryResponse queryName="ActivityFlexQuery" type="AF">\n'
        b'  <FlexStatements count="1">\n'
        b'    <FlexStatement accountId="DU0000000" fromDate="2026-01-01"\n'
        b'                   toDate="2026-11-15" period="Last365CalendarDays"\n'
        b'                   whenGenerated="2026-11-15;12:00:00">\n'
        b"      <CashReport>\n"
        b'        <CashReportCurrency accountId="DU0000000" currency="BASE_SUMMARY"\n'
        b'                            endingCash="2586.721525031"\n'
        b'                            endingSettledCash="2586.721525031" />\n'
        b"      </CashReport>\n"
        b"      <Trades>\n"
        b'        <Trade accountId="DU0000000" assetCategory="STK" symbol="FAKE"\n'
        b'               isin="XF0000000001" currency="USD" tradeID="TRD-0001"\n'
        b'               dateTime="20261030;09:39:39" tradeDate="20261030"\n'
        b'               buySell="BUY" quantity="4" tradePrice="280.22"\n'
        b'               netCash="-1121.23106925" proceeds="-1120.88"\n'
        b'               ibCommission="-0.35106925" />\n'
        b'        <Trade accountId="DU0000000" assetCategory="CASH" symbol="EUR.USD"\n'
        b'               isin="" securityID="" currency="USD" tradeID="TRD-0002"\n'
        b'               dateTime="20261030;09:39:40" tradeDate="20261030"\n'
        b'               buySell="SELL" quantity="-1004.51" tradePrice="1.11567"\n'
        b'               netCash="0" proceeds="1120.7016717" ibCommission="0" />\n'
        b'        <Trade accountId="DU0000000" assetCategory="CASH" symbol="EUR.USD"\n'
        b'               isin="" securityID="" currency="USD" tradeID="TRD-0003"\n'
        b'               dateTime="20261030;17:00:00" tradeDate="20261030"\n'
        b'               buySell="BUY" quantity="0.00528912" tradePrice="1.11616961"\n'
        b'               netCash="0" proceeds="-0.005903551" ibCommission="0" />\n'
        b"      </Trades>\n"
        b"    </FlexStatement>\n"
        b"  </FlexStatements>\n"
        b"</FlexQueryResponse>"
    )

    stmt = parse_flex_xml(xml_bytes)

    assert len(stmt.trades) == 1
    assert stmt.trades[0].symbol == "FAKE"
    assert stmt.ending_cash == Decimal("2586.72")


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


def test_merge_statement_data_deduplicates_and_uses_latest_positions(
    test_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify merging 2025.xml and 2026.xml deduplicates trades and uses latest OpenPositions."""
    xml_2025 = (
        b'<FlexQueryResponse queryName="ActivityFlexQuery" type="AF">\n'
        b'  <FlexStatements count="1">\n'
        b'    <FlexStatement accountId="DU0000000" fromDate="2025-01-01" toDate="2025-12-31"\n'
        b'                   period="" whenGenerated="2026-11-15;12:00:00">\n'
        b"      <CashReport>\n"
        b'        <CashReportCurrency accountId="DU0000000" currency="BASE_SUMMARY"\n'
        b'                            endingCash="5000.00" endingSettledCash="5000.00" />\n'
        b"      </CashReport>\n"
        b"      <OpenPositions>\n"
        b'        <OpenPosition accountId="DU0000000" assetCategory="STK" symbol="FAKE"\n'
        b'                      isin="XF0000000001" currency="EUR" fxRateToBase="1"\n'
        b'                      position="10" markPrice="100.00" positionValue="1000.00" />\n'
        b"      </OpenPositions>\n"
        b"      <Trades>\n"
        b'        <Trade accountId="DU0000000" assetCategory="STK" symbol="FAKE"\n'
        b'               isin="XF0000000001" currency="EUR" tradeID="TRD-2025"\n'
        b'               dateTime="20250615;10:00:00" buySell="BUY" quantity="10"\n'
        b'               tradePrice="100.00" netCash="-1001.00" />\n'
        b"      </Trades>\n"
        b"    </FlexStatement>\n"
        b"  </FlexStatements>\n"
        b"</FlexQueryResponse>"
    )
    xml_2026 = (
        b'<FlexQueryResponse queryName="ActivityFlexQuery" type="AF">\n'
        b'  <FlexStatements count="1">\n'
        b'    <FlexStatement accountId="DU0000000" fromDate="2025-06-01" toDate="2026-11-15"\n'
        b'                   period="" whenGenerated="2026-11-15;12:00:00">\n'
        b"      <CashReport>\n"
        b'        <CashReportCurrency accountId="DU0000000" currency="BASE_SUMMARY"\n'
        b'                            endingCash="250.00" endingSettledCash="250.00" />\n'
        b"      </CashReport>\n"
        b"      <OpenPositions>\n"
        b'        <OpenPosition accountId="DU0000000" assetCategory="STK" symbol="FAKE"\n'
        b'                      isin="XF0000000001" currency="EUR" fxRateToBase="1"\n'
        b'                      position="15" markPrice="120.00" positionValue="1800.00" />\n'
        b"      </OpenPositions>\n"
        b"      <Trades>\n"
        b'        <Trade accountId="DU0000000" assetCategory="STK" symbol="FAKE"\n'
        b'               isin="XF0000000001" currency="EUR" tradeID="TRD-2025"\n'
        b'               dateTime="20250615;10:00:00" buySell="BUY" quantity="10"\n'
        b'               tradePrice="100.00" netCash="-1001.00" />\n'
        b'        <Trade accountId="DU0000000" assetCategory="STK" symbol="FAKE"\n'
        b'               isin="XF0000000001" currency="EUR" tradeID="TRD-2026"\n'
        b'               dateTime="20260615;10:00:00" buySell="BUY" quantity="5"\n'
        b'               tradePrice="110.00" netCash="-551.00" />\n'
        b"      </Trades>\n"
        b"    </FlexStatement>\n"
        b"  </FlexStatements>\n"
        b"</FlexQueryResponse>"
    )

    xml_dir = tmp_path / "xml_inputs"
    xml_dir.mkdir()
    f_2025 = xml_dir / "2025.xml"
    f_2026 = xml_dir / "2026.xml"
    f_2025.write_bytes(xml_2025)
    f_2026.write_bytes(xml_2026)

    merged = merge_statement_data([parse_flex_xml(xml_2026), parse_flex_xml(xml_2025)])
    assert [t.trade_id for t in merged.trades] == ["TRD-2025", "TRD-2026"]
    assert merged.ending_cash == Decimal("250.00")
    assert merged.open_positions_value == Decimal("1800.00")
    assert len(merged.open_positions) == 1
    assert merged.open_positions[0].quantity == Decimal("15")

    db = DeduplicationStore(tmp_path / "xml_test.db")
    with BudgetBakersClient(token="test-token", dry_run=True, use_mock_data=True) as bb_client:
        worker = IBKRWorker(
            settings=test_settings.model_copy(update={"dry_run": True, "use_mock_data": True}),
            db=db,
            bb_client=bb_client,
            xml_paths=[xml_dir],
        )
        try:
            summary = worker.sync_once()
            assert summary.trades_synced == 2
        finally:
            worker.close()

    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "cli_test.db"))
    monkeypatch.setenv("USE_MOCK_DATA", "true")
    exit_code = main(
        [
            "--xml-file",
            str(f_2025),
            str(f_2026),
            "--dry-run",
        ]
    )
    assert exit_code == 0


def test_parse_flex_xml_prefers_trade_date_ending_cash_over_settled_cash() -> None:
    """Verify ending_cash uses trade-date endingCash (including zero) with settled fallback."""
    unsettled_xml = (
        b'<FlexQueryResponse queryName="ActivityFlexQuery" type="AF">\n'
        b'  <FlexStatements count="1">\n'
        b'    <FlexStatement accountId="DU0000000" fromDate="2026-10-22" toDate="2026-11-20"\n'
        b'                   period="Last30CalendarDays" whenGenerated="2026-11-21;03:00:00">\n'
        b"      <CashReport>\n"
        b'        <CashReportCurrency accountId="DU0000000" currency="BASE_SUMMARY"\n'
        b'                            endingCash="2490.877375832"\n'
        b'                            endingSettledCash="17579.177375832" />\n'
        b"      </CashReport>\n"
        b"    </FlexStatement>\n"
        b"  </FlexStatements>\n"
        b"</FlexQueryResponse>"
    )
    assert parse_flex_xml(unsettled_xml).ending_cash == Decimal("2490.88")

    zero_cash_xml = (
        b'<FlexQueryResponse queryName="ActivityFlexQuery" type="AF">\n'
        b'  <FlexStatements count="1">\n'
        b'    <FlexStatement accountId="DU0000000" fromDate="2026-10-22" toDate="2026-11-20"\n'
        b'                   period="Last30CalendarDays" whenGenerated="2026-11-21;03:00:00">\n'
        b"      <CashReport>\n"
        b'        <CashReportCurrency accountId="DU0000000" currency="BASE_SUMMARY"\n'
        b'                            endingCash="0"\n'
        b'                            endingSettledCash="500.00" />\n'
        b"      </CashReport>\n"
        b"    </FlexStatement>\n"
        b"  </FlexStatements>\n"
        b"</FlexQueryResponse>"
    )
    assert parse_flex_xml(zero_cash_xml).ending_cash == Decimal("0.00")

    settled_fallback_xml = (
        b'<FlexQueryResponse queryName="ActivityFlexQuery" type="AF">\n'
        b'  <FlexStatements count="1">\n'
        b'    <FlexStatement accountId="DU0000000" fromDate="2026-10-22" toDate="2026-11-20"\n'
        b'                   period="Last30CalendarDays" whenGenerated="2026-11-21;03:00:00">\n'
        b"      <CashReport>\n"
        b'        <CashReportCurrency accountId="DU0000000" currency="BASE_SUMMARY"\n'
        b'                            endingSettledCash="123.45" />\n'
        b"      </CashReport>\n"
        b"    </FlexStatement>\n"
        b"  </FlexStatements>\n"
        b"</FlexQueryResponse>"
    )
    assert parse_flex_xml(settled_fallback_xml).ending_cash == Decimal("123.45")


def test_parse_flex_xml_resolves_missing_trade_fx_rate_from_cash_fx_and_open_positions() -> None:
    """Verify foreign trades without fxRateToBase use same-day CASH FX or OpenPositions rate."""
    xml_bytes = (
        b'<FlexQueryResponse queryName="ActivityFlexQuery" type="AF">\n'
        b'  <FlexStatements count="1">\n'
        b'    <FlexStatement accountId="DU0000000" fromDate="2026-10-22" toDate="2026-11-20"\n'
        b'                   period="Last30CalendarDays" whenGenerated="2026-11-21;03:00:00">\n'
        b"      <CashReport>\n"
        b'        <CashReportCurrency accountId="DU0000000" currency="BASE_SUMMARY"\n'
        b'                            endingCash="100.00" endingSettledCash="100.00" />\n'
        b"      </CashReport>\n"
        b"      <OpenPositions>\n"
        b'        <OpenPosition accountId="DU0000000" assetCategory="STK" symbol="FAKEUSD"\n'
        b'                      isin="US0000000001" currency="USD" fxRateToBase="0.91"\n'
        b'                      position="6" markPrice="200.00" positionValue="1200.00" />\n'
        b"      </OpenPositions>\n"
        b"      <Trades>\n"
        b'        <Trade accountId="DU0000000" assetCategory="STK" symbol="FAKEUSD"\n'
        b'               isin="US0000000001" currency="USD" tradeID="TRD-FX-1"\n'
        b'               dateTime="20261030;09:39:39" tradeDate="20261030"\n'
        b'               buySell="BUY" quantity="4" tradePrice="280.00"\n'
        b'               netCash="-1120.00" proceeds="-1120.00" ibCommission="0" />\n'
        b'        <Trade accountId="DU0000000" assetCategory="STK" symbol="FAKEUSD"\n'
        b'               isin="US0000000001" currency="USD" tradeID="TRD-FX-2"\n'
        b'               dateTime="20261105;10:00:00" tradeDate="20261105"\n'
        b'               buySell="BUY" quantity="1" tradePrice="200.00"\n'
        b'               netCash="-200.00" proceeds="-200.00" ibCommission="0" />\n'
        b'        <Trade accountId="DU0000000" assetCategory="STK" symbol="FAKEUSD"\n'
        b'               isin="US0000000001" currency="USD" tradeID="TRD-FX-3"\n'
        b'               dateTime="20261110;10:00:00" tradeDate="20261110"\n'
        b'               buySell="BUY" quantity="1" tradePrice="200.00"\n'
        b'               netCash="-200.00" proceeds="-200.00" ibCommission="0"\n'
        b'               fxRateToBase="0.88" />\n'
        b'        <Trade accountId="DU0000000" assetCategory="CASH" symbol="EUR.USD"\n'
        b'               isin="" securityID="" currency="USD" tradeID="TRD-CASH-SMALL"\n'
        b'               dateTime="20261030;17:00:00" tradeDate="20261030"\n'
        b'               buySell="BUY" quantity="0.01" tradePrice="1.25"\n'
        b'               netCash="0" proceeds="-0.0125" ibCommission="0" />\n'
        b'        <Trade accountId="DU0000000" assetCategory="CASH" symbol="EUR.USD"\n'
        b'               isin="" securityID="" currency="USD" tradeID="TRD-CASH-MAIN"\n'
        b'               dateTime="20261030;09:39:40" tradeDate="20261030"\n'
        b'               buySell="SELL" quantity="-1000.00" tradePrice="1.12"\n'
        b'               netCash="0" proceeds="1120.00" ibCommission="0" />\n'
        b"      </Trades>\n"
        b"    </FlexStatement>\n"
        b"  </FlexStatements>\n"
        b"</FlexQueryResponse>"
    )

    stmt = parse_flex_xml(xml_bytes)
    by_id = {t.trade_id: t for t in stmt.trades}
    assert by_id["TRD-FX-1"].fx_rate_to_base == Decimal("1") / Decimal("1.12")
    assert by_id["TRD-FX-2"].fx_rate_to_base == Decimal("0.91")
    assert by_id["TRD-FX-3"].fx_rate_to_base == Decimal("0.88")
