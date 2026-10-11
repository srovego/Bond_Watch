from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

from bond_watch.db import connect
from bond_watch.issuer_news import Match, classify_credit_event, event_date_from_title, match_issuer_news, sync_related_companies
from bond_watch.pipeline import _issuer_rows, init_sources, poll_corporate_rss, seed_verified_bonds, sync_portfolio
from bond_watch.sources import Publication

ROOT = Path(__file__).resolve().parents[1]
RELATED = "RU000A10D541"
PORTFOLIO_RZD = "RU000A10AZ60"
NOW = datetime(2026, 10, 11, 13, tzinfo=timezone.utc)


def issue_payload(inn="7708503727", isin=RELATED):
    fixture = json.loads((ROOT / "tests/fixtures/moex_corporate.json").read_text(encoding="utf-8"))
    search = fixture["search"]["securities"]
    row = search["data"][0]
    for key, value in {"secid": isin, "isin": isin, "emitent_inn": inn,
                       "emitent_title": "открытое акционерное общество Российские железные дороги"}.items():
        row[search["columns"].index(key)] = value
    detail = fixture["detail"]["description"]
    for row in detail["data"]:
        if row[detail["columns"].index("name")] in {"ISIN", "SECID"}:
            row[detail["columns"].index("value")] = isin
    return fixture


class Reply:
    def __init__(self, data=None, content=b""):
        self.data = data
        self.content = content

    def json(self):
        return self.data


class Http:
    def __init__(self, feed, inn="7708503727", isin=RELATED):
        self.feed = feed
        self.issue = issue_payload(inn, isin)
        self.iss_calls = 0

    def get(self, url, **kwargs):
        if "iss.moex.com" in url:
            self.iss_calls += 1
            return Reply(self.issue["search"] if "securities.json" in url else self.issue["detail"])
        return Reply(content=self.feed)


def feed(title, guid="item-1", description=""):
    return (f"<rss><channel><item><title>{escape(title)}</title>"
            f"<link>https://www.moex.com/n104954</link><guid>{guid}</guid>"
            f"<description>{escape(description)}</description>"
            "<pubDate>Fri, 09 Oct 2026 09:33:00 +0300</pubDate>"
            "</item></channel></rss>").encode("utf-8")


def setup(tmp_path):
    conn = connect(tmp_path / "news.sqlite")
    settings = {"sources": {"moex_news": True}, "news_lookback_days": 3,
                "related_isin_lookup_limit": 2}
    init_sources(conn, settings)
    sync_portfolio(conn, ROOT)
    seed_verified_bonds(conn, ROOT)
    return conn, settings


def test_material_issuer_news_without_portfolio_isin_is_saved(tmp_path, monkeypatch):
    conn, settings = setup(tmp_path)
    monkeypatch.setattr("bond_watch.news_sources.datetime", type("Fixed", (datetime,),
                        {"now": classmethod(lambda cls, tz=None: NOW)}))
    title = 'ОАО "Российские железные дороги" опубликовало финансовую отчетность по МСФО'
    poll_corporate_rss(conn, settings, Http(feed(title)), "moex_news")
    row = conn.execute("SELECT * FROM news").fetchone()
    assert row["relation_type"] == "issuer" and row["severity"] == "INFO"
    assert row["category"] == "financial_results"
    assert json.loads(row["portfolio_isins_json"]) == [PORTFOLIO_RZD]
    assert row["mentioned_isin"] is None
    assert conn.execute("SELECT count(*) FROM news_issuers WHERE news_id=?", (row["id"],)).fetchone()[0] == 1
    conn.close()


def test_external_bond_default_uses_exact_iss_and_cache(tmp_path, monkeypatch):
    conn, settings = setup(tmp_path)
    monkeypatch.setattr("bond_watch.news_sources.datetime", type("Fixed", (datetime,),
                        {"now": classmethod(lambda cls, tz=None: NOW)}))
    title = f'ОАО "Российские железные дороги" допустило дефолт по облигациям {RELATED}'
    http = Http(feed(title))
    poll_corporate_rss(conn, settings, http, "moex_news")
    poll_corporate_rss(conn, settings, http, "moex_news")
    row = conn.execute("SELECT * FROM news").fetchone()
    assert row["relation_type"] == "related_bond"
    assert row["mentioned_isin"] == RELATED
    assert row["severity"] == "CRITICAL" and row["category"] == "confirmed_default"
    assert row["confirmation_source"].startswith("https://iss.moex.com/")
    assert json.loads(row["portfolio_isins_json"]) == [PORTFOLIO_RZD]
    assert conn.execute("SELECT isin FROM news_bonds WHERE news_id=?", (row["id"],)).fetchone()[0] == PORTFOLIO_RZD
    assert conn.execute("SELECT count(*) FROM news").fetchone()[0] == 1
    assert http.iss_calls == 2
    counts = json.loads(conn.execute("SELECT rejection_counts_json FROM collector_runs WHERE source_id='moex_news' ORDER BY id DESC LIMIT 1").fetchone()[0])
    assert counts["related_bond"] == 1 and counts["already_known"] == 1
    conn.close()


def test_real_moex_corridor_notice_is_related_but_excluded(tmp_path, monkeypatch):
    conn, settings = setup(tmp_path)
    monkeypatch.setattr("bond_watch.news_sources.datetime", type("Fixed", (datetime,),
                        {"now": classmethod(lambda cls, tz=None: NOW)}))
    title = ("09.10.2026, 09-33 (мск) изменены значения верхней границы ценового коридора "
             f"и диапазона оценки рыночных рисков ценной бумаги {RELATED} (РЖД 1Р-47R).")
    poll_corporate_rss(conn, settings, Http(feed(title)), "moex_news")
    assert conn.execute("SELECT count(*) FROM news").fetchone()[0] == 0
    run = conn.execute("SELECT rejection_counts_json FROM collector_runs ORDER BY id DESC LIMIT 1").fetchone()
    counts = json.loads(run[0])
    assert counts["related_bond"] == 1 and counts["technical_excluded"] == 1
    check = conn.execute("SELECT decision FROM rss_item_checks").fetchone()[0]
    assert check == "technical_excluded:related_bond"
    assert conn.execute("SELECT issuer_inn,status FROM isin_issuer_cache WHERE isin=?", (RELATED,)).fetchone()[0] == "7708503727"
    conn.close()


def test_similar_company_and_wrong_iss_inn_are_rejected(tmp_path):
    conn, _ = setup(tmp_path)
    issuers = _issuer_rows(conn)
    budget = {"used": 0, "limit": 2}
    item = Publication("moex_news", "1", "https://www.moex.com/n1", "MOEX", NOW.isoformat(),
                       "АО РЖД Логистика опубликовало финансовую отчетность", "")
    assert match_issuer_news(conn, item, issuers, Http(b""), budget).reason == "no_issuer_match"
    misleading = Publication("moex_news", "2", item.url, "MOEX", NOW.isoformat(),
                             f'ОАО "Российские железные дороги" и выпуск {RELATED}: отчетность', "")
    wrong = match_issuer_news(conn, misleading, issuers, Http(b"", inn="1234567890"), budget)
    assert wrong.reason == "external_isin_other_issuer" and wrong.issuer_id is None
    similar = Publication("moex_news", "3", item.url, "MOEX", NOW.isoformat(),
                          "Ростелеком-Солар опубликовал финансовые результаты", "")
    assert match_issuer_news(conn, similar, issuers, Http(b""), budget).reason == "no_issuer_match"
    assert conn.execute("SELECT count(*) FROM news").fetchone()[0] == 0
    conn.close()


def test_unverified_isin_and_lookup_budget_cannot_use_name_fallback(tmp_path):
    conn, _ = setup(tmp_path)
    issuers = _issuer_rows(conn)
    item = Publication("moex_news", "1", "https://www.moex.com/n1", "MOEX", NOW.isoformat(),
                       f'ОАО "Российские железные дороги" выплатило купон {RELATED}', "")
    blocked = match_issuer_news(conn, item, issuers, Http(b""), {"used": 0, "limit": 0})
    assert blocked.reason == "lookup_budget_exhausted"
    bad = match_issuer_news(conn, item, issuers, Http(b"", isin="RU000A10D542"),
                            {"used": 0, "limit": 1})
    assert bad.reason == "moex_iss_unverified"
    conn.close()


def test_configured_subsidiary_is_linked_without_parent_credit_alarm(tmp_path, monkeypatch):
    conn, settings = setup(tmp_path)
    folder = tmp_path / "config"
    folder.mkdir()
    (folder / "related_companies.yaml").write_text(
        """companies:
  - inn: "7701234567"
    name: АО РЖД Логистика
    parent_inn: "7708503727"
    aliases: []
    evidence_url: https://company.rzd.ru/verified-link
    enabled: true
""", encoding="utf-8")
    sync_related_companies(conn, tmp_path)
    item = Publication("moex_news", "1", "https://www.moex.com/n1", "MOEX", NOW.isoformat(),
                       "АО РЖД Логистика допустило дефолт", "")
    match = match_issuer_news(conn, item, _issuer_rows(conn), Http(b""), {"used": 0, "limit": 1})
    assert match.relation == "related_company" and match.confirmation.endswith("verified-link")
    category, severity, basis = classify_credit_event(item, match)
    assert category == "confirmed_default" and severity == "INFO"
    assert "parent_exposure_not_confirmed" in basis
    monkeypatch.setattr("bond_watch.news_sources.datetime", type("Fixed", (datetime,),
                        {"now": classmethod(lambda cls, tz=None: NOW)}))
    poll_corporate_rss(conn, settings, Http(feed(item.title)), "moex_news")
    row = conn.execute("SELECT relation_type,severity,classification_basis,confirmation_source FROM news").fetchone()
    assert row["relation_type"] == "related_company" and row["severity"] == "INFO"
    assert "parent_exposure_not_confirmed" in row["classification_basis"]
    assert row["confirmation_source"].endswith("verified-link")
    (folder / "related_companies.yaml").write_text("companies: []\n", encoding="utf-8")
    sync_related_companies(conn, tmp_path)
    assert match_issuer_news(conn, item, _issuer_rows(conn), Http(b""),
                             {"used": 0, "limit": 1}).reason == "no_issuer_match"
    conn.close()


def test_iss_cache_expires_and_rechecks(tmp_path):
    conn, _ = setup(tmp_path)
    issuers = _issuer_rows(conn)
    item = Publication("moex_news", "cache", "https://www.moex.com/n1", "MOEX", NOW.isoformat(),
                       f"Выпуск {RELATED}: финансовые результаты", "")
    first = Http(b"")
    assert match_issuer_news(conn, item, issuers, first, {"used": 0, "limit": 1}).relation == "related_bond"
    assert first.iss_calls == 2
    with conn:
        conn.execute("UPDATE isin_issuer_cache SET verified_at='2020-01-01T00:00:00+00:00' WHERE isin=?", (RELATED,))
    refreshed = Http(b"")
    assert match_issuer_news(conn, item, issuers, refreshed, {"used": 0, "limit": 1}).relation == "related_bond"
    assert refreshed.iss_calls == 2
    conn.close()


def test_rating_warning_negated_default_and_event_date():
    match = Match(1, "issuer", (), (PORTFOLIO_RZD,), "verified_issuer_name", "matched")
    def item(title):
        return Publication("moex_news", "x", "https://www.moex.com/n1", "MOEX", NOW.isoformat(), title, "")
    assert classify_credit_event(item("Агентство снизило кредитный рейтинг эмитента"), match)[:2] == ("rating", "WARNING")
    assert classify_credit_event(item("Эмитент не допустило дефолт"), match)[1] != "CRITICAL"
    assert classify_credit_event(item("Обсуждение риска дефолта эмитента"), match)[1] != "CRITICAL"
    assert event_date_from_title("09.10.2026: отчетность эмитента") == "2026-10-09"
    assert event_date_from_title("Отчетность за 09.10.2026") is None
