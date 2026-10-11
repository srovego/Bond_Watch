from __future__ import annotations

from datetime import datetime, timezone

from bond_watch.cbr_decisions import Decision, classify_decision, identify_issuer, parse_decisions
from bond_watch.db import connect
from bond_watch.pipeline import init_sources, poll_cbr_decisions
from bond_watch.sources import Publication


def feed(title, body, guid="msg_53737", category="Решение в отношении ценных бумаг"):
    return (f"""<rss><channel><item><title>{title}</title><guid>{guid}</guid>
<description>{body}</description><pubDate>Mon, 12 Oct 2026 12:00:00 +0300</pubDate>
<category>{category}</category></item></channel></rss>""").encode("utf-8")


def test_official_instrument_rss_identity_link_type_and_risk():
    payload = feed(
        "12.10.2026 ОАО «РЖД», ОГРН 1037739877295, ИНН 7708503727",
        "Банк России принял решение о государственной регистрации выпуска облигаций ОАО «РЖД».")
    rows, invalid = parse_decisions(payload, "cbr_instruments",
                                    now=datetime(2026, 10, 12, 10, tzinfo=timezone.utc))
    assert invalid == 0 and len(rows) == 1
    row = rows[0]
    assert row.subject_inn == "7708503727" and row.subject_ogrn == "1037739877295"
    assert row.publication.url == "https://www.cbr.ru/rbr/insideDKO/doc?id=53737"
    assert row.publication.published_at == "2026-10-12T09:00:00+00:00"
    issuer = {"id": 1, "name": "открытое акционерное общество Российские железные дороги",
              "inn": "7708503727", "category": "corporate", "isins": ["RU000A10AZ60"]}
    assert identify_issuer(row, [issuer]) == issuer
    assert classify_decision(row)[1] == "INFO"
    unrelated = Decision(row.publication, row.decision_type, "7707049388", row.subject_ogrn, row.subject_name)
    assert identify_issuer(unrelated, [issuer]) is None


def test_participant_identity_requires_name_and_unique_inn():
    payload = feed("Об аннулировании лицензии ОАО РЖД",
                   "Банк России аннулировал лицензию ОАО РЖД (ИНН 7708503727, ОГРН 1037739877295).",
                   "msg_51625", "Лицензионные меры")
    row = parse_decisions(payload, "cbr_participants",
                          now=datetime(2026, 10, 12, 10, tzinfo=timezone.utc))[0][0]
    issuer = {"id": 1, "name": "Российские железные дороги",
              "inn": "7708503727", "category": "corporate", "isins": []}
    assert row.publication.url == "https://www.cbr.ru/rbr/rbr_fr/doc?id=51625"
    assert identify_issuer(row, [issuer]) == issuer
    third_party = feed("Об аннулировании лицензии другой компании",
                       "Лицензия другой компании; ОАО РЖД (ИНН 7708503727) упомянуто как контрагент.")
    row = parse_decisions(third_party, "cbr_participants",
                          now=datetime(2026, 10, 12, 10, tzinfo=timezone.utc))[0][0]
    assert identify_issuer(row, [issuer]) is None
    multiple = feed("Об аннулировании лицензии ОАО РЖД",
                    "ОАО РЖД ИНН 7708503727; контрагент ИНН 7707049388.")
    row = parse_decisions(multiple, "cbr_participants",
                          now=datetime(2026, 10, 12, 10, tzinfo=timezone.utc))[0][0]
    assert identify_issuer(row, [issuer]) is None


def test_invalid_feed_and_negative_decision():
    rows, bad = parse_decisions(feed("x", "y", guid="not-a-doc"), "cbr_instruments",
                                now=datetime(2026, 10, 12, 10, tzinfo=timezone.utc))
    assert rows == [] and bad == 1
    item = Publication("cbr_instruments", "msg_1", "https://www.cbr.ru/rbr/insideDKO/doc?id=1",
                       "ЦБ", "2026-10-12", "РЖД",
                       "Банк России принял решение об отказе в государственной регистрации выпуска облигаций.")
    assert classify_decision(Decision(item, "Решение в отношении ценных бумаг",
                                      "7708503727", None, "РЖД"))[1] == "WARNING"


def test_source_failure_and_cross_source_duplicate(tmp_path, monkeypatch):
    conn = connect(tmp_path / "decisions.sqlite")
    init_sources(conn, {"sources": {"cbr_instruments": True}})
    with conn:
        issuer_id = conn.execute(
            "INSERT INTO issuers(name,inn) VALUES('ОАО РЖД','7708503727')").lastrowid
        conn.execute(
            "INSERT INTO bonds(isin,issuer_id,status) VALUES('RU000A10AZ60',?,'verified')", (issuer_id,))
    class Http:
        def get(self, url):
            return object()
    def fail(*args, **kwargs):
        raise TimeoutError("feed timeout")
    monkeypatch.setattr("bond_watch.pipeline.fetch_decisions", fail)
    poll_cbr_decisions(conn, {"sources": {"cbr_instruments": True}}, Http(), "cbr_instruments")
    assert conn.execute("SELECT status FROM sources WHERE id='cbr_instruments'").fetchone()[0] == "error"
    payload = feed("12.10.2026 ОАО РЖД, ОГРН 1037739877295, ИНН 7708503727",
                   "Банк России принял решение о регистрации облигаций ОАО РЖД.")
    decision = parse_decisions(payload, "cbr_instruments",
                               now=datetime(2026, 10, 12, 10, tzinfo=timezone.utc))[0][0]
    monkeypatch.setattr("bond_watch.pipeline.fetch_decisions", lambda *args, **kwargs: ([decision], 0))
    poll_cbr_decisions(conn, {"sources": {"cbr_instruments": True}}, Http(), "cbr_instruments")
    poll_cbr_decisions(conn, {"sources": {"cbr_instruments": True}}, Http(), "cbr_instruments")
    assert conn.execute("SELECT count(*) FROM news").fetchone()[0] == 1
    assert conn.execute("SELECT status FROM sources WHERE id='cbr_instruments'").fetchone()[0] == "ok"
    conn.close()


def test_disabled_source_and_duplicate_from_existing_cbr(tmp_path, monkeypatch):
    conn = connect(tmp_path / "cross.sqlite")
    init_sources(conn, {"sources": {"cbr_instruments": False, "cbr_site_rss": True}})
    with conn:
        issuer_id = conn.execute(
            "INSERT INTO issuers(name,inn) VALUES('ОАО РЖД','7708503727')").lastrowid
        conn.execute(
            "INSERT INTO bonds(isin,issuer_id,status) VALUES('RU000A10AZ60',?,'verified')", (issuer_id,))
    class Http:
        def get(self, url):
            raise AssertionError("duplicate should not fetch document")
    assert poll_cbr_decisions(conn, {"sources": {"cbr_instruments": False}}, Http(),
                              "cbr_instruments") is None
    assert conn.execute("SELECT status FROM sources WHERE id='cbr_instruments'").fetchone()[0] == "unavailable"
    payload = feed("12.10.2026 ОАО РЖД, ОГРН 1037739877295, ИНН 7708503727",
                   "Банк России принял решение о регистрации облигаций ОАО РЖД.")
    decision = parse_decisions(payload, "cbr_instruments",
                               now=datetime(2026, 10, 12, 10, tzinfo=timezone.utc))[0][0]
    with conn:
        old = conn.execute(
            """INSERT INTO news(source_id,source_item_id,url,source_name,published_at,discovered_at,
            title,body,category,severity,fingerprint)
            VALUES('cbr_site_rss','old',?,'ЦБ',? ,?,'Старый заголовок',?,'issuer_news','INFO','old')""",
            (decision.publication.url, decision.publication.published_at,
             decision.publication.published_at, decision.publication.body)).lastrowid
        conn.execute("INSERT INTO news_issuers(news_id,issuer_id) VALUES(?,?)", (old, issuer_id))
    init_sources(conn, {"sources": {"cbr_instruments": True, "cbr_site_rss": True}})
    monkeypatch.setattr("bond_watch.pipeline.fetch_decisions", lambda *args, **kwargs: ([decision], 0))
    poll_cbr_decisions(conn, {"sources": {"cbr_instruments": True}}, Http(), "cbr_instruments")
    assert conn.execute("SELECT count(*) FROM news").fetchone()[0] == 1
    assert conn.execute(
        "SELECT decision FROM rss_item_checks ORDER BY id DESC LIMIT 1").fetchone()[0] == "already_known"
    conn.close()
