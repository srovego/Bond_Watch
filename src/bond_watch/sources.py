from __future__ import annotations

import email.utils
import html
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
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


def fetch_bond_price(http: Http, secid: str) -> dict | None:
    response = http.get(
        f"https://iss.moex.com/iss/engines/stock/markets/bonds/securities/{secid}.json",
        params={"iss.meta": "off"},
    )
    data = response.json()
    rows = _rows(data, "marketdata")
    securities = _rows(data, "securities")
    valid = [row for row in rows if row.get("BOARDID") in {"TQCB", "TQOB", "TQIR", "TQOD"}]
    row = next((r for r in valid if _float(r.get("NUMTRADES")) and (_float(r.get("LAST")) or _float(r.get("LASTPRICE")))), None)
    security = next((s for s in securities if s.get("BOARDID") == row.get("BOARDID")), {}) if row else {}
    if not row:
        return None
    price = _float(row.get("LAST")) or _float(row.get("LASTPRICE"))
    previous_price = _float(row.get("PREVPRICE"))
    nominal = _float(security.get("FACEVALUE"))
    trade_date = row.get("LASTTRADEDATE") or row.get("TRADEDATE") or security.get("LASTTRADEDATE")
    if not price or not nominal or not trade_date:
        return None
    return {"clean_price": price, "previous_clean_price": previous_price, "nominal": nominal, "trading_date": str(trade_date), "board": row.get("BOARDID")}


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




