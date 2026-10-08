from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bond_watch.analysis import classify, match_issuers
from bond_watch.db import connect
from bond_watch.cli import main as cli_main
from bond_watch.pipeline import ingest_publication, init_sources, _price_event
from bond_watch.sources import Publication, fetch_bond_price, fetch_cbr_rss, resolve_isin
from bond_watch.telegram import digest_text, send_once


class Response:
    def __init__(self, data=None, content=b""):
        self.data = data
        self.content = content

    def json(self):
        return self.data


class FakeHttp:
    def __init__(self, responses):
        self.responses = iter(responses)

    def get(self, *_args, **_kwargs):
        return next(self.responses)


def test_resolve_requires_exact_isin_and_issuer():
    payload = {
        "securities": {
            "columns": ["secid", "ISIN", "shortname"],
            "data": [["SU29007RMFS0", "RU000A0JV4M0", "ОФЗ 29007"]],
        },
        "description": {
            "columns": ["name", "value"],
            "data": [["ISSUERNAME", "Минфин России"], ["ISSUERINN", "7710168360"], ["SECTYPE", "ofz"]],
        },
    }
    data = resolve_isin(FakeHttp([Response(payload)]), "RU000A0JV4M0", "SU29007RMFS0")
    assert data["issuer_name"] == "Минфин России"
    assert data["inn"] == "7710168360"
    assert data["category"] == "ofz"
    with pytest.raises(ValueError, match="exact"):
        resolve_isin(FakeHttp([Response(payload)]), "RU000A108EF8", "SU29007RMFS0")


def test_price_requires_trade_and_nominal():
    payload = {
        "marketdata": {"columns": ["BOARDID", "NUMTRADES", "LAST", "LASTTRADEDATE", "PREVPRICE"], "data": [["TQOB", 4, 95.2, "2026-10-08", 96.0]]},
        "securities": {"columns": ["BOARDID", "FACEVALUE"], "data": [["TQOB", 1000]]},
    }
    assert fetch_bond_price(FakeHttp([Response(payload)]), "SU29007RMFS0")["clean_price"] == 95.2
    payload["marketdata"]["data"][0][1] = 0
    assert fetch_bond_price(FakeHttp([Response(payload)]), "SU29007RMFS0") is None


def test_cbr_rss_keeps_source_url_and_publication_time():
    xml = b"""<rss><channel><item><guid>123</guid><title>OFZ update</title>
    <link>https://www.cbr.ru/press/event/?id=1</link>
    <pubDate>Thu, 08 Oct 2026 09:00:00 +0300</pubDate>
    <description>details</description></item></channel></rss>"""
    items = fetch_cbr_rss(FakeHttp([Response(content=xml), Response(content=b"<rss><channel/></rss>")]), lookback_days=365)
    assert len(items) == 1
    assert items[0].published_at.startswith("2026-10-08T06:00:00")
    assert items[0].url.startswith("https://www.cbr.ru/")


def test_matching_and_classification_conservative():
    issuers = [{"id": 1, "name": "ООО Ромашка Финанс", "inn": "1234567890", "category": "corporate"}]
    item = Publication("cbr_rss", "1", "https://www.cbr.ru/a", "ЦБ", None, "Дефолт ООО Ромашка Финанс", "")
    assert match_issuers(item, issuers) == [1]
    assert classify(item) == ("credit_event", "CRITICAL")
    unrelated = Publication("cbr_rss", "2", "https://www.cbr.ru/b", "ЦБ", None, "Ромашка", "")
    assert match_issuers(unrelated, issuers) == []


def test_cross_source_dedup_and_send_once(tmp_path):
    conn = connect(tmp_path / "db.sqlite")
    init_sources(conn, {"sources": {"cbr_rss": True, "moex_iss": True}})
    with conn:
        issuer = conn.execute("INSERT INTO issuers(name,inn) VALUES('ООО Ромашка Финанс','1234567890')").lastrowid
        conn.execute("INSERT INTO bonds(isin,issuer_id,status) VALUES('RU000A0JV4M0',?,'verified')", (issuer,))
    first = Publication("cbr_rss", "a", "https://www.cbr.ru/a", "ЦБ", "2026-10-08", "Дефолт ООО Ромашка Финанс", "")
    second = Publication("moex_iss", "b", "https://www.moex.com/b", "MOEX", "2026-10-08", first.title, "")
    assert ingest_publication(conn, first)
    assert ingest_publication(conn, second)
    assert digest_text(conn).count("Дефолт ООО Ромашка Финанс") == 1
    class Bot:
        count = 0
        def send(self, *_):
            self.count += 1
    bot = Bot()
    assert send_once(conn, bot, "42", "message", "key")
    assert not send_once(conn, bot, "42", "message", "key")
    assert bot.count == 1


def test_price_event_links_verified_issuer(tmp_path):
    conn = connect(tmp_path / "db.sqlite")
    init_sources(conn, {"sources": {"cbr_rss": True, "moex_iss": True}})
    with conn:
        issuer = conn.execute("INSERT INTO issuers(name) VALUES('Test Issuer')").lastrowid
        conn.execute("INSERT INTO bonds(isin,issuer_id,status) VALUES('RU000A0JV4M0',?,'verified')", (issuer,))
    bond = conn.execute("SELECT * FROM bonds").fetchone()
    previous = {"trading_date": "2026-10-07", "clean_price": 100.0}
    current = {"trading_date": "2026-10-08", "clean_price": 96.0}
    _price_event(conn, bond, current, previous, -4)
    row = conn.execute("SELECT category,severity FROM news").fetchone()
    assert tuple(row) == ("price_move", "WARNING")
    assert conn.execute("SELECT count(*) FROM news_issuers").fetchone()[0] == 1




def test_resolve_new_isin_uses_exact_search():
    search = {"securities": {"columns": ["secid", "isin"], "data": [["SU29007RMFS0", "RU000A0JV4M0"]]}}
    detail = {
        "securities": {"columns": ["secid", "ISIN"], "data": [["SU29007RMFS0", "RU000A0JV4M0"]]},
        "description": {"columns": ["name", "value"], "data": [["ISSUERNAME", "Минфин России"], ["ISIN", "RU000A0JV4M0"]]},
    }
    result = resolve_isin(FakeHttp([Response(search), Response(detail)]), "RU000A0JV4M0")
    assert result["secid"] == "SU29007RMFS0"


def test_verified_snapshot_seeds_all_bonds(tmp_path):
    from bond_watch.config import project_root
    from bond_watch.pipeline import seed_verified_bonds, sync_portfolio
    conn = connect(tmp_path / "db.sqlite")
    sync_portfolio(conn, project_root())
    seed_verified_bonds(conn, project_root())
    assert conn.execute("SELECT count(*) FROM bonds WHERE status='verified'").fetchone()[0] == 17
    assert conn.execute("SELECT count(*) FROM issuers").fetchone()[0] == 8


def test_price_poll_uses_previous_close_and_skips_amortization(tmp_path):
    from bond_watch.pipeline import poll_prices
    conn = connect(tmp_path / "db.sqlite")
    init_sources(conn, {"sources": {"moex_iss": True}})
    with conn:
        issuer = conn.execute("INSERT INTO issuers(name,category) VALUES('Issuer','corporate')").lastrowid
        conn.execute(
            "INSERT INTO bonds(isin,secid,issuer_id,status,card_url) VALUES('RU000A0JV4M0','RU000A0JV4M0',?,'verified','https://moex.com/card')",
            (issuer,),
        )
    def price(date, last, prev, nominal):
        return Response({
            "marketdata": {"columns": ["BOARDID", "NUMTRADES", "LAST", "PREVPRICE", "LASTTRADEDATE"], "data": [["TQCB", 3, last, prev, date]]},
            "securities": {"columns": ["BOARDID", "FACEVALUE"], "data": [["TQCB", nominal]]},
        })
    settings = {"sources": {"moex_iss": True}, "price_threshold_corporate_percent": 3, "price_threshold_ofz_percent": 2, "request_pause_seconds": 0}
    poll_prices(conn, settings, FakeHttp([price("2026-10-07", 100, 100, 1000)]))
    poll_prices(conn, settings, FakeHttp([price("2026-10-08", 96, 100, 1000)]))
    assert conn.execute("SELECT count(*) FROM news WHERE category='price_move'").fetchone()[0] == 1
    poll_prices(conn, settings, FakeHttp([price("2026-10-09", 90, 96, 900)]))
    assert conn.execute("SELECT count(*) FROM news WHERE category='price_move'").fetchone()[0] == 1




def test_long_digest_is_split_without_losing_news(tmp_path):
    from bond_watch.telegram import digest_parts
    conn = connect(tmp_path / "db.sqlite")
    init_sources(conn, {"sources": {"cbr_rss": True}})
    with conn:
        issuer = conn.execute("INSERT INTO issuers(name,inn) VALUES('Long Company','1234567890')").lastrowid
        conn.execute("INSERT INTO bonds(isin,issuer_id,status) VALUES('RU000A0JV4M0',?,'verified')", (issuer,))
    for index in range(15):
        item = Publication(
            "cbr_rss", str(index), f"https://www.cbr.ru/{index}", "ЦБ", "2026-10-08",
            f"Long Company report {index}", "X" * 400,
        )
        assert ingest_publication(conn, item)
    parts = digest_parts(conn)
    assert len(parts) > 1
    assert all(len(part) <= 3900 for part in parts)
    assert sum(part.count("Long Company report") for part in parts) == 15


def test_hypothetical_default_is_not_critical():
    item = Publication("cbr_rss", "x", "https://www.cbr.ru/x", "ЦБ", None, "Риск дефолта эмитента", "")
    assert classify(item)[1] != "CRITICAL"



def test_verified_ratings_are_seeded_with_provenance(tmp_path):
    from bond_watch.config import project_root
    from bond_watch.pipeline import seed_verified_bonds, sync_portfolio
    conn = connect(tmp_path / "ratings.sqlite")
    sync_portfolio(conn, project_root())
    seed_verified_bonds(conn, project_root())
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    assert conn.execute("SELECT count(*) FROM bonds WHERE rating IS NOT NULL AND rating_url IS NOT NULL AND rating_date IS NOT NULL").fetchone()[0] == 6
    assert conn.execute("SELECT rating FROM bonds WHERE isin='RU000A1090N4'").fetchone()[0] is None


def test_price_event_reports_related_publication(tmp_path):
    conn = connect(tmp_path / "price_news.sqlite")
    init_sources(conn, {"sources": {"cbr_rss": True, "moex_iss": True}})
    with conn:
        issuer = conn.execute("INSERT INTO issuers(name,inn) VALUES('Example Issuer','1234567890')").lastrowid
        conn.execute("INSERT INTO bonds(isin,issuer_id,status) VALUES('RU000A0JV4M0',?,'verified')", (issuer,))
    item = Publication("cbr_rss", "related", "https://www.cbr.ru/example", "ЦБ", None, "Example Issuer report", "")
    assert ingest_publication(conn, item)
    bond = conn.execute("SELECT * FROM bonds").fetchone()
    _price_event(conn, bond, {"trading_date": "2026-10-08", "clean_price": 96.0}, {"clean_price": 100.0}, -4)
    body = conn.execute("SELECT body FROM news WHERE category='price_move'").fetchone()[0]
    assert "Example Issuer report" in body
    assert "https://www.cbr.ru/example" in body
