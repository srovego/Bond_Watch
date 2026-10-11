"""Official Bank of Russia decision RSS feeds. No private site endpoints."""
from __future__ import annotations

import email.utils
import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .analysis import normalized
from .news_sources import VERIFIED_ALIASES
from .sources import Publication

FEEDS = {
    "cbr_instruments": ("https://www.cbr.ru/rss/insideDKO", "insideDKO", "Банк России: решения по инструментам"),
    "cbr_participants": ("https://www.cbr.ru/rss/rbr", "rbr_fr", "Банк России: решения по участникам"),
}
# OGRNs verified on the issuers' official disclosure cards; other issuers
# still require the exact INN and subject-name association.
VERIFIED_OGRN = {
    "7708503727": "1037739877295",
    "7707049388": "1027700198767",
    "7703104630": "1027700003891",
}
GUID = re.compile(r"msg_(\d+)\Z")
INN = re.compile(r"(?<!\d)ИНН\s*[:№]?\s*(\d{10}|\d{12})(?!\d)", re.I)
OGRN = re.compile(r"(?<!\d)ОГРН\s*[:№]?\s*(\d{13}|\d{15})(?!\d)", re.I)


@dataclass(frozen=True)
class Decision:
    publication: Publication
    decision_type: str
    subject_inn: str | None
    subject_ogrn: str | None
    subject_name: str | None


def clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", html.unescape(value or ""))).strip()


def parse_decisions(payload: bytes, source_id: str, *, lookback_days: int = 3,
                    now: datetime | None = None) -> tuple[list[Decision], int]:
    root = ET.fromstring(payload)
    if root.tag.lower() != "rss" or root.find("channel") is None:
        raise ValueError("expected RSS channel")
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=lookback_days)
    _, path, source_name = FEEDS[source_id]
    decisions, invalid = [], 0
    for item in root.findall("./channel/item"):
        guid = (item.findtext("guid") or "").strip()
        title = clean(item.findtext("title"))
        body = clean(item.findtext("description"))
        category = clean(item.findtext("category"))
        try:
            match = GUID.fullmatch(guid)
            published = email.utils.parsedate_to_datetime(item.findtext("pubDate"))
            if not match or not title or not body or not category or published.tzinfo is None:
                raise ValueError("incomplete item")
            published = published.astimezone(timezone.utc)
        except (TypeError, ValueError):
            invalid += 1
            continue
        if published < cutoff:
            continue
        # Both public decision pages use /doc?id=<numeric part of RSS guid>.
        url = f"https://www.cbr.ru/rbr/{path}/doc?id={match.group(1)}"
        subject_text = re.sub(r"^\d{2}\.\d{2}\.\d{4}\s*", "", title)
        if source_id == "cbr_instruments":
            inns = INN.findall(subject_text)
            ogrns = OGRN.findall(subject_text)
            subject_inn = inns[0] if len(inns) == 1 else None
            subject_ogrn = ogrns[0] if len(ogrns) == 1 else None
            subject_name = re.split(r",\s*(?:ОГРН|ИНН)\b", subject_text, maxsplit=1, flags=re.I)[0].strip()
        else:
            inns = list(dict.fromkeys(INN.findall(body)))
            ogrns = list(dict.fromkeys(OGRN.findall(body)))
            # A multi-party decision cannot be attributed from bare numbers.
            subject_inn = inns[0] if len(inns) == 1 else None
            subject_ogrn = ogrns[0] if len(ogrns) == 1 else None
            subject_name = subject_text
        publication = Publication(source_id, guid, url, source_name, published.isoformat(),
                                  subject_text[:500], body[:800])
        decisions.append(Decision(publication, category, subject_inn, subject_ogrn, subject_name))
    return decisions, invalid


def fetch_decisions(http, source_id: str, *, lookback_days: int = 3) -> tuple[list[Decision], int]:
    return parse_decisions(http.get(FEEDS[source_id][0]).content, source_id, lookback_days=lookback_days)


def _has_name(text: str, issuer: dict) -> bool:
    names = (issuer["name"], *VERIFIED_ALIASES.get(issuer["inn"], ()))
    text = normalized(text)
    return any(len(normalized(name)) >= 7 and
               re.search(r"(?<!\w)" + re.escape(normalized(name)) + r"(?!\w)", text)
               for name in names)


def identify_issuer(decision: Decision, issuers: list[dict]) -> dict | None:
    if not decision.subject_inn:
        return None
    candidates = [i for i in issuers if i.get("category") == "corporate"
                  and i.get("inn") == decision.subject_inn]
    if len(candidates) != 1:
        return None
    issuer = candidates[0]
    expected_ogrn = VERIFIED_OGRN.get(issuer["inn"])
    if expected_ogrn and decision.subject_ogrn and decision.subject_ogrn != expected_ogrn:
        return None
    if decision.publication.source_id == "cbr_instruments":
        return issuer if _has_name(decision.subject_name or "", issuer) else None
    # Participant RSS may mention third parties. Require the same legal entity
    # in the headline and the text containing its sole INN.
    if not (_has_name(decision.subject_name or "", issuer)
            and _has_name(decision.publication.body, issuer)):
        return None
    return issuer


def classify_decision(decision: Decision) -> tuple[str, str, str]:
    text = normalized(decision.publication.title + " " + decision.publication.body)
    if re.search(r"обращени\w* в суд о признани\w* банкрот|"
                 r"отказ\w* в государственн\w* регистрац\w*.{0,100}облигац|"
                 r"приостанов\w*.{0,80}(?:выпуск|размещен)\w*.{0,40}облигац|"
                 r"аннулирован\w*.{0,60}выпуск\w*.{0,40}облигац", text):
        return "regulatory_decision", "WARNING", "explicit_adverse_bond_or_insolvency_decision"
    return "regulatory_decision", "INFO", "routine_or_unconfirmed_credit_impact"
