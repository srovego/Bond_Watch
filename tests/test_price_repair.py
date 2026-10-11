import sqlite3

import pytest

from bond_watch.db import connect
from bond_watch.repair import repair_price_date
from bond_watch.pipeline import init_sources


ISIN = "RU000A1095W4"
URL = "https://iss.moex.com/iss/securities/RU000A1095W4.json"


def prepared_db(tmp_path):
    path = tmp_path / "bond_watch.db"
    conn = connect(path)
    init_sources(conn, {"sources": {"moex_iss": True}})
    with conn:
        conn.execute("INSERT INTO issuers(name) VALUES('Issuer')")
        conn.execute("INSERT INTO bonds(isin,issuer_id,status) VALUES(?,1,'verified')", (ISIN,))
        conn.execute("INSERT INTO price_observations(isin,trading_date,clean_price,nominal,observed_at) VALUES(?,?,?,?,?)",
                     (ISIN, "2026-10-11", 100.24, 1000, "2026-10-11T12:00:00+00:00"))
    conn.close()
    return path


def call(path, **kwargs):
    return repair_price_date(path, ISIN, "2026-10-11", "2026-10-09 20:45:42",
                             100.24, 1000, URL, **kwargs)


def test_price_date_repair_dry_run_then_backup_and_apply(tmp_path):
    path = prepared_db(tmp_path)
    assert call(path) is None
    conn = connect(path)
    assert conn.execute("SELECT trading_date FROM price_observations").fetchone()[0] == "2026-10-11"
    conn.close()
    backup_path = call(path, apply=True)
    assert backup_path.is_file()
    with sqlite3.connect(backup_path) as old:
        assert old.execute("SELECT trading_date FROM price_observations").fetchone()[0] == "2026-10-11"
    conn = connect(path)
    assert tuple(conn.execute("SELECT trading_date,trade_moment FROM price_observations").fetchone()) == ("2026-10-09", "2026-10-09 20:45:42")
    conn.close()


def test_price_date_repair_rejects_conflict_alert_and_wrong_quote(tmp_path):
    path = prepared_db(tmp_path)
    with pytest.raises(ValueError, match="price or nominal"):
        repair_price_date(path, ISIN, "2026-10-11", "2026-10-09 20:45:42", 101, 1000, URL, apply=True)
    conn = connect(path)
    with conn:
        conn.execute("INSERT INTO price_observations(isin,trading_date,clean_price,nominal,observed_at) VALUES(?,?,?,?,?)",
                     (ISIN, "2026-10-09", 99, 1000, "2026-10-09"))
    conn.close()
    with pytest.raises(ValueError, match="already exists"):
        call(path, apply=True)
    conn = connect(path)
    with conn:
        conn.execute("DELETE FROM price_observations WHERE trading_date='2026-10-09'")
        conn.execute("INSERT INTO news(source_id,source_item_id,url,source_name,discovered_at,title,body,category,severity,fingerprint,analysis_status) VALUES('moex_iss',?,?,?,?,?,?,?,?,?,?)",
                     (f"price:{ISIN}:2026-10-11", URL, "MOEX", "2026-10-11", "Alert", "Alert", "price_move", "WARNING", "test", "disabled"))
    conn.close()
    with pytest.raises(ValueError, match="price alert"):
        call(path, apply=True)
