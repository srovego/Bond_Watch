from __future__ import annotations

import email.utils
import html
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from math import isfinite
from datetime import date, datetime, timezone
from urllib.parse import urlparse

from .http import Http

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Publication:
    source_id: str
    item_id: str
    url: str
    source_name: str
    published_at: str | None
    title: str
    body: str


def _rows(data: dict, key: str) -> list[dict]:
    block = data.get(key) or {}
    return [dict(zip(block.get("columns", []), values)) for values in block.get("data", [])]


def _description(block: dict) -> dict:
    columns = block.get("columns") or []
    if "name" not in columns or "value" not in columns:
        raise ValueError("ISS description lacks name/value columns")
    return {
        row["name"]: row.get("value")
        for row in _rows({"description": block}, "description")
        if row.get("name")
    }


def _field(row: dict, name: str):
    return row.get(name) if name in row else row.get(name.lower())


def resolve_isin(http: Http, isin: str, secid_hint: str | None = None) -> dict:
    # The search block contains the issuer and maps an ISIN to its SECID. For OFZ
    # these identifiers differ, so a saved hint must never replace the exact search.
    search = http.get("https://iss.moex.com/iss/securities.json", params={"q": isin, "iss.meta": "off"}).json()
    matches = [row for row in _rows(search, "securities") if _field(row, "ISIN") == isin]
    secids = {_field(row, "SECID") for row in matches}
    if len(secids) != 1 or None in secids:
        raise ValueError("ISS search returned no unique exact ISIN/SECID match")
    candidate = matches[0]
    secid = secids.pop()

    response = http.get(f"https://iss.moex.com/iss/securities/{secid}.json", params={"iss.meta": "off"})
    data = response.json()
    desc = _description(data.get("description") or {})
    # This endpoint returns description and boards, with no securities block.
    # Both identifiers must agree with the exact search result.
    if desc.get("ISIN") != isin:
        raise ValueError("ISS detail returned no exact ISIN match")
    if desc.get("SECID") != secid:
        raise ValueError("ISS detail SECID differs from exact search result")
    search_emitter_id = _field(candidate, "EMITENT_ID")
    detail_emitter_id = desc.get("EMITTER_ID")
    if search_emitter_id is not None and detail_emitter_id is not None and str(search_emitter_id) != str(detail_emitter_id):
        raise ValueError("ISS detail issuer ID differs from exact search result")

    issuer = _field(candidate, "EMITENT_TITLE") or desc.get("ISSUERNAME")
    inn = _field(candidate, "EMITENT_INN") or desc.get("ISSUERINN")
    issuer_id = search_emitter_id or detail_emitter_id
    instrument_type = desc.get("TYPE") or _field(candidate, "TYPE")
    category = "ofz" if instrument_type == "ofz_bond" or str(secid).upper().startswith("SU") else "corporate"
    if not issuer:
        raise ValueError("ISS exact search lacks a verified issuer name")
    return {
        "isin": isin,
        "secid": secid,
        "issuer_name": str(issuer),
        "inn": str(inn) if inn else None,
        "moex_id": str(issuer_id) if issuer_id is not None else None,
        "category": category,
        "shortname": desc.get("SHORTNAME") or _field(candidate, "SHORTNAME"),
        "issue_name": desc.get("ISSUENAME") or desc.get("NAME"),
        "issue_number": desc.get("REGNUMBER") or _field(candidate, "REGNUMBER"),
        "bond_type": desc.get("TYPENAME") or instrument_type,
        "card_url": f"https://www.moex.com/ru/issue.aspx?code={secid}",
        "nominal": _float(desc.get("FACEVALUE")),
    }

def _float(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _positive(value) -> float | None:
    number = _float(value)
    return number if number is not None and isfinite(number) and number > 0 else None


def _iso_date(value) -> str | None:
    if not value:
        return None
    try:
        raw = str(value)
        return (date.fromisoformat(raw) if len(raw) == 10 else datetime.fromisoformat(raw).date()).isoformat()
    except ValueError:
        return None


def fetch_bond_price(http: Http, secid: str, rejections: dict[str, int] | None = None) -> dict | None:
    row_rejections: dict[str, int] = {}

    def reject(reason: str) -> None:
        row_rejections[reason] = row_rejections.get(reason, 0) + 1

    def record_rejections() -> None:
        if rejections is not None:
            for reason, count in row_rejections.items():
                rejections[reason] = rejections.get(reason, 0) + count

    response = http.get(
        f"https://iss.moex.com/iss/engines/stock/markets/bonds/securities/{secid}.json",
        params={"iss.meta": "off"},
    )
    data = response.json()
    securities = {
        (row.get("SECID"), row.get("BOARDID")): row
        for row in _rows(data, "securities")
    }
    yields = {
        (row.get("SECID"), row.get("BOARDID")): row
        for row in _rows(data, "marketdata_yields")
    }
    versions = _rows(data, "dataversion")
    session_date = _iso_date(versions[0].get("trade_date")) if len(versions) == 1 else None
    supported_boards = {"TQCB", "TQOB", "TQIR", "TQOD"}
    market_rows = _rows(data, "marketdata")
    if not market_rows:
        reject("no_marketdata")
        record_rejections()
        return None
    for row in market_rows:
        board = row.get("BOARDID")
        if row.get("SECID") != secid:
            reject("secid_mismatch")
            continue
        if board not in supported_boards:
            reject("unsupported_board")
            continue
        security = securities.get((secid, board))
        if security is None:
            reject("missing_security_row")
            continue
        if _positive(row.get("NUMTRADES")) is None:
            reject("no_trades")
            continue
        price = _positive(row.get("LAST")) or _positive(row.get("LASTPRICE"))
        if price is None:
            reject("invalid_price")
            continue
        nominal = _positive(security.get("FACEVALUE"))
        if nominal is None:
            reject("invalid_nominal")
            continue
        raw_market_date = row.get("LASTTRADEDATE") or row.get("TRADEDATE")
        raw_trade_moment = yields.get((secid, board), {}).get("TRADEMOMENT")
        dated_values = [value for value in (raw_market_date, raw_trade_moment, versions[0].get("trade_date") if len(versions) == 1 else None) if value]
        dates = [_iso_date(value) for value in dated_values]
        if any(value is None for value in dates):
            reject("invalid_trade_date")
            continue
        if len(set(dates)) > 1:
            reject("conflicting_trade_date")
            continue
        trade_date = dates[0] if dates else session_date
        if not trade_date:
            reject("missing_trade_date")
            continue
        return {
            "clean_price": price,
            "previous_clean_price": _positive(security.get("PREVPRICE")),
            "previous_trade_date": _iso_date(security.get("PREVDATE")),
            "nominal": nominal,
            "trading_date": trade_date,
            "board": board,
        }
    record_rejections()
    return None


def _clean(value: str) -> str:
    return re.sub(r"<[^>]+>", " ", html.unescape(value or "")).strip()


def fetch_cbr_rss(http: Http, *, lookback_days: int = 3) -> list[Publication]:
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    result = []
    for feed in ("eventrss", "RssPress"):
        response = http.get(f"https://www.cbr.ru/rss/{feed}")
        root = ET.fromstring(response.content)
        for item in root.findall(".//item"):
            title = _clean(item.findtext("title") or "")
            url = (item.findtext("link") or "").strip()
            if urlparse(url).hostname not in {"cbr.ru", "www.cbr.ru"}:
                continue
            raw_date = item.findtext("pubDate")
            try:
                published = email.utils.parsedate_to_datetime(raw_date).astimezone(timezone.utc) if raw_date else None
            except (TypeError, ValueError):
                published = None
            if published and published < cutoff:
                continue
            guid = item.findtext("guid") or url
            result.append(Publication("cbr_rss", guid, url, "Банк России", published.isoformat() if published else None, title, _clean(item.findtext("description") or "")))
    return result




