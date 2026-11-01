"""Event parser for Trade Republic timeline events and portfolio valuations."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from config.settings import Settings
from shared.budgetbakers import (
    AmountPayload,
    CreateRecordPayload,
    format_stock_buy_note,
)


class TRRecordKind(StrEnum):
    """Classification of parsed Trade Republic timeline records."""

    CARD_EXPENSE = "card_expense"
    INTEREST = "interest"
    SAVEBACK = "saveback"
    SAVINGS_PLAN_CASH = "savings_plan_cash"
    SAVINGS_PLAN_BUY = "savings_plan_buy"
    DIVIDEND = "dividend"
    TRANSFER = "transfer"


@dataclass(frozen=True, slots=True)
class ParsedTRRecord:
    """Normalized Trade Republic record mapped to a target BudgetBakers account."""

    external_id: str
    kind: TRRecordKind
    timestamp: str
    amount: float
    currency: str
    payload: CreateRecordPayload


def _parse_iso_datetime(value: str) -> datetime:
    cleaned = value.strip()
    if cleaned.endswith("Z"):
        cleaned = f"{cleaned[:-1]}+00:00"
    dt = datetime.fromisoformat(cleaned)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _format_iso_utc(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _extract_trade_details(event: dict[str, Any]) -> tuple[str, str, Decimal, Decimal]:
    """Extract (symbol, isin, shares, price) from a Trade Republic event object."""
    details: dict[str, Any] = event.get("details") or {}
    title = str(event.get("title") or "ETF").strip()
    isin = str(details.get("isin") or event.get("isin") or "UNKNOWN").strip()
    symbol = str(details.get("symbol") or title).strip()
    shares = Decimal(str(details.get("shares") or event.get("shares") or "0"))
    price = Decimal(str(details.get("price") or event.get("price") or "0"))

    # Also support pytr detail sections if present
    for section in event.get("sections", []):
        if not isinstance(section, dict):
            continue
        for item in section.get("data", []):
            if not isinstance(item, dict):
                continue
            label = str(item.get("title") or "").lower()
            detail_text = str((item.get("detail") or {}).get("text") or "").strip()
            if "isin" in label and detail_text:
                isin = detail_text
            elif "shares" in label and detail_text:
                num = detail_text.split()[0].replace(",", ".")
                shares = Decimal(num)
            elif "share price" in label and detail_text:
                num = detail_text.split()[0].replace(",", ".")
                price = Decimal(num)

    amount_obj: dict[str, Any] = event.get("amount") or {}
    raw_amount = abs(Decimal(str(amount_obj.get("value") or "0")))
    if shares > Decimal("0") and price == Decimal("0") and raw_amount > Decimal("0"):
        price = (raw_amount / shares).quantize(Decimal("0.01"))
    elif price > Decimal("0") and shares == Decimal("0") and raw_amount > Decimal("0"):
        shares = (raw_amount / price).quantize(Decimal("0.0001"))

    return symbol, isin, shares, price


def parse_tr_timeline_events(
    events: Sequence[dict[str, Any]],
    settings: Settings,
) -> list[ParsedTRRecord]:
    """Parse Trade Republic timeline JSON events into BudgetBakers record payloads."""
    parsed: list[ParsedTRRecord] = []

    for event in events:
        status = str(event.get("status") or "EXECUTED").upper()
        if status in ("CANCELED", "CANCELLED", "FAILED", "REJECTED", "PENDING"):
            continue

        event_id = str(event.get("id") or "").strip()
        raw_ts = str(event.get("timestamp") or "").strip()
        if not event_id or not raw_ts:
            continue

        amount_dict: dict[str, Any] = event.get("amount") or {}
        if "value" not in amount_dict or amount_dict["value"] is None:
            continue
        raw_amount = float(amount_dict["value"])
        if raw_amount == 0.0:
            continue
        currency = str(amount_dict.get("currency") or "EUR")

        event_dt = _parse_iso_datetime(raw_ts)
        iso_ts = _format_iso_utc(event_dt)

        event_type = str(event.get("eventType") or "").lower()
        title = str(event.get("title") or "").strip()
        subtitle = str(event.get("subtitle") or "").strip()
        subtitle_lower = subtitle.lower()
        title_lower = title.lower()

        # 1. 1% Saveback bonus income -> Trade republic wealth
        if "saveback" in event_type or "saveback" in title_lower or "saveback" in subtitle_lower:
            symbol, isin, shares, price = _extract_trade_details(event)
            positive_amount = round(abs(raw_amount), 2)
            if shares > 0 and isin != "UNKNOWN":
                note = format_stock_buy_note(
                    symbol=symbol,
                    isin=isin,
                    shares=shares,
                    price=price,
                    currency=currency,
                    side="SAVEBACK",
                )
            else:
                note = "Trade Republic 1% Saveback bonus"
            parsed.append(
                ParsedTRRecord(
                    external_id=f"{event_id}:saveback",
                    kind=TRRecordKind.SAVEBACK,
                    timestamp=iso_ts,
                    amount=positive_amount,
                    currency=currency,
                    payload=CreateRecordPayload(
                        account_id=settings.bb_tr_wealth_account_id,
                        amount=AmountPayload(value=positive_amount, currency_code=currency),
                        category_id=settings.bb_category_interest_dividends,
                        record_date=iso_ts,
                        counter_party="Trade Republic",
                        note=note,
                    ),
                )
            )
            continue

        # 2. Monthly Interest -> Trade republic (cash)
        if "interest" in event_type or title_lower == "interest" or "interest" in subtitle_lower:
            positive_amount = round(abs(raw_amount), 2)
            parsed.append(
                ParsedTRRecord(
                    external_id=event_id,
                    kind=TRRecordKind.INTEREST,
                    timestamp=iso_ts,
                    amount=positive_amount,
                    currency=currency,
                    payload=CreateRecordPayload(
                        account_id=settings.bb_tr_cash_account_id,
                        amount=AmountPayload(value=positive_amount, currency_code=currency),
                        category_id=settings.bb_category_interest_dividends,
                        record_date=iso_ts,
                        counter_party="Trade Republic",
                        note="Interest",
                    ),
                )
            )
            continue

        # 3. Savings Plan & Stock Buys -> Cash deduction on TR Cash + Stock Buy on TR Wealth
        is_savings_or_buy = any(
            kw in event_type or kw in subtitle_lower
            for kw in ("savings_plan", "savings plan", "sparplan", "order_executed", "buy order")
        )
        if is_savings_or_buy:
            symbol, isin, shares, price = _extract_trade_details(event)
            expense_amount = -round(abs(raw_amount), 2)
            buy_note = format_stock_buy_note(
                symbol=symbol,
                isin=isin,
                shares=shares,
                price=price,
                currency=currency,
                side="BUY",
            )
            # 3a. Deduct cash from Trade Republic cash account
            parsed.append(
                ParsedTRRecord(
                    external_id=f"{event_id}:cash",
                    kind=TRRecordKind.SAVINGS_PLAN_CASH,
                    timestamp=iso_ts,
                    amount=expense_amount,
                    currency=currency,
                    payload=CreateRecordPayload(
                        account_id=settings.bb_tr_cash_account_id,
                        amount=AmountPayload(value=expense_amount, currency_code=currency),
                        category_id=settings.bb_category_investments,
                        record_date=iso_ts,
                        counter_party="Trade Republic",
                        note=buy_note,
                    ),
                )
            )
            # 3b. Log stock buy record on Trade Republic wealth account
            parsed.append(
                ParsedTRRecord(
                    external_id=f"{event_id}:wealth",
                    kind=TRRecordKind.SAVINGS_PLAN_BUY,
                    timestamp=iso_ts,
                    amount=expense_amount,
                    currency=currency,
                    payload=CreateRecordPayload(
                        account_id=settings.bb_tr_wealth_account_id,
                        amount=AmountPayload(value=expense_amount, currency_code=currency),
                        category_id=settings.bb_category_investments,
                        record_date=iso_ts,
                        counter_party="Trade Republic",
                        note=buy_note,
                    ),
                )
            )
            continue

        # 4. Dividends -> Trade republic wealth
        if "dividend" in event_type or "dividend" in subtitle_lower or event_type == "credit":
            positive_amount = round(abs(raw_amount), 2)
            symbol, isin, _, _ = _extract_trade_details(event)
            div_note = f"Dividend {symbol} ({isin})" if isin != "UNKNOWN" else f"Dividend {title}"
            parsed.append(
                ParsedTRRecord(
                    external_id=event_id,
                    kind=TRRecordKind.DIVIDEND,
                    timestamp=iso_ts,
                    amount=positive_amount,
                    currency=currency,
                    payload=CreateRecordPayload(
                        account_id=settings.bb_tr_wealth_account_id,
                        amount=AmountPayload(value=positive_amount, currency_code=currency),
                        category_id=settings.bb_category_interest_dividends,
                        record_date=iso_ts,
                        counter_party="Trade Republic",
                        note=div_note[:255],
                    ),
                )
            )
            continue

        # 5. SEPA Cash Transfers -> Trade republic (cash)
        if "transfer" in event_type or "sepa" in subtitle_lower or "deposit" in subtitle_lower:
            signed_amount = round(raw_amount, 2)
            parsed.append(
                ParsedTRRecord(
                    external_id=event_id,
                    kind=TRRecordKind.TRANSFER,
                    timestamp=iso_ts,
                    amount=signed_amount,
                    currency=currency,
                    payload=CreateRecordPayload(
                        account_id=settings.bb_tr_cash_account_id,
                        amount=AmountPayload(value=signed_amount, currency_code=currency),
                        category_id=settings.bb_category_transfer,
                        record_date=iso_ts,
                        counter_party=title or "SEPA Transfer",
                        note=f"{title} ({subtitle})".strip(" ()")[:255],
                    ),
                )
            )
            continue

        # 6. Card Spending -> Standard expenses on Trade republic (cash) if >= cutoff date
        if "card" in event_type or "card" in subtitle_lower or raw_amount < 0:
            if event_dt < settings.tr_card_cutoff_date:
                continue
            expense_amount = -round(abs(raw_amount), 2)
            mcc = str(event.get("mcc") or "").strip() or None
            category_id = settings.resolve_merchant_category(title, mcc=mcc)
            parsed.append(
                ParsedTRRecord(
                    external_id=event_id,
                    kind=TRRecordKind.CARD_EXPENSE,
                    timestamp=iso_ts,
                    amount=expense_amount,
                    currency=currency,
                    payload=CreateRecordPayload(
                        account_id=settings.bb_tr_cash_account_id,
                        amount=AmountPayload(value=expense_amount, currency_code=currency),
                        category_id=category_id,
                        record_date=iso_ts,
                        counter_party=title or "Card Merchant",
                        note=title or "Card payment",
                    ),
                )
            )

    return parsed


def parse_tr_portfolio_value(portfolio_payload: dict[str, Any]) -> Decimal:
    """Extract total live market value of the Trade Republic investment portfolio."""
    if "netValue" in portfolio_payload and portfolio_payload["netValue"] is not None:
        val = Decimal(str(portfolio_payload["netValue"]))
        if val > Decimal("0"):
            return val.quantize(Decimal("0.01"))

    total = Decimal("0.00")
    for pos in portfolio_payload.get("positions", []):
        if not isinstance(pos, dict):
            continue
        if pos.get("marketValue") is not None:
            total += Decimal(str(pos["marketValue"]))
        else:
            size = Decimal(str(pos.get("netSize") or "0"))
            price = Decimal(str(pos.get("currentPrice") or "0"))
            total += size * price
    return total.quantize(Decimal("0.01"))
