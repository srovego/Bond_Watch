from datetime import datetime, timezone
from pathlib import Path

from bond_watch.db import connect
from bond_watch.news_sources import is_market_event, match_portfolio, parse_rss
from bond_watch.pipeline import init_sources, poll_corporate_rss
from bond_watch.sources import Publication


CBR_XML = (Path(__file__).parent / "fixtures" / "cbr_rss_news.xml").read_bytes()
NOW = datetime(2026, 10, 11, 13, tzinfo=timezone.utc)
HOSTS = {"www.cbr.ru", "cbr.ru"}


class Response:
    content = CBR_XML


class FakeHttp:
    def get(self, *_args):
        return Response()


def test_real_cbr_rss_structure_and_updated_guid_identity():
    items, invalid = parse_rss(CBR_XML, source_id="cbr_site_rss", source_name="CBR",
                               allowed_hosts=HOSTS, now=NOW)
    assert len(items) == 3 and invalid == 0
    assert items[0].title.startswith("Информация о ценных бумагах")
    assert items[0].published_at == "2026-10-11T08:51:14+00:00"
    changed = CBR_XML.replace(b"11:51:14", b"12:51:14", 1)
    updated, _ = parse_rss(changed, source_id="cbr_site_rss", source_name="CBR",
                           allowed_hosts=HOSTS, now=NOW)
    assert updated[0].item_id != items[0].item_id


def test_strong_identifiers_names_and_ambiguity():
    issuers = [
        {"id": 1, "inn": "7707049388", "name": "ПАО Ростелеком", "category": "corporate", "isins": ["RU000A1095W4"]},
        {"id": 2, "inn": "7708591995", "name": "ПАО ТрансКонтейнер", "category": "corporate", "isins": ["RU000A109E71"]},
    ]
    def item(title):
        return Publication("moex_news", "id", "https://www.moex.com/n1", "MOEX", NOW.isoformat(), title, "")
    assert match_portfolio(item("RU000A1095W4"), issuers) == (1, ["RU000A1095W4"], "issue", "isin")
    assert match_portfolio(item("ИНН 7707049388"), issuers) == (1, [], "issuer", "inn")
    assert match_portfolio(item("Ростелеком"), issuers) == (1, [], "issuer", "name")
    assert match_portfolio(item("RU000A1095W4 и ТрансКонтейнер"), issuers)[2] == "ambiguous"
    assert match_portfolio(item("Облигации RU000A1095W5"), issuers)[2] == "market"


def test_poll_real_feed_rejections_and_issue_persistence(tmp_path, monkeypatch):
    conn = connect(tmp_path / "news.db")
    settings = {"sources": {"cbr_site_rss": True, "moex_news": True}, "news_lookback_days": 3}
    init_sources(conn, settings)
    with conn:
        issuer_id = conn.execute("INSERT INTO issuers(name,inn,category) VALUES(?,?,?)",
                                 ("ПАО Ростелеком", "7707049388", "corporate")).lastrowid
        conn.execute("INSERT INTO bonds(isin,issuer_id,status) VALUES(?,?,?)",
                     ("RU000A1095W4", issuer_id, "verified"))
    monkeypatch.setattr("bond_watch.news_sources.datetime", type("FixedDate", (datetime,),
                        {"now": classmethod(lambda cls, tz=None: NOW)}))
    poll_corporate_rss(conn, settings, FakeHttp(), "cbr_site_rss")
    run = conn.execute("SELECT * FROM collector_runs ORDER BY id DESC LIMIT 1").fetchone()
    assert run["status"] == "ok" and run["items_seen"] == 3
    assert conn.execute("SELECT count(*) FROM news").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM rss_item_checks WHERE decision LIKE 'no_portfolio_match:%'").fetchone()[0] == 3

    xml = b"<rss><channel><item><title>RU000A1095W4 trading notice</title><link>https://www.moex.com/n1</link><guid>x1</guid><pubDate>Sun, 11 Oct 2026 11:51:14 +0300</pubDate></item></channel></rss>"
    class MoexHttp:
        def get(self, *_args):
            return type("Reply", (), {"content": xml})()
    poll_corporate_rss(conn, settings, MoexHttp(), "moex_news")
    poll_corporate_rss(conn, settings, MoexHttp(), "moex_news")
    news = conn.execute("SELECT * FROM news").fetchall()
    assert len(news) == 1 and news[0]["event_scope"] == "issue"
    assert conn.execute("SELECT isin FROM news_bonds").fetchone()[0] == "RU000A1095W4"
    assert conn.execute("SELECT count(*) FROM news_issuers").fetchone()[0] == 1
    runs = conn.execute("SELECT rejection_counts_json FROM collector_runs WHERE source_id='moex_news' ORDER BY id").fetchall()
    assert '"saved": 1' in runs[0][0]
    assert '"already_known": 1' in runs[1][0]
    conn.close()


def test_market_notice_requires_exchange_source_and_broad_trading_scope():
    notice = Publication("moex_news", "m", "https://www.moex.com/n2", "MOEX", NOW.isoformat(),
                         "Приостановка торгов всеми облигациями", "")
    assert is_market_event(notice)
    assert not is_market_event(Publication("cbr_site_rss", "m", notice.url, "CBR", NOW.isoformat(),
                                           notice.title, ""))
    assert not is_market_event(Publication("moex_news", "m", notice.url, "MOEX", NOW.isoformat(),
                                           "Приостановка торгов облигациями другого эмитента", ""))


def test_rss_rejects_missing_dates_and_foreign_links():
    xml = (b"<rss><channel><item><title>Notice</title><link>https://evil.example/a</link>"
           b"<pubDate>Sun, 11 Oct 2026 11:51:14 +0300</pubDate></item>"
           b"<item><title>Notice</title><link>https://www.cbr.ru/a</link></item></channel></rss>")
    items, invalid = parse_rss(xml, source_id="cbr_site_rss", source_name="CBR",
                               allowed_hosts=HOSTS, now=NOW)
    assert items == [] and invalid == 2


def test_v5_database_migrates_without_deleting_news(tmp_path):
    import sqlite3
    from bond_watch.db import MIGRATIONS
    path = tmp_path / "existing.sqlite"
    raw = sqlite3.connect(path)
    for version, migration in enumerate(MIGRATIONS[:5], start=1):
        raw.executescript(migration + f"\nPRAGMA user_version={version};")
    raw.execute("INSERT INTO sources(id,name,enabled,status) VALUES('cbr_rss','CBR',1,'ok')")
    raw.execute("""INSERT INTO news(source_id,source_item_id,url,source_name,discovered_at,title,
                category,severity,fingerprint) VALUES('cbr_rss','old','https://www.cbr.ru/a',
                'CBR','2026-10-10','Old','other','INFO','old')""")
    raw.commit()
    raw.close()
    conn = connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 6
    row = conn.execute("SELECT title,event_scope FROM news").fetchone()
    assert row["title"] == "Old" and row["event_scope"] is None
    conn.close()
