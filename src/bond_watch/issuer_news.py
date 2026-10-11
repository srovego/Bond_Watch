"""Issuer-centred matching and conservative credit-event rules."""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

from .analysis import normalized
from .db import utcnow
from .news_sources import VERIFIED_ALIASES
from .sources import Publication, resolve_isin

ISIN_PATTERN = re.compile(r"(?<![A-Z0-9])(RU[A-Z0-9]{10})(?![A-Z0-9])")
CACHE_DAYS = 7
NEGATIVE_CACHE_HOURS = 1
ISS_SOURCE = "https://iss.moex.com/iss/securities/{isin}.json?iss.meta=off"


@dataclass(frozen=True)
class Match:
    issuer_id: int | None
    relation: str | None
    mentioned_isins: tuple[str, ...]
    portfolio_isins: tuple[str, ...]
    confirmation: str | None
    reason: str


def _contains(text: str, phrase: str) -> bool:
    phrase = normalized(phrase)
    return bool(phrase and re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", text))


def _contains_alias(raw: str, alias: str) -> bool:
    text = unicodedata.normalize("NFKC", raw).casefold().replace("ё", "е")
    phrase = unicodedata.normalize("NFKC", alias).casefold().replace("ё", "е")
    return bool(re.search(r"(?<![\w-])" + re.escape(phrase) + r"(?![\w-])", text))


def sync_related_companies(conn, root: Path) -> None:
    """Load only explicitly evidenced legal-entity links; an empty file is valid."""
    path = root / "config/related_companies.yaml"
    if not path.exists():
        return
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = data.get("companies") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise ValueError("related_companies.yaml needs a companies list")
    with conn:
        conn.execute("UPDATE related_companies SET enabled=0")
    for entry in entries:
        if not isinstance(entry, dict) or not re.fullmatch(r"\d{10}|\d{12}", str(entry.get("inn", ""))):
            raise ValueError("related company requires exact INN")
        if not entry.get("name") or not entry.get("evidence_url") or not entry.get("parent_inn"):
            raise ValueError("related company requires name, parent_inn and evidence_url")
        parent = conn.execute("SELECT id FROM issuers WHERE inn=? AND status='verified'",
                              (str(entry["parent_inn"]),)).fetchone()
        if not parent:
            raise ValueError(f"unverified parent INN: {entry['parent_inn']}")
        aliases = entry.get("aliases", [])
        if not isinstance(aliases, list) or any(not isinstance(x, str) for x in aliases):
            raise ValueError("related company aliases must be a list of strings")
        with conn:
            conn.execute("""INSERT INTO related_companies(inn,name,parent_issuer_id,aliases_json,evidence_url,enabled)
                VALUES(?,?,?,?,?,?) ON CONFLICT(inn) DO UPDATE SET name=excluded.name,
                parent_issuer_id=excluded.parent_issuer_id,aliases_json=excluded.aliases_json,
                evidence_url=excluded.evidence_url,enabled=excluded.enabled""",
                (str(entry["inn"]), entry["name"], parent["id"], json.dumps(aliases, ensure_ascii=False),
                 entry["evidence_url"], int(entry.get("enabled", True))))


def _cached_iss_inn(conn, http, isin: str, budget: dict) -> tuple[str | None, str]:
    row = conn.execute("SELECT * FROM isin_issuer_cache WHERE isin=?", (isin,)).fetchone()
    now = datetime.now(timezone.utc)
    if row:
        age = now - datetime.fromisoformat(row["verified_at"])
        lifetime = timedelta(days=CACHE_DAYS) if row["status"] == "verified" else timedelta(hours=NEGATIVE_CACHE_HOURS)
        if age < lifetime:
            return (row["issuer_inn"] if row["status"] == "verified" else None,
                    row["source_url"] or "moex_iss:cached_unverified")
    if budget["used"] >= budget["limit"]:
        return None, "lookup_budget_exhausted"
    budget["used"] += 1
    try:
        detail = resolve_isin(http, isin)
        if detail["isin"] != isin or not detail["inn"]:
            raise ValueError("ISS lacks exact ISIN and issuer INN")
    except Exception as exc:
        with conn:
            conn.execute("""INSERT INTO isin_issuer_cache(isin,status,verified_at,error) VALUES(?,'unverified',?,?)
                ON CONFLICT(isin) DO UPDATE SET status='unverified',issuer_inn=NULL,
                verified_at=excluded.verified_at,source_url=NULL,error=excluded.error""",
                (isin, utcnow(), f"{type(exc).__name__}: {exc}"[:300]))
        return None, "moex_iss_unverified"
    source = ISS_SOURCE.format(isin=detail["secid"])
    with conn:
        conn.execute("""INSERT INTO isin_issuer_cache(isin,issuer_inn,moex_id,status,verified_at,source_url)
            VALUES(?,?,?,'verified',?,?) ON CONFLICT(isin) DO UPDATE SET issuer_inn=excluded.issuer_inn,
            moex_id=excluded.moex_id,status='verified',verified_at=excluded.verified_at,
            source_url=excluded.source_url,error=NULL""",
            (isin, detail["inn"], detail["moex_id"], utcnow(), source))
    return detail["inn"], source


def match_issuer_news(conn, item: Publication, issuers: list[dict], http, budget: dict) -> Match:
    """An external ISIN must be checked even when the title also names an issuer."""
    raw = item.title + " " + item.body
    text = normalized(raw)
    mentioned = tuple(dict.fromkeys(ISIN_PATTERN.findall(raw.upper())))
    tracked = [i for i in issuers if i.get("inn")]
    by_inn = {i["inn"]: i for i in tracked}
    portfolio = {isin: i for i in tracked for isin in i["isins"]}
    direct: set[int] = set()
    for issuer in tracked:
        if re.search(r"(?<!\d)" + re.escape(issuer["inn"]) + r"(?!\d)", raw):
            direct.add(issuer["id"])
        if _contains(text, issuer["name"]) or any(
            _contains_alias(raw, alias) for alias in VERIFIED_ALIASES.get(issuer["inn"], ())
        ):
            direct.add(issuer["id"])
    companies = conn.execute("SELECT * FROM related_companies WHERE enabled=1").fetchall()
    linked: set[int] = set()
    company_evidence: dict[int, str] = {}
    company_by_inn = {row["inn"]: row for row in companies}
    for company in companies:
        aliases = json.loads(company["aliases_json"])
        if (re.search(r"(?<!\d)" + re.escape(company["inn"]) + r"(?!\d)", raw)
                or _contains(text, company["name"])
                or any(_contains_alias(raw, alias) for alias in aliases)):
            linked.add(company["parent_issuer_id"])
            company_evidence[company["parent_issuer_id"]] = company["evidence_url"]
    bond_issuers: set[int] = set()
    bond_companies: set[int] = set()
    confirmations: list[str] = []
    for isin in mentioned:
        if isin in portfolio:
            bond_issuers.add(portfolio[isin]["id"])
            confirmations.append("portfolio:verified_bonds.yaml")
            continue
        inn, source = _cached_iss_inn(conn, http, isin, budget)
        if inn is None:
            return Match(None, None, mentioned, (), None, source)
        confirmations.append(source)
        if inn in by_inn:
            bond_issuers.add(by_inn[inn]["id"])
        elif inn in company_by_inn:
            bond_companies.add(company_by_inn[inn]["parent_issuer_id"])
            company_evidence[company_by_inn[inn]["parent_issuer_id"]] = company_by_inn[inn]["evidence_url"]
        else:
            return Match(None, None, mentioned, (), None, "external_isin_other_issuer")
    all_ids = direct | linked | bond_issuers | bond_companies
    if len(all_ids) > 1:
        return Match(None, None, mentioned, (), None, "ambiguous_issuer")
    if not all_ids:
        return Match(None, None, mentioned, (), None, "no_issuer_match")
    issuer_id = next(iter(all_ids))
    issuer = next(i for i in tracked if i["id"] == issuer_id)
    if linked or bond_companies:
        relation = "related_company"
        source = "; ".join(dict.fromkeys(confirmations + [company_evidence[issuer_id]]))
    elif mentioned:
        relation = "related_bond"
        source = "; ".join(dict.fromkeys(confirmations))
    else:
        relation = "issuer"
        source = "verified_issuer_inn" if re.search(r"(?<!\d)" + re.escape(issuer["inn"]) + r"(?!\d)", raw) else "verified_issuer_name"
    return Match(issuer_id, relation, mentioned, tuple(issuer["isins"]), source, "matched")


def event_date_from_title(title: str) -> str | None:
    found = re.match(r"^\s*(\d{2})[.]([0-1]\d)[.](20\d{2})", title)
    if not found:
        return None
    try:
        return date(int(found[3]), int(found[2]), int(found[1])).isoformat()
    except ValueError:
        return None


def classify_credit_event(item: Publication, match: Match) -> tuple[str | None, str | None, str]:
    """Return category, severity and the explicit rule used; None means omit from news."""
    text = normalized(item.title + " " + item.body)
    technical = re.search(
        r"ценов.{0,15}коридор|риск.{0,15}параметр|диапазон оценки рыночных рисков|"
        r"режим.{0,12}торг|параметр.{0,12}торг|шаг цены|лотност|техническ.{0,25}характеристик", text)
    negated = re.search(r"\bне (?:допустил[аои]?|объявил[аои]?) (?:технический )?дефолт", text)
    default = not negated and re.search(
        r"(?:допустил[аои]?|объявил[аои]?|зафиксирован|наступил) (?:техническ\w* )?дефолт|"
        r"(?:не исполнил[а]?|не выплатил[а]?|просрочил[а]?) .{0,80}(?:купон|облигац|погашен|долгов)|"
        r"(?:признан[ао]?|признал суд) .{0,50}банкрот|"
        r"(?:введен[оа]?|открыт[оа]?) .{0,30}конкурсн\w* производств", text)
    if default:
        category, severity, basis = "confirmed_default", "CRITICAL", "explicit_nonpayment_or_insolvency_statement"
    elif technical:
        return None, None, "technical_market_notice"
    elif re.search(r"(?:понизил\w*|снизил\w*) .{0,45}(?:кредитн\w* )?рейтинг|негативн\w* прогноз|"
                   r"прогноз .{0,25}(?:негативн|отрицательн)|снижени\w* .{0,30}рейтинг", text):
        category, severity, basis = "rating", "WARNING", "rating_downgrade_or_negative_outlook"
    elif re.search(r"ухудшени\w* .{0,35}ликвидност|рост .{0,25}долгов\w* нагрузк|"
                   r"нехватк\w* .{0,25}ликвидност", text):
        category, severity, basis = "debt_liquidity", "WARNING", "explicit_financial_deterioration"
    elif re.search(r"(?:иск|дело|заявлени\w*) .{0,35}банкротств|крупн\w* судебн\w* спор", text):
        category, severity, basis = "litigation", "WARNING", "material_legal_proceeding"
    elif re.search(r"финансов\w* результат|бухгалтерск\w* отчетност|финансов\w* отчетност|"
                   r"мсфо|рсбу|годов\w* отчет", text):
        category, severity, basis = "financial_results", "INFO", "reported_financial_information"
    elif re.search(r"кредитн\w* рейтинг|рейтингов\w* прогноз|подтвердил\w* рейтинг", text):
        category, severity, basis = "rating", "INFO", "rating_action_without_downgrade"
    elif re.search(r"долгов\w* нагрузк|ликвидност|заемн\w* средств|обслуживани\w* долг", text):
        category, severity, basis = "debt_liquidity", "INFO", "debt_or_liquidity_information"
    elif re.search(r"смен\w* .{0,30}(?:акционер|собственник)|структур\w* собственност|"
                   r"контрольн\w* пакет", text):
        category, severity, basis = "ownership", "INFO", "ownership_change"
    elif re.search(r"гаранти\w*|поручительств|реструктуризац\w* долг|"
                   r"услови\w* .{0,25}облигац|погашени\w* .{0,25}облигац", text):
        category, severity, basis = "debt_obligations", "INFO", "debt_obligation_event"
    else:
        return None, None, "not_credit_relevant"
    if match.relation == "related_company" and severity in {"WARNING", "CRITICAL"}:
        return category, "INFO", basis + ";parent_exposure_not_confirmed"
    return category, severity, basis
