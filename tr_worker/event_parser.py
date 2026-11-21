"""Event parser for Trade Republic timeline events and portfolio valuations."""

from __future__ import annotations

import contextlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any

from config.settings import Settings
from shared.budgetbakers import (
    AmountPayload,
    CreateRecordPayload,
    TransferPayload,
    format_stock_buy_note,
)
from shared.couchdb import LivePositionInput, StockTradeInput


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


_ISIN_RE = re.compile(r"\b([A-Z]{2}[A-Z0-9]{10})\b")
_MCC_RE = re.compile(r"\b(\d{4})\b")
_MERCHANT_FALLBACK_ICON_RE = re.compile(
    r"merchant[-_]fallback[-_]([a-z0-9_-]+)",
    re.IGNORECASE,
)


def _collect_all_sections(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect top-level and nested timelineDetailV2 section dicts from a TR event."""
    details: dict[str, Any] = event.get("details") or {}
    sections = list(event.get("sections") or details.get("sections") or [])
    all_sections: list[dict[str, Any]] = []
    for sec in sections:
        if isinstance(sec, dict):
            all_sections.append(sec)
            data_items = sec.get("data")
            if not isinstance(data_items, list):
                continue
            for item in data_items:
                if isinstance(item, dict):
                    nested = (
                        (item.get("detail") or {})
                        .get("action", {})
                        .get("payload", {})
                        .get("sections", [])
                    )
                    for nsec in nested:
                        if isinstance(nsec, dict):
                            all_sections.append(nsec)
    return all_sections


def _icon_to_str(icon_val: Any) -> str:
    if isinstance(icon_val, dict):
        return str(icon_val.get("asset") or "")
    return str(icon_val or "")


def _extract_card_category_hints(event: dict[str, Any]) -> tuple[str | None, str | None]:
    """Extract (mcc, tr_hint) from top-level fields, fallback icon URIs, and timelineDetailV2."""
    details: dict[str, Any] = event.get("details") or {}
    mcc: str | None = None
    tr_hint: str | None = None

    for raw_mcc in (event.get("mcc"), details.get("mcc")):
        if raw_mcc is not None:
            m = _MCC_RE.search(str(raw_mcc))
            if m:
                mcc = m.group(1)
                break

    for raw_cat in (event.get("category"), details.get("category")):
        if isinstance(raw_cat, str) and raw_cat.strip():
            tr_hint = raw_cat.strip()
            break

    icon_candidates = [_icon_to_str(event.get("icon"))]
    all_sections = _collect_all_sections(event)
    for section in all_sections:
        if section.get("type") == "header":
            data_obj = section.get("data")
            if isinstance(data_obj, dict):
                icon_candidates.append(_icon_to_str(data_obj.get("icon")))
        data_list = section.get("data")
        if not isinstance(data_list, list):
            continue
        for item in data_list:
            if not isinstance(item, dict):
                continue
            label = str(item.get("title") or "").strip().lower()
            detail_obj: dict[str, Any] = item.get("detail") or {}
            detail_text = str(detail_obj.get("text") or "").strip()
            if not detail_text:
                continue
            if mcc is None and ("mcc" in label or "merchant category code" in label):
                m = _MCC_RE.search(detail_text)
                if m:
                    mcc = m.group(1)
            elif tr_hint is None and any(
                k in label for k in ("category", "kategorie", "kategorija")
            ):
                tr_hint = detail_text

    if tr_hint is None:
        for icon_str in icon_candidates:
            m_icon = _MERCHANT_FALLBACK_ICON_RE.search(icon_str)
            if m_icon:
                tr_hint = m_icon.group(1)
                break

    return mcc, tr_hint


def _parse_localized_decimal(text: str) -> Decimal | None:
    """Extract a positive Decimal from a localized string like '0,1524' or '328,10 €'."""
    cleaned = re.sub(r"[^\d,.-]", "", text).strip()
    if not cleaned:
        return None
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        val = Decimal(cleaned)
        return abs(val) if val != Decimal("0") else None
    except Exception:
        return None


def _extract_trade_details(event: dict[str, Any]) -> tuple[str, str, Decimal, Decimal]:
    """Extract (symbol, isin, shares, price) from a Trade Republic event object."""
    details: dict[str, Any] = event.get("details") or {}
    title = str(event.get("title") or "ETF").strip()
    isin = str(details.get("isin") or event.get("isin") or "UNKNOWN").strip()
    symbol = str(details.get("symbol") or title).strip()
    shares = Decimal(str(details.get("shares") or event.get("shares") or "0"))
    price = Decimal(str(details.get("price") or event.get("price") or "0"))

    # 1. Extract ISIN from icon path (e.g. "logos/IE00B3WJKG14/v2") if not yet set
    if isin == "UNKNOWN":
        icon_str = str(event.get("icon") or "")
        m = _ISIN_RE.search(icon_str.replace("/", " "))
        if m:
            isin = m.group(1)

    # 2. Walk top-level and nested timelineDetailV2 sections
    all_sections = _collect_all_sections(event)

    for section in all_sections:
        action = section.get("action")
        if isinstance(action, dict) and action.get("type") == "instrumentDetail":
            payload_isin = str(action.get("payload") or "").strip()
            if _ISIN_RE.fullmatch(payload_isin):
                isin = payload_isin
        if section.get("type") == "header" and isin == "UNKNOWN":
            data_obj = section.get("data")
            if isinstance(data_obj, dict):
                icon_val = data_obj.get("icon")
                icon_s = (
                    str(icon_val.get("asset") or "")
                    if isinstance(icon_val, dict)
                    else str(icon_val or "")
                )
                m = _ISIN_RE.search(icon_s.replace("/", " "))
                if m:
                    isin = m.group(1)

        data_list = section.get("data")
        if not isinstance(data_list, list):
            continue
        for item in data_list:
            if not isinstance(item, dict):
                continue
            label = str(item.get("title") or "").lower()
            detail_obj: dict[str, Any] = item.get("detail") or {}
            detail_text = str(detail_obj.get("text") or "").strip()
            prefix_text = str((detail_obj.get("displayValue") or {}).get("prefix") or "").strip()

            if "isin" in label and detail_text:
                isin = detail_text
            elif any(k in label for k in ("shares", "aktien", "anteile")) and detail_text:
                parsed_sh = _parse_localized_decimal(detail_text)
                if parsed_sh is not None and shares == Decimal("0"):
                    shares = parsed_sh
            elif any(k in label for k in ("share price", "quotation", "kurs")) and detail_text:
                parsed_pr = _parse_localized_decimal(detail_text)
                if parsed_pr is not None and price == Decimal("0"):
                    price = parsed_pr
            elif any(k in label for k in ("transaktion", "transaction")) and prefix_text:
                parsed_sh = _parse_localized_decimal(prefix_text)
                if parsed_sh is not None and shares == Decimal("0"):
                    shares = parsed_sh

    # 3. Also consult pytr.event.Event.from_dict if shares or isin are still missing
    if (isin == "UNKNOWN" or shares == Decimal("0")) and details.get("sections"):
        with contextlib.suppress(Exception):
            from pytr.event import Event

            ev = Event.from_dict(event)
            if ev.isin and isin == "UNKNOWN":
                isin = str(ev.isin)
            if ev.shares and shares == Decimal("0"):
                shares = Decimal(str(ev.shares))

    amount_obj: dict[str, Any] = event.get("amount") or {}
    raw_amount = abs(Decimal(str(amount_obj.get("value") or "0")))
    if shares > Decimal("0") and price == Decimal("0") and raw_amount > Decimal("0"):
        price = (raw_amount / shares).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    elif price > Decimal("0") and shares == Decimal("0") and raw_amount > Decimal("0"):
        shares = (raw_amount / price).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

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
            if settings.has_couchdb:
                # Synced as a native -AssetTransaction stock purchase in CouchDB
                continue
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
            if settings.has_couchdb:
                # 3a. Create paired Transfer TR Cash -> TR Wealth (stock buy synced via CouchDB)
                transfer_note = f"Savings plan: {title}"[:255] if title else buy_note
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
                            category_id=None,
                            record_date=iso_ts,
                            counter_party="Trade Republic",
                            note=transfer_note,
                            transfer=TransferPayload(
                                pairing_mode="new",
                                account_id=settings.bb_tr_wealth_account_id,
                            ),
                        ),
                    )
                )
                continue

            # Legacy/REST-only mode:
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
            mcc, tr_hint = _extract_card_category_hints(event)
            category_id = settings.resolve_merchant_category(
                title,
                mcc=mcc,
                tr_hint=tr_hint,
            )
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


def parse_tr_stock_trades(events: Sequence[dict[str, Any]]) -> list[StockTradeInput]:
    """Extract stock buy and 1% Saveback transactions from Trade Republic timeline events."""
    trades: list[StockTradeInput] = []
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
        raw_amount = Decimal(str(amount_dict["value"]))
        if raw_amount == Decimal("0"):
            continue
        currency = str(amount_dict.get("currency") or "EUR")
        iso_ts = _format_iso_utc(_parse_iso_datetime(raw_ts))

        event_type = str(event.get("eventType") or "").lower()
        title = str(event.get("title") or "").strip().lower()
        subtitle = str(event.get("subtitle") or "").strip().lower()

        is_saveback = "saveback" in event_type or "saveback" in title or "saveback" in subtitle
        is_savings_or_buy = any(
            kw in event_type or kw in subtitle
            for kw in ("savings_plan", "savings plan", "sparplan", "order_executed", "buy order")
        )
        if not (is_saveback or is_savings_or_buy):
            continue

        symbol, isin, shares, price = _extract_trade_details(event)
        if shares <= Decimal("0") or isin == "UNKNOWN":
            continue

        ext_id = f"{event_id}:saveback" if is_saveback else event_id
        trades.append(
            StockTradeInput(
                external_id=ext_id,
                timestamp=iso_ts,
                symbol=symbol,
                isin=isin,
                quantity=shares,
                price=Decimal("0") if is_saveback else price,
                price_currency=currency,
                cash_amount_eur=Decimal("0") if is_saveback else abs(raw_amount),
                side="BUY",
            )
        )
    return trades


def parse_tr_live_positions(portfolio_payload: dict[str, Any]) -> list[LivePositionInput]:
    """Extract live positions from Trade Republic portfolio payload for CouchDB reconciliation."""
    positions: list[LivePositionInput] = []
    for pos in portfolio_payload.get("positions", []):
        if not isinstance(pos, dict):
            continue
        isin = str(pos.get("instrumentId") or pos.get("isin") or "").strip()
        symbol = str(pos.get("name") or pos.get("symbol") or isin).strip()
        qty = Decimal(str(pos.get("netSize") or "0"))
        price = Decimal(str(pos.get("currentPrice") or pos.get("averageBuyIn") or "0"))
        if isin and qty > Decimal("0"):
            positions.append(
                LivePositionInput(
                    symbol=symbol,
                    isin=isin,
                    quantity=qty,
                    unit_price=price,
                    unit_price_currency="EUR",
                    unit_price_eur=price,
                )
            )
    return positions
