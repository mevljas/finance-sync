"""IBKR Flex Web Service client and Activity Flex Query XML parser using ibflex2."""

from __future__ import annotations

import datetime as dt
import logging
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx
from ibflex import parser as ibflex_parser

logger = logging.getLogger(__name__)

FLEX_SEND_REQUEST_URL = (
    "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest"
)
FLEX_GET_STATEMENT_URL = (
    "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement"
)


class FlexWebServiceError(RuntimeError):
    """Raised when IBKR Flex Web Service returns an error or invalid response."""


@dataclass(frozen=True, slots=True)
class IBKRTradeEvent:
    """Normalized settled stock/ETF trade parsed from IBKR Flex XML."""

    trade_id: str
    symbol: str
    isin: str
    side: str
    quantity: Decimal
    price: Decimal
    net_cash: Decimal
    currency: str
    timestamp: str


@dataclass(frozen=True, slots=True)
class IBKRCashEvent:
    """Normalized dividend, withholding tax, or interest cash transaction."""

    transaction_id: str
    transaction_type: str
    symbol: str
    isin: str
    amount: Decimal
    currency: str
    description: str
    timestamp: str


@dataclass(frozen=True, slots=True)
class IBKRStatementData:
    """Aggregated result from parsing an IBKR Activity Flex Query XML statement."""

    account_id: str
    trades: list[IBKRTradeEvent] = field(default_factory=list)
    cash_events: list[IBKRCashEvent] = field(default_factory=list)
    open_positions_value: Decimal = Decimal("0.00")
    ending_cash: Decimal = Decimal("0.00")

    @property
    def total_portfolio_value(self) -> Decimal:
        """Return combined market value of OpenPositions plus ending settled cash."""
        return self.open_positions_value + self.ending_cash


def _format_iso_timestamp(value: object) -> str:
    """Normalize an ibflex2 date/datetime or raw string into an ISO-8601 UTC string."""
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.UTC)
        return str(value.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
    if isinstance(value, dt.date):
        return f"{value.isoformat()}T00:00:00Z"
    raw = str(value or "").strip()
    if ";" in raw:
        date_part, time_part = raw.split(";", 1)
        if len(date_part) == 8 and date_part.isdigit():
            date_part = f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]}"
        if len(time_part) == 6 and time_part.isdigit():
            time_part = f"{time_part[:2]}:{time_part[2:4]}:{time_part[4:6]}"
        return f"{date_part}T{time_part}Z"
    if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
        return f"{raw}T00:00:00Z"
    return raw or "1970-01-01T00:00:00Z"


def _to_decimal(value: Any, default: str = "0") -> Decimal:
    if value is None or value == "":
        return Decimal(default)
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _enum_name(value: Any) -> str:
    if value is None:
        return ""
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name
    val = getattr(value, "value", None)
    if isinstance(val, str):
        return val
    return str(value)


def parse_flex_xml(xml_bytes: bytes) -> IBKRStatementData:
    """Parse IBKR Activity Flex Query XML using ibflex2 into IBKRStatementData."""
    try:
        response = ibflex_parser.parse(xml_bytes)
    except Exception as exc:
        raise FlexWebServiceError(f"ibflex2 failed to parse XML: {exc}") from exc

    statements = getattr(response, "FlexStatements", ())
    if not statements:
        raise FlexWebServiceError("No FlexStatements found in IBKR XML response")

    account_id = ""
    trades: list[IBKRTradeEvent] = []
    cash_events: list[IBKRCashEvent] = []
    open_positions_value = Decimal("0.00")
    ending_cash = Decimal("0.00")

    for stmt in statements:
        if not account_id:
            account_id = str(getattr(stmt, "accountId", "") or "")

        # 1. Parse Trades
        for trade in getattr(stmt, "Trades", ()):
            symbol = str(getattr(trade, "symbol", "") or "").strip()
            isin = str(
                getattr(trade, "isin", None) or getattr(trade, "securityID", None) or symbol
            ).strip()
            side_raw = _enum_name(getattr(trade, "buySell", "BUY")).upper()
            side = "SELL" if "SELL" in side_raw else "BUY"
            quantity = abs(_to_decimal(getattr(trade, "quantity", 0)))
            price = _to_decimal(getattr(trade, "tradePrice", 0))
            net_cash = _to_decimal(getattr(trade, "netCash", None))
            if net_cash == Decimal("0"):
                proceeds = _to_decimal(getattr(trade, "proceeds", 0))
                commission = _to_decimal(getattr(trade, "ibCommission", 0))
                net_cash = proceeds + commission
            if side == "BUY" and net_cash > Decimal("0"):
                net_cash = -net_cash
            elif side == "SELL" and net_cash < Decimal("0"):
                net_cash = abs(net_cash)

            trade_id = str(
                getattr(trade, "tradeID", None)
                or getattr(trade, "transactionID", None)
                or f"{symbol}-{getattr(trade, 'dateTime', '')}"
            )
            dt_val = getattr(trade, "dateTime", None) or getattr(trade, "tradeDate", None)
            timestamp = _format_iso_timestamp(dt_val)
            currency = str(getattr(trade, "currency", "EUR") or "EUR")

            trades.append(
                IBKRTradeEvent(
                    trade_id=trade_id,
                    symbol=symbol,
                    isin=isin,
                    side=side,
                    quantity=quantity,
                    price=price,
                    net_cash=net_cash,
                    currency=currency,
                    timestamp=timestamp,
                )
            )

        # 2. Parse CashTransactions (Dividends & Interest; skip Deposits/Withdrawals)
        for cash_tx in getattr(stmt, "CashTransactions", ()):
            tx_type = _enum_name(getattr(cash_tx, "type", "")).lower()
            if "deposit" in tx_type or "withdrawal" in tx_type:
                continue
            amount = _to_decimal(getattr(cash_tx, "amount", 0))
            if amount == Decimal("0"):
                continue
            tx_id = str(
                getattr(cash_tx, "transactionID", None)
                or f"cash-{getattr(cash_tx, 'dateTime', '')}-{amount}"
            )
            symbol = str(getattr(cash_tx, "symbol", "") or "").strip()
            isin = str(getattr(cash_tx, "isin", "") or symbol).strip()
            description = str(getattr(cash_tx, "description", "") or tx_type).strip()
            dt_val = getattr(cash_tx, "dateTime", None) or getattr(cash_tx, "reportDate", None)
            timestamp = _format_iso_timestamp(dt_val)
            currency = str(getattr(cash_tx, "currency", "EUR") or "EUR")

            cash_events.append(
                IBKRCashEvent(
                    transaction_id=tx_id,
                    transaction_type=tx_type,
                    symbol=symbol,
                    isin=isin,
                    amount=amount,
                    currency=currency,
                    description=description,
                    timestamp=timestamp,
                )
            )

        # 3. Parse OpenPositions market value
        for pos in getattr(stmt, "OpenPositions", ()):
            pos_val = _to_decimal(getattr(pos, "positionValue", None))
            if pos_val == Decimal("0"):
                qty = _to_decimal(getattr(pos, "position", 0))
                mark = _to_decimal(getattr(pos, "markPrice", 0))
                pos_val = qty * mark
            open_positions_value += pos_val

        # 4. Parse CashReport ending cash
        for cash_rep in getattr(stmt, "CashReport", ()):
            ccy = str(getattr(cash_rep, "currency", "") or "")
            if ccy in ("BASE_SUMMARY", "EUR") or ending_cash == Decimal("0.00"):
                ending_cash = _to_decimal(
                    getattr(cash_rep, "endingSettledCash", None)
                    or getattr(cash_rep, "endingCash", 0)
                )
                if ccy == "BASE_SUMMARY":
                    break

    return IBKRStatementData(
        account_id=account_id,
        trades=trades,
        cash_events=cash_events,
        open_positions_value=open_positions_value,
        ending_cash=ending_cash,
    )


class FlexClient:
    """Two-step HTTP client for IBKR Flex Web Service."""

    def __init__(
        self,
        token: str,
        query_id: str,
        *,
        wait_seconds: float = 10.0,
        http_client: httpx.Client | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.token = token
        self.query_id = query_id
        self.wait_seconds = wait_seconds
        self._sleep = sleep_fn
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(timeout=30.0)

    def close(self) -> None:
        """Close the owned HTTP client."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> FlexClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def send_request(self) -> tuple[str, str]:
        """Step 1: Call SendRequest and return (reference_code, get_statement_url)."""
        resp = self._client.get(
            FLEX_SEND_REQUEST_URL,
            params={"t": self.token, "q": self.query_id, "v": "3"},
        )
        if resp.status_code >= 400:
            raise FlexWebServiceError(f"SendRequest HTTP {resp.status_code}: {resp.text}")

        try:
            root = ET.fromstring(resp.text)  # noqa: S314
        except ET.ParseError as exc:
            raise FlexWebServiceError(f"Invalid XML in SendRequest response: {exc}") from exc

        status = (root.findtext("Status") or "").strip()
        if status != "Success":
            err_code = (root.findtext("ErrorCode") or "").strip()
            err_msg = (root.findtext("ErrorMessage") or "Unknown Flex error").strip()
            raise FlexWebServiceError(f"SendRequest failed ({err_code}): {err_msg}")

        ref_code = (root.findtext("ReferenceCode") or "").strip()
        url = (root.findtext("Url") or FLEX_GET_STATEMENT_URL).strip()
        if not ref_code:
            raise FlexWebServiceError("SendRequest succeeded but ReferenceCode is missing")
        return ref_code, url

    def get_statement(
        self,
        reference_code: str,
        url: str = FLEX_GET_STATEMENT_URL,
        max_retries: int = 3,
    ) -> bytes:
        """Step 2: Poll GetStatement after waiting wait_seconds."""
        for attempt in range(max_retries):
            self._sleep(self.wait_seconds)
            resp = self._client.get(
                url,
                params={"t": self.token, "q": reference_code, "v": "3"},
            )
            if resp.status_code >= 400:
                raise FlexWebServiceError(f"GetStatement HTTP {resp.status_code}: {resp.text}")

            content = resp.content
            if b"<FlexStatementResponse" in content and b"<Status>Fail</Status>" in content:
                root = ET.fromstring(content)  # noqa: S314
                err_code = (root.findtext("ErrorCode") or "").strip()
                err_msg = (root.findtext("ErrorMessage") or "").strip()
                if err_code in ("1019", "1018") and attempt < max_retries - 1:
                    logger.info("Flex statement still generating (%s); retrying...", err_code)
                    continue
                raise FlexWebServiceError(f"GetStatement failed ({err_code}): {err_msg}")
            return content

        raise FlexWebServiceError("GetStatement retries exhausted")

    def fetch_and_parse(self) -> IBKRStatementData:
        """Execute two-step Flex query and parse the returned XML statement."""
        ref_code, url = self.send_request()
        xml_bytes = self.get_statement(ref_code, url)
        return parse_flex_xml(xml_bytes)
