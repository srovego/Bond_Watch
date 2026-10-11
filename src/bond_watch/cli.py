from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import load_env, load_portfolio, load_settings, project_root
from .db import backup, connect, utcnow
from .email_sender import EmailConfig, TEST_SUBJECT, send_digest_once, send_email
from .email_notifications import process_daily_email, retry_safe
from .http import Http
from .pipeline import init_sources, poll, resolve_portfolio, sync_portfolio, seed_verified_bonds
from .telegram import Telegram, digest_parts, run_bot, send_once


def setup_logging(root: Path, settings: dict) -> None:
    path = root / settings.get("log_file", "data/bond_watch.log")
    path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[RotatingFileHandler(path, maxBytes=5_000_000, backupCount=3, encoding="utf-8"), logging.StreamHandler()],
    )


def send_digest(conn, settings: dict, scheduled: bool, dry_run: bool, email: bool = False) -> None:
    now = datetime.now(ZoneInfo(settings.get("digest_timezone", "Europe/Moscow")))
    slot = f"{now:%Y-%m-%d-%H}" if scheduled else f"manual-{utcnow()}"
    key = f"digest:{slot}"
    last = conn.execute("SELECT value FROM bot_state WHERE key='last_digest_at'").fetchone()
    cutoff = utcnow()
    parts = digest_parts(conn, since=last["value"] if last else None, until=cutoff)
    if dry_run:
        print("\n\n".join(parts))
        return
    if email:
        delivered = send_digest_once(conn, EmailConfig.from_env(), "\n\n".join(parts))
        print("Email digest sent" if delivered else "Email digest already handled")
        return
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not chat_id:
        raise RuntimeError("TELEGRAM_CHAT_ID is missing")
    telegram = Telegram()
    for index, message in enumerate(parts):
        send_once(conn, telegram, chat_id, message, f"{key}:{index}")
    if scheduled:
        with conn:
            conn.execute("INSERT OR REPLACE INTO bot_state(key,value) VALUES('last_digest_at',?)", (cutoff,))
    print(f"Digest processed: {len(parts)} message(s)")

def status(conn) -> None:
    print("Sources:")
    for row in conn.execute("SELECT * FROM sources ORDER BY id"):
        print(f"  {row['id']}: {row['status']} | last success {row['last_success_at'] or 'never'} | {row['last_error'] or ''}")
    latest_prices = conn.execute(
        "SELECT items_seen,rejection_counts_json FROM collector_runs "
        "WHERE source_id='moex_iss' AND rejection_counts_json IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if latest_prices:
        print(f"Price quotes: accepted {latest_prices['items_seen']} | diagnostics {latest_prices['rejection_counts_json']}")
    latest_rss = conn.execute(
        "SELECT items_seen,rejection_counts_json FROM collector_runs "
        "WHERE source_id='cbr_rss' AND rejection_counts_json IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if latest_rss:
        print(f"CBR RSS: received {latest_rss['items_seen']} | diagnostics {latest_rss['rejection_counts_json']}")
    for source_id in ("cbr_site_rss", "moex_news"):
        latest = conn.execute(
            "SELECT items_seen,rejection_counts_json,status,error FROM collector_runs "
            "WHERE source_id=? ORDER BY id DESC LIMIT 1", (source_id,)
        ).fetchone()
        if latest:
            print(f"{source_id}: {latest['status']} | received {latest['items_seen']} | "
                  f"diagnostics {latest['rejection_counts_json'] or '{}'} | {latest['error'] or ''}")
    print("Bonds:")
    for row in conn.execute("SELECT b.isin,b.status,b.resolution_error,i.name FROM bonds b LEFT JOIN issuers i ON i.id=b.issuer_id ORDER BY b.isin"):
        print(f"  {row['isin']}: {row['name'] or 'unresolved'} [{row['status']}] {row['resolution_error'] or ''}")
    print("Notifications:")
    for row in conn.execute("SELECT kind,status,created_at,error FROM notifications ORDER BY id DESC LIMIT 5"):
        print(f"  {row['kind']}: {row['status']} {row['created_at']} {row['error'] or ''}")


def rss_recent(conn, limit: int, source_id: str = "cbr_rss") -> None:
    run = conn.execute(
        """SELECT r.id,r.started_at,r.status FROM collector_runs r
        WHERE r.source_id=? AND EXISTS
        (SELECT 1 FROM rss_item_checks c WHERE c.run_id=r.id)
        ORDER BY r.id DESC LIMIT 1""", (source_id,)
    ).fetchone()
    if not run:
        print("No received RSS headlines are recorded yet.")
        return
    print(f"{source_id} run {run['id']} | {run['started_at']} | {run['status']}")
    for item in conn.execute(
        """SELECT title,url,decision,match_ids_json FROM rss_item_checks
        WHERE run_id=? ORDER BY id DESC LIMIT ?""", (run["id"], limit)
    ):
        print(f"{item['decision']} | {item['title']} | {item['url']} | issuer IDs {item['match_ids_json']}")


def price_audit(conn, limit: int) -> None:
    condition = "trade_moment IS NULL OR substr(trade_moment,1,10)!=trading_date OR strftime('%w',trading_date) IN ('0','6')"
    total = conn.execute(f"SELECT count(*) FROM price_observations WHERE {condition}").fetchone()[0]
    print(f"Price observations requiring review: {total}")
    for item in conn.execute(
        f"SELECT isin,trading_date,clean_price,nominal,observed_at,trade_moment FROM price_observations WHERE {condition} ORDER BY trading_date DESC,isin LIMIT ?",
        (limit,),
    ):
        flags = []
        if not item["trade_moment"]:
            flags.append("unverified_legacy_date")
        elif item["trade_moment"][:10] != item["trading_date"]:
            flags.append("date_mismatch")
        try:
            if date.fromisoformat(item["trading_date"]).weekday() >= 5:
                flags.append("weekend_review")
        except ValueError:
            flags.append("invalid_date")
        print(f"{item['isin']} | {item['trading_date']} | {item['clean_price']} | nominal {item['nominal']} | trade {item['trade_moment'] or '-'} | observed {item['observed_at']} | {','.join(flags)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bond-watch")
    parser.add_argument("--root", type=Path, default=project_root())
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("init-db", "resolve", "poll", "status", "portfolio", "bot", "backup", "check-telegram", "check-email", "email-daily"):
        sub.add_parser(name)
    queue = sub.add_parser("email-queue")
    queue.add_argument("--limit", type=int, default=50)
    retry = sub.add_parser("email-retry")
    choice = retry.add_mutually_exclusive_group(required=True)
    choice.add_argument("--id", type=int)
    choice.add_argument("--all-safe", action="store_true")
    recent = sub.add_parser("rss-recent")
    recent.add_argument("--limit", type=int, default=20)
    recent.add_argument("--source", choices=("cbr_rss", "cbr_site_rss", "moex_news"), default="cbr_rss")
    audit = sub.add_parser("price-audit")
    audit.add_argument("--limit", type=int, default=200)
    digest = sub.add_parser("digest")
    digest.add_argument("--dry-run", action="store_true")
    digest.add_argument("--scheduled", action="store_true")
    digest.add_argument("--email", action="store_true")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    load_env(root / ".env")
    settings = load_settings(root)
    setup_logging(root, settings)
    conn = None
    try:
        if args.command == "check-email":
            send_email(EmailConfig.from_env(), TEST_SUBJECT,
                       "Проверка SMTP Bond Watch. Если вы получили это письмо, Gmail STARTTLS работает.\n")
            print("SMTP test email sent")
            return 0
        conn = connect(root / settings.get("database", "data/bond_watch.db"))
        init_sources(conn, settings)
        sync_portfolio(conn, root)
        seed_verified_bonds(conn, root)
        if args.command == "init-db":
            print(f"Database ready: {root / settings['database']}")
        elif args.command == "resolve":
            with Http(float(settings.get("http_timeout_seconds", 12)), int(settings.get("http_retries", 2))) as http:
                resolve_portfolio(conn, root, settings, http)
            status(conn)
        elif args.command == "poll":
            poll(conn, root, settings)
            status(conn)
        elif args.command == "digest":
            send_digest(conn, settings, args.scheduled, args.dry_run, args.email)
        elif args.command == "status":
            status(conn)
        elif args.command == "email-daily":
            print(process_daily_email(conn))
        elif args.command == "email-queue":
            for row in conn.execute("SELECT id,kind,event_key,recipient,status,attempt_count,last_attempt_at,next_attempt_at,result FROM notifications WHERE kind IN ('email_urgent','email_daily') ORDER BY id DESC LIMIT ?", (max(1, min(args.limit, 500)),)):
                print(" | ".join(str(row[key] or "-") for key in row.keys()))
        elif args.command == "email-retry":
            print(retry_safe(conn, EmailConfig.from_env(), args.id))
        elif args.command == "rss-recent":
            rss_recent(conn, max(1, min(args.limit, 100)), args.source)
        elif args.command == "price-audit":
            price_audit(conn, max(1, args.limit))
        elif args.command == "portfolio":
            for isin in load_portfolio(root):
                row = conn.execute("SELECT b.*,i.name issuer_name,i.inn FROM bonds b LEFT JOIN issuers i ON i.id=b.issuer_id WHERE b.isin=?", (isin,)).fetchone()
                rating = f" | {row['rating_agency']} {row['rating']} ({row['rating_date']})" if row["rating"] else ""
                print(f"{isin} | {row['issuer_name'] or 'unresolved'} | ИНН {row['inn'] or 'unresolved'} | {row['status']}{rating}")
        elif args.command == "bot":
            run_bot(conn, root, settings, poll)
        elif args.command == "backup":
            dest = root / "data/backups" / f"bond_watch_{datetime.now():%Y%m%d_%H%M%S}.db"
            backup(conn, dest)
            print(dest)
        elif args.command == "check-telegram":
            telegram = Telegram()
            print(telegram.call("getMe"))
        return 0
    except Exception:
        logging.exception("Command failed")
        return 1
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    sys.exit(main())






