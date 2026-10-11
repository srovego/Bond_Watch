"""Public RSS feeds for corporate and security events."""
from __future__ import annotations

import email.utils
import hashlib
import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from .analysis import normalized
from .sources import Publication

CBR_SITE_URL = "https://www.cbr.ru/rss/RssNews"
MOEX_URLS = (
    "https://www.moex.com/export/news.aspx?cat=100",
    "https://www.moex.com/export/news.aspx?cat=104",
)

# Aliases are tied to the verified INNs in config/verified_bonds.yaml.
VERIFIED_ALIASES = {
    "7706664260": ("атомэнергопром",),
    "7707049388": ("ростелеком",),
    "7708591995": ("трансконтейнер",),
    "7714216826": ("аэрофьюэлз",),
    "7708503727": ("российские железные дороги", "оао ржд"),
    "7703104630": ("афк система",),
    "7750004150": ("вэб рф", "вэб.рф"),
}


def _clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", html.unescape(value or ""))).strip()


def _contains(text: str, phrase: str) -> bool:
    phrase = normalized(phrase)
    return bool(phrase and re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", text))


def parse_rss(payload: bytes, *, source_id: str, source_name: str, allowed_hosts: set[str],
              lookback_days: int = 3, now: datetime | None = None) -> tuple[list[Publication], int]:
    root = ET.fromstring(payload)
    if root.tag.lower() != "rss" or root.find("channel") is None:
        raise ValueError("expected RSS channel")
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=lookback_days)
    result: list[Publication] = []
    rejected = 0
    for element in root.findall("./channel/item"):
        title = _clean(element.findtext("title"))
        url = (element.findtext("link") or "").strip()
        raw_date = element.findtext("pubDate")
        if not title or urlparse(url).hostname not in allowed_hosts or not raw_date:
            rejected += 1
            continue
        try:
            published = email.utils.parsedate_to_datetime(raw_date)
            if published.tzinfo is None:
                raise ValueError("RSS date has no timezone")
            published = published.astimezone(timezone.utc)
        except (TypeError, ValueError):
            rejected += 1
            continue
        if published < cutoff:
            continue
        guid = (element.findtext("guid") or url).strip()
        # RssNews uses a stable GUID for a page that may be updated repeatedly.
        identity = guid + "|" + published.isoformat() + "|" + title if source_id == "cbr_site_rss" else guid
        item_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        result.append(Publication(source_id, item_id, url, source_name, published.isoformat(),
                                  title[:500], _clean(element.findtext("description"))[:800]))
    return result, rejected


def fetch_cbr_site_rss(http, *, lookback_days: int = 3) -> tuple[list[Publication], int]:
    response = http.get(CBR_SITE_URL)
    return parse_rss(response.content, source_id="cbr_site_rss", source_name="Банк России: новое на сайте",
                     allowed_hosts={"www.cbr.ru", "cbr.ru"}, lookback_days=lookback_days)


def fetch_moex_news(http, *, lookback_days: int = 3) -> tuple[list[Publication], int]:
    items: dict[str, Publication] = {}
    rejected = 0
    failures = []
    for url in MOEX_URLS:
        try:
            response = http.get(url)
            parsed, bad = parse_rss(response.content, source_id="moex_news", source_name="Московская биржа: новости",
                                    allowed_hosts={"www.moex.com", "moex.com"}, lookback_days=lookback_days)
            rejected += bad
            for item in parsed:
                items.setdefault(item.url, item)
        except Exception as exc:
            failures.append(f"{url}: {type(exc).__name__}: {exc}")
    if len(failures) == len(MOEX_URLS):
        raise RuntimeError("; ".join(failures)[:500])
    return list(items.values()), rejected + len(failures)


def match_portfolio(item: Publication, issuers: list[dict]) -> tuple[int | None, list[str], str, str]:
    text = normalized(item.title + " " + item.body)
    raw = (item.title + " " + item.body).upper()
    corporate = [issuer for issuer in issuers if issuer.get("category") == "corporate"]
    isin_hits: dict[int, list[str]] = {}
    inn_hits: set[int] = set()
    for issuer in corporate:
        for isin in issuer.get("isins", []):
            if re.search(r"(?<![A-Z0-9])" + re.escape(isin) + r"(?![A-Z0-9])", raw):
                isin_hits.setdefault(issuer["id"], []).append(isin)
        inn = issuer.get("inn")
        if inn and re.search(r"(?<!\d)" + re.escape(inn) + r"(?!\d)", raw):
            inn_hits.add(issuer["id"])
    strong = set(isin_hits) | inn_hits
    name_hits = {issuer["id"] for issuer in corporate if _contains(text, issuer["name"]) or
                 any(_contains(text, alias) for alias in VERIFIED_ALIASES.get(issuer.get("inn"), ()))}
    if len(strong | name_hits) > 1:
        return None, [], "ambiguous", "conflicting_identifiers"
    if isin_hits:
        issuer_id = next(iter(isin_hits))
        return issuer_id, isin_hits[issuer_id], "issue", "isin"
    if inn_hits:
        return next(iter(inn_hits)), [], "issuer", "inn"
    if name_hits:
        return next(iter(name_hits)), [], "issuer", "name"
    return None, [], "market", "no_portfolio_match"


def is_market_event(item: Publication) -> bool:
    """Only broad exchange notices can enter the portfolio news table without an issuer."""
    title = normalized(item.title)
    return item.source_id == "moex_news" and bool(re.search(
        r"(все|всех|всеми|рынк|секци|режим).{0,35}(облигац|торг)|"
        r"(облигац|торг).{0,35}(все|всех|всеми|рынк|секци|режим)", title
    )) and classify_event(item)[0] in {"trading_restriction", "listing"}


def classify_event(item: Publication) -> tuple[str, str]:
    title = normalized(item.title)
    if "техническ" in title and "дефолт" in title:
        return "technical_default", "CRITICAL"
    if "дефолт" in title:
        return "default", "CRITICAL"
    if re.search(r"(приостанов|прекращ|огранич|возобнов).{0,40}торг|торг.{0,40}(приостанов|прекращ|огранич|возобнов)", title):
        return "trading_restriction", "WARNING"
    if "листинг" in title or ("включен" in title or "исключен" in title) and "список" in title:
        return "listing", "INFO"
    if "облигац" in title and re.search(r"размещен|погашен|услови|выплат|купон|регистрац", title):
        return "bond_terms", "INFO"
    return "issuer_news", "INFO"
