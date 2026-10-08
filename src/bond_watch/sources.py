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


def _description(rows: list[dict]) -> dict:
    return {r.get("name"): r.get("value") for r in rows if r.get("name")}


def _field(row: dict, name: str):
    return row.get(name) if name in row else row.get(name.lower())


def resolve_isin(http: Http, isin: str, secid_hint: str | None = None) -> dict:
    lookup = secid_hint
    if not lookup:
        search = http.get("https://iss.moex.com/iss/securities.json", params={"q": isin, "iss.meta": "off"}).json()
        candidate = next((row for row in _rows(search, "securities") if _field(row, "ISIN") == isin), None)
        if not candidate:
            raise ValueError("ISS search returned no exact ISIN match")
        lookup = _field(candidate, "SECID")
    if not lookup:
        raise ValueError("ISS returned no SECID")
    response = http.get(f"https://iss.moex.com/iss/securities/{lookup}.json", params={"iss.meta": "off"})
    data = response.json()
    sec = _rows(data, "securities")
    desc = _description(_rows(data, "description"))
    exact = next((row for row in sec if _field(row, "ISIN") == isin), None)
    if not exact and desc.get("ISIN") == isin and sec:
        exact = sec[0]
    if not exact:
        raise ValueError("ISS detail returned no exact ISIN match")
    secid = _field(exact, "SECID") or desc.get("SECID") or lookup
    issuer = desc.get("ISSUERNAME") or _field(exact, "ISSUERNAME") or desc.get("EMITENT_TITLE")
    inn = desc.get("ISSUERINN") or desc.get("INN")
    issuer_id = desc.get("ISSUERID") or desc.get("EMITENT_ID")
    category = "ofz" if (str(desc.get("SECTYPE") or "").lower() == "ofz" or str(secid).upper().startswith("SU")) else "corporate"
    if category == "ofz" and not issuer:
        issuer = "Министерство финансов Российской Федерации"
    if not issuer:
        raise ValueError("ISS lacks a verified issuer name")
    return {
        "isin": isin,
        "secid": secid,
        "issuer_name": str(issuer),
        "inn": str(inn) if inn else None,
        "moex_id": str(issuer_id) if issuer_id else None,
        "category": category,
        "shortname": _field(exact, "SHORTNAME"),
        "issue_name": desc.get("NAME") or _field(exact, "NAME"),
        "issue_number": desc.get("REGNUMBER") or desc.get("REGNUM"),
        "bond_type": desc.get("SECTYPE") or _field(exact, "TYPE"),
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




