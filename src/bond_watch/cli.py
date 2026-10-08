from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import load_env, load_portfolio, load_settings, project_root
from .db import backup, connect, utcnow
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


def send_digest(conn, settings: dict, scheduled: bool, dry_run: bool) -> None:
    now = datetime.now(ZoneInfo(settings.get("digest_timezone", "Europe/Moscow")))
    slot = f"{now:%Y-%m-%d-%H}" if scheduled else f"manual-{utcnow()}"
    key = f"digest:{slot}"
    last = conn.execute("SELECT value FROM bot_state WHERE key='last_digest_at'").fetchone()
    cutoff = utcnow()
    parts = digest_parts(conn, since=last["value"] if last else None, until=cutoff)
    if dry_run:
        print("\n\n".join(parts))
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
    print("Bonds:")
    for row in conn.execute("SELECT b.isin,b.status,b.resolution_error,i.name FROM bonds b LEFT JOIN issuers i ON i.id=b.issuer_id ORDER BY b.isin"):
        print(f"  {row['isin']}: {row['name'] or 'unresolved'} [{row['status']}] {row['resolution_error'] or ''}")
    print("Notifications:")
    for row in conn.execute("SELECT kind,status,created_at,error FROM notifications ORDER BY id DESC LIMIT 5"):
        print(f"  {row['kind']}: {row['status']} {row['created_at']} {row['error'] or ''}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bond-watch")
    parser.add_argument("--root", type=Path, default=project_root())
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("init-db", "resolve", "poll", "status", "portfolio", "bot", "backup", "check-telegram"):
        sub.add_parser(name)
    digest = sub.add_parser("digest")
    digest.add_argument("--dry-run", action="store_true")
    digest.add_argument("--scheduled", action="store_true")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    load_env(root / ".env")
    settings = load_settings(root)
    setup_logging(root, settings)
    conn = connect(root / settings.get("database", "data/bond_watch.db"))
    try:
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
            send_digest(conn, settings, args.scheduled, args.dry_run)
        elif args.command == "status":
            status(conn)
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
        conn.close()


if __name__ == "__main__":
    sys.exit(main())






