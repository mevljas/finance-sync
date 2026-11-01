"""Synthetic mock data provider for offline execution and automated testing."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

DEFAULT_IBKR_FLEX_XML = """\
<FlexQueryResponse queryName="ActivityFlexQuery" type="AF">
  <FlexStatements count="1">
    <FlexStatement accountId="DU0000000" fromDate="2026-01-01" toDate="2026-11-01"
      period="Last365CalendarDays" whenGenerated="2026-11-01;12:00:00">
      <CashReport>
        <CashReportCurrency accountId="DU0000000" currency="BASE_SUMMARY"
          endingCash="100.00" endingSettledCash="100.00" />
      </CashReport>
      <OpenPositions>
        <OpenPosition accountId="DU0000000" assetCategory="STK" symbol="FAKE"
          description="FAKE GLOBAL INDEX UCITS ETF" isin="XF0000000001" currency="EUR"
          position="100" markPrice="100.00" positionValue="10000.00" side="Long" />
      </OpenPositions>
      <Trades>
        <Trade accountId="DU0000000" assetCategory="STK" symbol="FAKE"
          description="FAKE GLOBAL INDEX UCITS ETF" isin="XF0000000001" currency="EUR"
          tradeID="TRD-1001" dateTime="2026-10-15;12:00:00" tradeDate="2026-10-15"
          settleDateTarget="2026-10-17" transactionType="ExchTrade" buySell="BUY"
          quantity="5" tradePrice="100.00" tradeMoney="500.00" ibCommission="-1.00"
          ibCommissionCurrency="EUR" netCash="-501.00" proceeds="-500.00" />
        <Trade accountId="DU0000000" assetCategory="STK" symbol="FAKE"
          description="FAKE GLOBAL INDEX UCITS ETF" isin="XF0000000001" currency="EUR"
          tradeID="TRD-1002" dateTime="2026-09-15;12:00:00" tradeDate="2026-09-15"
          settleDateTarget="2026-09-17" transactionType="ExchTrade" buySell="BUY"
          quantity="10" tradePrice="100.00" tradeMoney="1000.00" ibCommission="-1.00"
          ibCommissionCurrency="EUR" netCash="-1001.00" proceeds="-1000.00" />
      </Trades>
      <CashTransactions>
        <CashTransaction accountId="DU0000000" currency="EUR" assetCategory="STK"
          symbol="FAKE" description="FAKE(XF0000000001) CASH DIVIDEND EUR 0.25 PER SHARE"
          isin="XF0000000001" amount="25.00" type="Dividends" transactionID="DIV-2001"
          dateTime="2026-10-20;12:00:00" reportDate="2026-10-20" />
        <CashTransaction accountId="DU0000000" currency="EUR"
          description="DEMO ELECTRONIC FUND TRANSFER" amount="1000.00"
          type="Deposits/Withdrawals" transactionID="DEP-3001"
          dateTime="2026-10-14;12:00:00" reportDate="2026-10-14" />
      </CashTransactions>
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>
"""


def load_mock_ibkr_xml() -> bytes:
    """Load synthetic IBKR Activity Flex Query XML payload."""
    xml_path = FIXTURES_DIR / "ibkr_flex_statement.xml"
    if xml_path.exists():
        return xml_path.read_bytes()
    return DEFAULT_IBKR_FLEX_XML.encode("utf-8")


def load_mock_tr_timeline() -> list[dict[str, Any]]:
    """Load synthetic Trade Republic timeline events JSON payload."""
    json_path = FIXTURES_DIR / "tr_timeline_events.json"
    if json_path.exists():
        data: list[dict[str, Any]] = json.loads(json_path.read_text(encoding="utf-8"))
        return data
    return [
        {
            "id": "tr-card-1001",
            "timestamp": "2026-11-01T12:00:00.000Z",
            "eventType": "card_successful_transaction",
            "title": "ACME SUPERMARKET",
            "subtitle": "Card payment",
            "status": "EXECUTED",
            "amount": {"value": -25.00, "currency": "EUR"},
            "mcc": "5411",
        },
        {
            "id": "tr-sparplan-2026-10",
            "timestamp": "2026-10-16T10:00:00.000Z",
            "eventType": "SAVINGS_PLAN_EXECUTED",
            "title": "Fake Global Index ETF (Acc)",
            "subtitle": "Savings plan executed",
            "status": "EXECUTED",
            "amount": {"value": -50.00, "currency": "EUR"},
            "details": {
                "isin": "XF0000000001",
                "symbol": "FAKE",
                "shares": 0.5000,
                "price": 100.00,
            },
        },
    ]


def load_mock_tr_portfolio() -> dict[str, Any]:
    """Load synthetic Trade Republic portfolio valuation JSON payload."""
    json_path = FIXTURES_DIR / "tr_portfolio.json"
    if json_path.exists():
        data: dict[str, Any] = json.loads(json_path.read_text(encoding="utf-8"))
        return data
    return {"netValue": 1000.00, "currency": "EUR", "positions": []}
