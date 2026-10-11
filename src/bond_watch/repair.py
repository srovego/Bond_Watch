"""Explicit, evidence-backed correction of legacy price observation dates."""
from __future__ import annotations

import argparse
import math
import sqlite3
import uuid
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

from .db import backup, connect


def repair_price_date(db_path: Path, isin: str, stored_date: str, trade_moment: str,
                      clean_price: float, nominal: float, evidence_url: str,
                      *, apply: bool = False) -> Path | None:
    if not isin or not isin.isascii() or not isin.isalnum():
        raise ValueError("A valid ISIN is required")
    date.fromisoformat(stored_date)
    moment = datetime.fromisoformat(trade_moment)
    if moment.tzinfo is not None or len(trade_moment) < 19:
        raise ValueError("Use the MOEX local trade moment with seconds and no timezone")
    corrected_date = moment.date().isoformat()
    if corrected_date == stored_date:
        raise ValueError("Confirmed date equals stored date; no correction needed")
    if not math.isfinite(clean_price) or clean_price <= 0 or not math.isfinite(nominal) or nominal <= 0:
        raise ValueError("Price and nominal must be positive finite numbers")
    url = urlparse(evidence_url)
    if url.scheme != "https" or url.hostname not in {"iss.moex.com", "www.moex.com", "moex.com"}:
        raise ValueError("Provide an HTTPS MOEX evidence URL")
    if not db_path.is_file():
        raise FileNotFoundError(db_path)
    conn = connect(db_path)
    try:
        def check():
            row = conn.execute(
                "SELECT clean_price,nominal,trade_moment FROM price_observations WHERE isin=? AND trading_date=?",
                (isin, stored_date),
            ).fetchone()
            if row is None:
                raise ValueError("Stored observation not found")
            if row["trade_moment"] is not None:
                raise ValueError("Observation already has a verified trade moment")
            if not math.isclose(row["clean_price"], clean_price, rel_tol=0, abs_tol=1e-6) or not math.isclose(row["nominal"], nominal, rel_tol=0, abs_tol=1e-6):
                raise ValueError("Confirmed price or nominal differs from stored observation")
            if conn.execute("SELECT 1 FROM price_observations WHERE isin=? AND trading_date=?", (isin, corrected_date)).fetchone():
                raise ValueError("An observation already exists on the confirmed date")
            if conn.execute("SELECT 1 FROM news WHERE source_id='moex_iss' AND source_item_id=?", (f"price:{isin}:{stored_date}",)).fetchone():
                raise ValueError("A price alert references the stored date; manual review required")
        check()
        print(f"{isin}: {stored_date} -> {corrected_date} at {trade_moment}; price {clean_price}, nominal {nominal}")
        print(f"MOEX evidence: {evidence_url}")
        if not apply:
            print("Dry run only. Add --apply after checking the evidence.")
            return None
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        destination = db_path.parent / "backups" / f"price_date_repair_{stamp}_{uuid.uuid4().hex[:8]}.db"
        backup(conn, destination)
        try:
            conn.execute("BEGIN IMMEDIATE")
            check()
            conn.execute(
                "UPDATE price_observations SET trading_date=?,trade_moment=? WHERE isin=? AND trading_date=?",
                (corrected_date, moment.isoformat(sep=" ", timespec="seconds"), isin, stored_date),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        print(f"Applied. Backup: {destination}")
        return destination
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/bond_watch.db"))
    parser.add_argument("--isin", required=True)
    parser.add_argument("--stored-date", required=True)
    parser.add_argument("--trade-moment", required=True)
    parser.add_argument("--clean-price", type=float, required=True)
    parser.add_argument("--nominal", type=float, required=True)
    parser.add_argument("--evidence-url", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        repair_price_date(args.db, args.isin, args.stored_date, args.trade_moment,
                          args.clean_price, args.nominal, args.evidence_url, apply=args.apply)
    except (ValueError, FileNotFoundError, sqlite3.Error) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
